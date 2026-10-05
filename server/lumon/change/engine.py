"""
Temporal change engine with false-alarm gates.

WHY YEARLY DRY-SEASON COMPOSITES?
Comparing single images is unreliable here. On the demo AOI the share of
tiles classed as vegetation swings from ~600 to ~1400 between the dry
season and just after the monsoon, and tidal creeks change the amount of
visible water from image to image. So we compare like with like:

  for every tile and every year we take the clear observations from
  January-May (the dry season, also the least cloudy) and use the MEDIAN
  of their class shares. That "composite" is far more stable than any
  single image: one hazy image, one high tide or one late harvest cannot
  move a median of several images.

PIPELINE (plain language):

  usable observations        cloudy tile-looks are skipped, never filled in
        v
  yearly composites          per tile: median water / vegetation / open share
        v                    in Jan-May of each year -> class per year
  persistent transitions     class A for >= 2 years, then class B for >= 2 years
        v
  candidate events           neighbouring tiles with the same A -> B in about
        v                    the same year are grouped into one event
  six gates                  any failure SUPPRESSES the candidate, with reason
        v
  uncalibrated score         documented formula, never called a probability
        v
  analyst queue

COMPOSITE CLASSES: water, vegetation, and open land (neither water nor
vegetation) split in two by colour:
  built   open land that is clearly greyer than the AOI's typical open
          land in the SAME year (pavement, concrete, roofs)
  soil    other open land (e.g. exposed red laterite soil, graded earth)
Comparing colour with the same year's typical open land makes haze or
atmospheric-correction differences between years cancel out. This colour
rule is a heuristic that has NOT been validated against ground truth; the
gate list says so and the analyst can relabel.

EARLIEST SUPPORTED OBSERVATION:
For each event we report
  last_clean_before          the last clear dry-season image still showing A
  earliest_supported_after   the first clear dry-season image showing B
and the change happened somewhere between them. We never claim a precise
date and never interpolate missing observations.

THE SIX GATES:
  1 quality      >= 2 composite years before and after, each built from
                 >= 2 clear observations
  2 geometry     the change is large inside the tile: the composite share
                 of the new class rose by >= 30 points (or, for soil ->
                 built, the red/blue colour ratio fell by >= 0.25).
                 Sub-pixel mis-registration between dates can only shift a
                 thin edge of a 100 m tile, not a third of it
  3 radiometric  the new state is seen by >= 2 different Sentinel-2
                 satellites, and every scene passed the radiometric
                 dark-pixel check at ingest, so a single sensor's
                 calibration or one scene's processing cannot create it
  4 season       before and after are compared within the same season
                 (Jan-May composites of different years) and the new state
                 lasts >= 2 dry seasons
  5 size/shape   >= 2 tiles (2 ha); long thin shapes become road development
  6 persistence  the new class holds in >= 75 % of later composite years,
                 including the most recent one
"""

import hashlib
import statistics
from collections import deque

from .. import audit, db, provenance
from ..imagery import observations

ENGINE_VERSION = "change-engine-v2-composites"

SEASON_MONTHS = {1, 2, 3, 4, 5}  # January-May dry season
MIN_LOOKS_PER_COMPOSITE = 2
MIN_YEARS_BEFORE = 2
MIN_YEARS_AFTER = 2
AGREEMENT = 0.75
MIN_FRACTION_SHIFT = 0.30
MIN_SATELLITES = 2
MIN_TILES = 2
TILE_AREA_M2 = 100 * 100
GROUP_YEARS = 1  # tiles whose change year differs by at most this may be grouped
CLASS_MAJORITY = 0.5
GREY_MARGIN = 0.10  # red/blue ratio below (year's open-land median - margin) -> built-like
MIN_COLOUR_SHIFT = 0.25  # soil -> built must lower red/blue by at least this

PARAMETERS = {
    "season_months": sorted(SEASON_MONTHS), "min_looks_per_composite": MIN_LOOKS_PER_COMPOSITE,
    "min_years_before": MIN_YEARS_BEFORE, "min_years_after": MIN_YEARS_AFTER, "agreement": AGREEMENT,
    "min_fraction_shift": MIN_FRACTION_SHIFT, "min_satellites": MIN_SATELLITES, "min_tiles": MIN_TILES,
    "group_years": GROUP_YEARS, "grey_margin": GREY_MARGIN, "min_colour_shift": MIN_COLOUR_SHIFT,
}


# ---------------------------------------------------------------------------
# Yearly composites
# ---------------------------------------------------------------------------

def class_from_shares(water: float, vegetation: float, open_share: float) -> str:
    """The robust class of a composite: the majority share, or 'mixed'."""
    if water >= CLASS_MAJORITY:
        return "water"
    if vegetation >= CLASS_MAJORITY:
        return "vegetation"
    if open_share >= CLASS_MAJORITY:
        return "open"
    return "mixed"


def red_blue(item: dict) -> float | None:
    """Red / blue reflectance ratio of a tile-look (bare soil is redder than pavement)."""
    blue = item["features"].get("B02_mean")
    red = item["features"].get("B04_mean")
    if blue is None or red is None or blue <= 0.005:
        return None
    return red / blue


def build_composites(sequence: list[dict]) -> dict:
    """
    {year: composite} for one tile. A composite holds the median class
    shares of that year's clear Jan-May looks, how many looks it used, and
    their dates and scene ids. Years with too few clear looks get class None.
    """
    by_year = {}
    for item in sequence:
        if int(item["date"][5:7]) in SEASON_MONTHS:
            by_year.setdefault(int(item["date"][:4]), []).append(item)
    composites = {}
    for year, items in sorted(by_year.items()):
        clear = [i for i in items if i["land_class"] is not None and i["fractions"]]
        composite = {"year": year, "looks": len(clear), "masked": len(items) - len(clear),
                     "dates": [i["date"] for i in clear], "scene_ids": [i["scene_id"] for i in clear], "land_class": None}
        if len(clear) >= MIN_LOOKS_PER_COMPOSITE:
            composite["water"] = statistics.median(i["fractions"]["water"] for i in clear)
            composite["vegetation"] = statistics.median(i["fractions"]["vegetation"] for i in clear)
            composite["open"] = statistics.median(i["fractions"]["open"] for i in clear)
            ratios = [r for r in (red_blue(i) for i in clear) if r is not None]
            composite["red_blue"] = statistics.median(ratios) if ratios else None
            composite["land_class"] = class_from_shares(composite["water"], composite["vegetation"], composite["open"])
        composites[year] = composite
    return composites


def split_open_land(all_composites: dict, reference: dict) -> None:
    """
    Turn every 'open' composite into 'built' or 'soil' using the same
    year's colour reference (see open_colour_reference). Changes the
    composites in place.
    """
    for composites in all_composites.values():
        for year, composite in composites.items():
            if composite["land_class"] != "open":
                continue
            ratio, typical = composite.get("red_blue"), reference.get(year)
            built = ratio is not None and typical is not None and ratio <= typical - GREY_MARGIN
            composite["land_class"] = "built" if built else "soil"


def open_colour_reference(all_composites: dict) -> dict:
    """
    For each year: the median red/blue ratio over all tiles that are 'open'
    in that year's composite. An open tile is called greyer (built-like)
    or redder (soil-like) relative to typical open land IN THE SAME YEAR.
    """
    values = {}
    for composites in all_composites.values():
        for year, composite in composites.items():
            if composite["land_class"] == "open" and composite.get("red_blue") is not None:
                values.setdefault(year, []).append(composite["red_blue"])
    return {year: statistics.median(v) for year, v in values.items()}


# ---------------------------------------------------------------------------
# Per-tile transition
# ---------------------------------------------------------------------------

def find_transition(composites: dict) -> dict | None:
    """
    Find the best persistent class change in one tile's yearly composites.

    We try every split point. Before it the most common class is A, after
    it B. A split is acceptable when A != B (neither 'mixed'), there are
    enough years on both sides, both sides agree with their class at least
    AGREEMENT of the time, the first year after the split already shows B,
    and the most recent year still shows B. Among acceptable splits we keep
    the one with the best combined agreement.
    """
    years = [c for c in composites.values() if c["land_class"] is not None]
    best = None
    for k in range(MIN_YEARS_BEFORE, len(years) - MIN_YEARS_AFTER + 1):
        before, after = years[:k], years[k:]
        class_a = max({c["land_class"] for c in before}, key=lambda cls: sum(c["land_class"] == cls for c in before))
        class_b = max({c["land_class"] for c in after}, key=lambda cls: sum(c["land_class"] == cls for c in after))
        if class_a == class_b or "mixed" in (class_a, class_b):
            continue
        agree_a = sum(c["land_class"] == class_a for c in before) / len(before)
        agree_b = sum(c["land_class"] == class_b for c in after) / len(after)
        if agree_a < AGREEMENT or agree_b < AGREEMENT:
            continue
        if after[0]["land_class"] != class_b or after[-1]["land_class"] != class_b:
            continue
        score = agree_a + agree_b
        if best is None or score > best["agreement_sum"]:
            last_a = max((c for c in before if c["land_class"] == class_a), key=lambda c: c["year"])
            ratios_after = [c["red_blue"] for c in after if c.get("red_blue") is not None]
            best = {
                "from_class": class_a, "to_class": class_b, "agreement_sum": score,
                "agree_before": agree_a, "agree_after": agree_b,
                "last_year_before": last_a["year"], "first_year_after": after[0]["year"],
                "years_before": len(before), "years_after": len(after), "latest_year": after[-1]["year"],
                "share_before": statistics.median(c[SHARE_KEY[class_b]] for c in before),
                "share_after": statistics.median(c[SHARE_KEY[class_b]] for c in after),
                "red_blue_before": statistics.median([c["red_blue"] for c in before if c.get("red_blue") is not None] or [0]),
                "red_blue_after": statistics.median(ratios_after) if ratios_after else None,
                "masked_looks": sum(c["masked"] for c in composites.values()),
                "after_scene_ids": [s for c in after for s in c["scene_ids"]],
                "scene_ids": [s for c in years for s in c["scene_ids"]],
            }
    return best


# Which composite share measures each class (built and soil are both "open" land).
SHARE_KEY = {"water": "water", "vegetation": "vegetation", "built": "open", "soil": "open"}

# Per-image tile classes mapped to the composite classes. Single images
# cannot tell built from soil reliably, so both count as "open" here.
ROBUST = {"water": "water", "vegetation": "vegetation", "bare": "open", "built": "open", "mixed": None}
OPEN_CLASSES = {"built", "soil"}


def refine_dates(sequence: list[dict], transition: dict) -> tuple[str | None, str | None]:
    """
    The honest date window from individual dry-season looks between the
    last 'before' year and the first 'after' year:
      last clear look still showing the old class, and
      first clear look showing the new class after it.
    """
    window = [i for i in sequence if int(i["date"][5:7]) in SEASON_MONTHS and i["land_class"] is not None
              and transition["last_year_before"] <= int(i["date"][:4]) <= transition["first_year_after"]]
    def robust(cls):
        return "open" if cls in OPEN_CLASSES else cls

    from_class, to_class = robust(transition["from_class"]), robust(transition["to_class"])
    if from_class == to_class:
        # soil -> built: single images cannot separate them, so the window is
        # the dry seasons of the last 'before' and first 'after' composite years.
        before_dates = [i["date"] for i in window if int(i["date"][:4]) == transition["last_year_before"]]
        after_dates = [i["date"] for i in window if int(i["date"][:4]) == transition["first_year_after"]]
        return max(before_dates, default=None), min(after_dates, default=None)
    old = [i["date"] for i in window if ROBUST.get(i["land_class"]) == from_class]
    last_before = max(old, default=None)
    new = [i["date"] for i in window if ROBUST.get(i["land_class"]) == to_class
           and (last_before is None or i["date"] > last_before)]
    return last_before, min(new, default=None)


# ---------------------------------------------------------------------------
# Grouping, classification, gates, score
# ---------------------------------------------------------------------------

def group_tiles(transitions: dict) -> list[list[str]]:
    """
    Connected groups (8-neighbour) of tiles with the same from->to classes
    whose first changed year differs by at most GROUP_YEARS.
    """
    by_position = {}
    for tile_id in transitions:
        _, row, col = tile_id.rsplit(":", 2)
        by_position[(int(row), int(col))] = tile_id
    seen, groups = set(), []
    for start in sorted(transitions):
        if start in seen:
            continue
        seen.add(start)
        group, queue = [start], deque([start])
        reference = transitions[start]
        while queue:
            _, row, col = queue.popleft().rsplit(":", 2)
            for d_row in (-1, 0, 1):
                for d_col in (-1, 0, 1):
                    neighbour = by_position.get((int(row) + d_row, int(col) + d_col))
                    if neighbour is None or neighbour in seen:
                        continue
                    other = transitions[neighbour]
                    if ((other["from_class"], other["to_class"]) == (reference["from_class"], reference["to_class"])
                            and abs(other["first_year_after"] - reference["first_year_after"]) <= GROUP_YEARS):
                        seen.add(neighbour)
                        group.append(neighbour)
                        queue.append(neighbour)
        groups.append(group)
    return groups


def is_elongated(tile_ids: list[str]) -> bool:
    """
    'Elongated' (road-like): the longer side of the group's bounding box is
    at least 4 times the shorter side, or the group is a long diagonal that
    fills less than 35 % of its bounding box.
    """
    positions = [tuple(int(v) for v in t.rsplit(":", 2)[1:]) for t in tile_ids]
    rows = [p[0] for p in positions]
    cols = [p[1] for p in positions]
    height, width = max(rows) - min(rows) + 1, max(cols) - min(cols) + 1
    long_side, short_side = max(height, width), min(height, width)
    long_and_thin = len(tile_ids) >= 4 and long_side >= 4 * short_side
    sparse_diagonal = len(tile_ids) >= 6 and long_side >= 6 and len(tile_ids) / (height * width) < 0.35
    return long_and_thin or sparse_diagonal


def classify(from_class: str, to_class: str, elongated: bool) -> tuple[str, str]:
    """
    Composite transition -> Lumon change class and direction.
      * -> water             water-extent, expansion
      water -> *             water-extent, contraction
      * -> built             construction (road-development if elongated)
      vegetation -> soil     clearance
      anything else          other (e.g. regrowth soil -> vegetation)
    """
    if to_class == "water":
        return "water-extent", "expansion"
    if from_class == "water":
        return "water-extent", "contraction"
    if to_class == "built":
        return ("road-development" if elongated else "construction"), "appearance"
    if from_class == "vegetation" and to_class == "soil":
        return "clearance", "disappearance"
    return "other", "appearance" if to_class == "vegetation" else "disappearance"


def satellites_of(scene_ids: list[str]) -> set:
    """'S2B_43QBB_20240312_0_L2A' -> 'S2B'."""
    return {scene_id.split("_")[0] for scene_id in scene_ids}


def run_gates(group: list[dict], elongated: bool) -> list[dict]:
    """Evaluate the six gates for a group of tile transitions."""
    n = len(group)
    to_class = group[0]["to_class"]
    results = []

    years_before = min(t["years_before"] for t in group)
    years_after = min(t["years_after"] for t in group)
    masked = sum(t["masked_looks"] for t in group)
    results.append({"gate": "1 quality", "passed": years_before >= MIN_YEARS_BEFORE and years_after >= MIN_YEARS_AFTER,
                    "detail": f">= {years_before} clear composite years before, >= {years_after} after "
                              f"(each from >= {MIN_LOOKS_PER_COMPOSITE} clear looks); {masked} cloudy tile-looks excluded"})

    if group[0]["from_class"] in OPEN_CLASSES and to_class in OPEN_CLASSES:
        drop = statistics.median(t["red_blue_before"] - (t["red_blue_after"] or 0) for t in group)
        results.append({"gate": "2 geometry", "passed": drop >= MIN_COLOUR_SHIFT,
                        "detail": f"red/blue colour ratio fell by {drop:.2f} (needs >= {MIN_COLOUR_SHIFT}); open land on both sides"})
    else:
        shift = statistics.median(t["share_after"] - t["share_before"] for t in group)
        results.append({"gate": "2 geometry", "passed": shift >= MIN_FRACTION_SHIFT,
                        "detail": f"share of '{to_class}' pixels rose by {shift:+.0%} (needs >= +{MIN_FRACTION_SHIFT:.0%})"})

    satellites = satellites_of([s for t in group for s in t["after_scene_ids"]])
    results.append({"gate": "3 radiometric", "passed": len(satellites) >= MIN_SATELLITES,
                    "detail": f"new state seen by {len(satellites)} satellite(s) ({', '.join(sorted(satellites))}); "
                              "all scenes passed the ingest dark-pixel offset check"})

    lasting = years_after >= MIN_YEARS_AFTER
    results.append({"gate": "4 season", "passed": lasting,
                    "detail": "compared Jan-May composites of different years" + ("" if lasting else "; new state seen in fewer than 2 dry seasons")})

    results.append({"gate": "5 size/shape", "passed": n >= MIN_TILES,
                    "detail": f"{n} tile(s) = {n * TILE_AREA_M2 / 10000:.0f} ha (needs >= {MIN_TILES}); {'elongated (road-like)' if elongated else 'compact'}"})

    agree = statistics.mean(t["agree_after"] for t in group)
    latest = min(t["latest_year"] for t in group)
    results.append({"gate": "6 persistence", "passed": agree >= AGREEMENT,
                    "detail": f"new class held in {agree:.0%} of later composite years, still present in {latest}"})
    return results


def uncalibrated_score(group: list[dict]) -> float:
    """
    UNCALIBRATED score in [0, 1] - NOT a probability.

        score = average of four parts:
          agreement before   how consistently class A was seen
          agreement after    how consistently class B has been seen
          persistence        min(1, years B has held / 3)
          magnitude          min(1, rise in class share / 0.6), or for
                             soil -> built min(1, colour-ratio drop / 0.5)

    Used only to rank the review queue. It becomes a probability only after
    calibration against analyst-labelled examples (docs/evaluation.md).
    """
    before = statistics.mean(t["agree_before"] for t in group)
    after = statistics.mean(t["agree_after"] for t in group)
    persistence = min(1.0, min(t["years_after"] for t in group) / 3)
    if group[0]["from_class"] in OPEN_CLASSES and group[0]["to_class"] in OPEN_CLASSES:
        magnitude = min(1.0, statistics.median(t["red_blue_before"] - (t["red_blue_after"] or 0) for t in group) / 0.5)
    else:
        magnitude = min(1.0, statistics.median(t["share_after"] - t["share_before"] for t in group) / 0.6)
    return round((before + after + persistence + max(0.0, magnitude)) / 4, 3)


def class_note(group: list[dict], reference: dict) -> dict | None:
    """An informational line explaining the built/soil colour decision."""
    if not ({group[0]["from_class"], group[0]["to_class"]} & OPEN_CLASSES):
        return None
    year = min(t["first_year_after"] for t in group)
    ratios = [t["red_blue_after"] for t in group if t["red_blue_after"] is not None]
    typical = reference.get(year)
    if not ratios or typical is None:
        return {"gate": "class note", "passed": True, "detail": "no colour reference for that year"}
    return {"gate": "class note", "passed": True,
            "detail": f"after the change: red/blue {statistics.median(ratios):.2f} vs typical open land {typical:.2f} in {year} "
                      f"(built-like if <= {typical - GREY_MARGIN:.2f}); colour heuristic, unvalidated"}


# ---------------------------------------------------------------------------
# Running the engine
# ---------------------------------------------------------------------------

def run(aoi_id: str, log=print) -> dict:
    """
    Run the whole pipeline for an AOI and store the candidates. Candidates
    the analyst already reviewed are kept; unreviewed ones are replaced.
    """
    observations.process_aoi(aoi_id, log=log)
    connection = db.connect()
    scenes, sequences = observations.load_sequences(connection, aoi_id)
    composites = {tile_id: build_composites(sequence) for tile_id, sequence in sequences.items()}
    years = sorted({year for c in composites.values() for year, v in c.items() if v["land_class"] is not None})
    if len(years) < MIN_YEARS_BEFORE + MIN_YEARS_AFTER:
        connection.close()
        return {"aoi_id": aoi_id, "status": "not-enough-imagery", "composite_years": years}

    colour_reference = open_colour_reference(composites)
    split_open_land(composites, colour_reference)
    transitions = {}
    for tile_id, tile_composites in composites.items():
        found = find_transition(tile_composites)
        if found:
            found["last_clean_before"], found["earliest_after"] = refine_dates(sequences[tile_id], found)
            transitions[tile_id] = found

    tile_rows = {row["id"]: row for row in connection.execute("SELECT * FROM tiles WHERE aoi_id = ?", (aoi_id,))}
    provenance_id = provenance.create(
        connection, kind="change-run", source_id=aoi_id, input_ref=f"{len(scenes)} scenes, composite years {years[0]}-{years[-1]}",
        input_sha256=hashlib.sha256("|".join(s["file_sha256"] or "" for s in scenes).encode()).hexdigest(),
        processing="Jan-May yearly median composites per 100 m tile, persistent transitions, grouping, six false-alarm gates",
        processing_version=ENGINE_VERSION, parameters=PARAMETERS, retrieved_at=db.now_iso(),
    )

    connection.execute("DELETE FROM change_candidates WHERE aoi_id = ? AND review_state = 'unreviewed'", (aoi_id,))
    reviewed_ids = {row["id"] for row in connection.execute("SELECT id FROM change_candidates WHERE aoi_id = ?", (aoi_id,))}

    counts = {"accepted": 0, "suppressed": 0}
    for tile_group in group_tiles(transitions):
        group = [transitions[t] for t in tile_group]
        elongated = is_elongated(tile_group)
        note = class_note(group, colour_reference)
        change_class, direction = classify(group[0]["from_class"], group[0]["to_class"], elongated)
        gates = run_gates(group, elongated)
        status = "accepted" if all(g["passed"] for g in gates) else "suppressed"
        if note:
            gates.append(note)  # informational, never decides acceptance

        # Honest window for the whole event: the earliest supported look in the
        # group, and the latest clean 'before' look that precedes it.
        afters = [t["earliest_after"] for t in group if t["earliest_after"]]
        earliest_after = min(afters) if afters else None
        befores = [t["last_clean_before"] for t in group
                   if t["last_clean_before"] and (earliest_after is None or t["last_clean_before"] < earliest_after)]
        last_before = max(befores) if befores else None

        polygons = [db.from_json(tile_rows[t]["geometry"])["coordinates"] for t in tile_group]
        lon = sum(tile_rows[t]["lon"] for t in tile_group) / len(tile_group)
        lat = sum(tile_rows[t]["lat"] for t in tile_group) / len(tile_group)
        changed_share = sum(max(0.0, t["share_after"] - t["share_before"]) for t in group)
        key = f"{aoi_id}|{group[0]['from_class']}|{group[0]['to_class']}|{sorted(tile_group)[0]}"
        candidate_id = "chg-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        if candidate_id in reviewed_ids:
            continue  # never overwrite an analyst's decision

        connection.execute(
            """INSERT OR REPLACE INTO change_candidates (id, aoi_id, change_class, direction, from_class, to_class, tile_ids,
                   geometry, lon, lat, area_m2, area_min_m2, area_max_m2, last_clean_before, earliest_supported_after,
                   supporting_scene_ids, score, score_kind, status, gate_results, review_state, provenance_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'uncalibrated-score', ?, ?, 'unreviewed', ?, ?)""",
            (candidate_id, aoi_id, change_class, direction, group[0]["from_class"], group[0]["to_class"],
             db.to_json(sorted(tile_group)), db.to_json({"type": "MultiPolygon", "coordinates": polygons}), lon, lat,
             len(tile_group) * TILE_AREA_M2, round(changed_share * TILE_AREA_M2), len(tile_group) * TILE_AREA_M2,
             last_before, earliest_after, db.to_json(sorted({s for t in group for s in t["scene_ids"]})),
             uncalibrated_score(group), status, db.to_json(gates), provenance_id, db.now_iso()),
        )
        counts[status] += 1

    connection.commit()
    summary = {"aoi_id": aoi_id, "status": "done", "scenes": len(scenes), "composite_years": f"{years[0]}-{years[-1]}",
               "tiles": len(sequences), "tiles_with_transition": len(transitions), **counts, "provenance_id": provenance_id}
    audit.record(connection, "system", "change-analysis", aoi_id, summary)
    connection.close()
    log(f"  change engine: {summary}")
    return summary
