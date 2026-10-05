"""
Deterministic query parser: turn an analyst's sentence into a structured,
editable QUERY PLAN.

Example:
    "Show newly constructed areas near rivers since January"
becomes
    intent:    find-changes
    change:    construction (appearance)
    reference: river
    distance:  not given -> marked as assumed so the analyst can edit it
    time:      2026-01-01 -> now

HOW IT WORKS:
The parser looks for known words and patterns (vocabulary tables below).
Every piece it recognises becomes one "chip" in the plan, recording the
exact words it came from. Words it does not understand are listed in
`unrecognised` instead of being guessed at. Values the analyst did not
give but the engine needs (such as a search radius) are filled with a
default and flagged `assumed: true`, so the UI can show them for editing.

No language model is used. A local LLM could later propose plans, but this
parser must keep working without one.
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone

from ..geo import places

# ---------------------------------------------------------------------------
# Vocabulary tables. Each entry: (regular expression, value).
# Order matters: earlier, more specific patterns win.
# ---------------------------------------------------------------------------

CHANGE_TERMS = [
    (r"\bnew(ly)?\s+(built|constructed)\b|\bconstruct\w*\b|\bnew\s+buildings?\b|\bbuildings?\s+(added|built|constructed|appeared)\b|\bbuilt[- ]up\b|\burban(isation|ization)\b", ("construction", "appearance")),
    (r"\bnew\s+roads?\b|\broad\s+(development|construction|building)\b", ("road-development", "appearance")),
    (r"\bclear(ed|ing|ance)\b|\bdeforest\w*\b|\btree\s+loss\b|\bvegetation\s+loss\b", ("clearance", "disappearance")),
    (r"\bwater\s+(expansion|increase|growth|gain)\b|\bexpanding\s+water\b|\bnew\s+water\b|\bwater\s+extent\s+increase\b", ("water-extent", "expansion")),
    (r"\bwater\s+(loss|decrease|shrink\w*|contraction)\b|\bdrying\b|\bshrinking\s+(lakes?|reservoirs?|water)\b", ("water-extent", "contraction")),
    (r"\bwater\s+(changes?|extent)\b", ("water-extent", None)),
    (r"\bchanges?\b|\bchanged\b", ("any", None)),
]

EVENT_TERMS = [
    (r"\bearthquakes?\b|\bquakes?\b|\bseismic\b", ["earthquake"]),
    (r"\b(wild)?fires?\b|\bburning\b|\bthermal anomal\w*\b", ["fire-detection", "wildfire"]),
    (r"\bfloods?\b|\bflooding\b", ["flood"]),
    (r"\bcyclones?\b|\bstorms?\b|\btyphoons?\b", ["cyclone", "severe-storm"]),
    (r"\blandslides?\b", ["landslide"]),
    (r"\bdroughts?\b", ["drought"]),
    (r"\baircraft\b|\bflights?\b|\bplanes?\b|\baviation\b", ["aircraft-position"]),
    (r"\blaunch(es)?\b|\brockets?\b", ["launch"]),
    (r"\bsatellites?\b|\bspacecraft\b|\bovers?head\b", ["satellite-position"]),
    (r"\b(internet|network)\s+(outages?|disruptions?|shutdowns?)\b|\boutages?\b", ["internet-outage-signal"]),
    (r"\bweather\b|\btemperatures?\b|\brain(fall)?\b|\bwind\b", ["weather-current"]),
    (r"\bspace weather\b|\bgeomagnetic\b|\bkp\b", ["geomagnetic-kp"]),
    (r"\bvessels?\b|\bships?\b|\bboats?\b|\bmaritime traffic\b", ["vessel-position"]),
]

ENTITY_TERMS = [
    (r"\bairports?\b|\bairfields?\b", ["airport"]),
    (r"\bports?\b|\bharbou?rs?\b", ["port"]),
    (r"\bpower\s+(plants?|stations?)\b|\bpower\b", ["power-plant"]),
]

# Reference features used in "near X" relations.
REFERENCE_TERMS = [
    (r"\brivers?\b|\briverside\b|\bwater\s*bod(y|ies)\b", "river"),
    (r"\blakes?\b|\breservoirs?\b", "lake"),
    (r"\bcoast(s|al|line)?\b", "coast"),
    (r"\broads?\b|\bhighways?\b", "road"),
    (r"\brail(way|road)s?\b", "railway"),
]

SENSOR_TERMS = [
    (r"\bsentinel[- ]?2\b|\bs2\b", "sentinel-2"),
    (r"\bsentinel[- ]?1\b|\bs1\b|\bsar\b|\bradar\b", "sentinel-1"),
    (r"\blandsat\b", "landsat"),
]

SIMILAR_TERMS = r"\bsimilar\b|\bmore like\b|\blike this\b|\blook(s)? like\b"

# ---------------------------------------------------------------------------
# Study-area scope. India is the default; the NIT Raipur pilot has two
# levels: the study area and the campus itself. Campus features such as
# hostels only exist on the campus, so they imply the campus scope.
# ---------------------------------------------------------------------------
STUDY_AREA_TERMS = [
    (r"\bnit\s*raipur\s+campus\b|\bon\s+(the\s+)?campus\b|\bcampus\b", "nit-raipur-campus"),
    (r"\bhostels?\b|\bacademic\s+blocks?\b", "nit-raipur-campus"),
    (r"\bnit\s*raipur\b|\bnational\s+institute\s+of\s+technology(\s+raipur)?\b|\bnit\b", "nit-raipur"),
]

# Campus features used as a "near X" reference (need the building inventory).
CAMPUS_FEATURES = [(r"\bhostels?\b", "hostels"), (r"\bacademic\s+blocks?\b", "academic blocks")]

# What kind of object/surface the question is about.
TARGET_CLASSES = [
    (r"\bbuildings?\b", "building"),
    (r"\bvegetation\b|\btrees?\b|\bgreen\s+cover\b|\bgreenery\b", "vegetation"),
    (r"\bponds?\b|\bwater\s+bod(y|ies)\b", "water"),
    (r"\bpaths?\b|\bpathways?\b", "road"),
]

# Semantic image search (RemoteCLIP): an EXPLICIT request for images, e.g.
# "show satellite images of an airport runway", "find imagery showing
# mangroves since 2023". The description after "of/showing/with/containing"
# is passed to the model as free text (it is not matched against Lumon's
# vocabulary). Anything not phrased this way is parsed exactly as before.
IMAGE_SEARCH = (r"^\s*(?:please\s+)?(?:(?:find|show|search(?:\s+for)?|get|look\s+for|display)\s+(?:me\s+)?)?"
                r"(?:(?:satellite|sentinel(?:-2)?)\s+)?(?:images?|imagery|image\s+chips?|scenes?)\s+"
                r"(?:of|showing|that\s+shows?|with|containing|that\s+contains?)\s+(?P<what>.+?)\s*[.?!]?\s*$")

# Which AI/ML capability (config/ai/capabilities.json) an operation needs.
OPERATION_TERMS = [
    (r"\bsegment\w*\b|\boutline\w*\b|\bfootprints?\b", "segmentation"),
    (r"\bland[- ](cover|use)\b", "land-cover"),
    (r"\bsummar\w*\b", "analyst-summary"),
    (r"\btrends?\b|\bover\s+time\b|\bseasonal\w*\b", "temporal-analysis"),
]

# Words that qualify a request but have no defined threshold.
QUALIFIERS = {"significant": "'significant' has no defined threshold: every persistent change that passes the false-alarm gates is returned, ranked by score."}
HERE_TERMS = r"\b(this|selected|current)\s+(location|place|area|site|aoi)\b|\bhere\b"

MONTHS = {name: index for index, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

# Words that carry no meaning for the plan; never reported as unrecognised.
STOPWORDS = set("""
show find list get display give me all any the a an of in on at to for from with within around near nearby
by and or that which where what are is were was be been this these those areas area sites site locations
location places place recent recently new newly since last past during between before after until please
there their its into over about than more most activity activities reported report imagery image images
showing happening occurred occurring
""".split())

# Default values used when the analyst did not say. Always flagged as assumed.
DEFAULT_RADIUS_M = 50_000
DEFAULT_RECENT_DAYS = 30


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _first_match(text: str, table):
    """Return (value, matched words) for the first table pattern found in text."""
    for pattern, value in table:
        match = re.search(pattern, text)
        if match:
            return value, match.group(0)
    return None, None


def parse_distance(text: str) -> dict | None:
    """Find an explicit distance such as '200 m', '5 km' or '2.5 kilometres'."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(km|kms|kilomet(?:er|re)s?|m|meters?|metres?)\b", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    metres = value * 1000 if unit.startswith("k") else value
    return {"value_m": metres, "matched": match.group(0), "assumed": False}


def parse_time(text: str, now: datetime) -> dict | None:
    """
    Understand simple time expressions. Supported forms:
      since <month> / since <month> <year> / since <year>
      last|past N days|weeks|months|years
      today / yesterday / this week / this month / this year
      in <year> / in <month> <year>
      recent / recently / latest  (-> last 30 days, flagged as assumed)
    Returns {"start", "end", "matched", "assumed"} or None.
    """
    match = re.search(r"\bsince\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?\b", text)
    if match:
        month = MONTHS[match.group(1)]
        year = int(match.group(2)) if match.group(2) else (now.year if month <= now.month else now.year - 1)
        return {"start": _iso(datetime(year, month, 1, tzinfo=timezone.utc)), "end": None, "matched": match.group(0), "assumed": False}

    match = re.search(r"\bafter\s+(\d{4})\b", text)
    if match:
        # "after 2020" = from the start of 2021 (the whole of 2020 is excluded).
        return {"start": _iso(datetime(int(match.group(1)) + 1, 1, 1, tzinfo=timezone.utc)), "end": None, "matched": match.group(0), "assumed": False}

    match = re.search(r"\bbefore\s+(\d{4})\b", text)
    if match:
        return {"start": None, "end": _iso(datetime(int(match.group(1)), 1, 1, tzinfo=timezone.utc)), "matched": match.group(0), "assumed": False}

    match = re.search(r"\bsince\s+(\d{4})\b", text)
    if match:
        return {"start": _iso(datetime(int(match.group(1)), 1, 1, tzinfo=timezone.utc)), "end": None, "matched": match.group(0), "assumed": False}

    match = re.search(r"\b(?:last|past)\s+(\d+)?\s*(day|week|month|year)s?\b", text)
    if match:
        count = int(match.group(1) or 1)
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[match.group(2)] * count
        return {"start": _iso(now - timedelta(days=days)), "end": None, "matched": match.group(0), "assumed": False}

    match = re.search(r"\bin\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", text)
    if match:
        year, month = int(match.group(2)), MONTHS[match.group(1)]
        start = datetime(year, month, 1, tzinfo=timezone.utc)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
        return {"start": _iso(start), "end": _iso(end), "matched": match.group(0), "assumed": False}

    match = re.search(r"\bin\s+(\d{4})\b", text)
    if match:
        year = int(match.group(1))
        return {"start": _iso(datetime(year, 1, 1, tzinfo=timezone.utc)), "end": _iso(datetime(year + 1, 1, 1, tzinfo=timezone.utc)), "matched": match.group(0), "assumed": False}

    simple = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "yesterday": now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1),
        "this week": now - timedelta(days=now.weekday()),
        "this month": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        "this year": now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0),
    }
    for phrase, start in simple.items():
        if re.search(rf"\b{phrase}\b", text):
            return {"start": _iso(start), "end": None, "matched": phrase, "assumed": False}

    match = re.search(r"\b(recent(ly)?|latest|current(ly)?|active|ongoing)\b", text)
    if match:
        return {"start": _iso(now - timedelta(days=DEFAULT_RECENT_DAYS)), "end": None, "matched": match.group(0), "assumed": True,
                "note": f"'{match.group(0)}' interpreted as the last {DEFAULT_RECENT_DAYS} days"}
    return None


def parse_filters(text: str) -> dict:
    """Numeric filters such as 'magnitude 4+', 'M5 and above', 'above magnitude 4.5'."""
    filters = {}
    match = re.search(r"\b(?:magnitude|mag|m)\s*(\d(?:\.\d)?)\s*(?:\+|and above|or (?:more|greater|higher))?", text) \
        or re.search(r"\b(?:above|over|greater than)\s+(?:magnitude\s+)?(\d(?:\.\d)?)\b", text)
    if match:
        filters["min_magnitude"] = {"value": float(match.group(1)), "matched": match.group(0)}
    return filters


def parse_study_area(text: str) -> dict:
    """
    Which area the question is scoped to:
      india              the whole India operating area (default)
      nit-raipur         the NIT Raipur pilot study area
      nit-raipur-campus  the campus itself (needs a campus boundary)
    The status comes from the pilot module (PROVISIONAL / VERIFIED ...).
    """
    from .. import pilot  # local import: the pilot module reads config files

    scope, matched = _first_match(text, STUDY_AREA_TERMS)
    if not scope:
        return {"id": "india", "name": "India operating area", "level": "country", "status": None, "matched": None}
    area = pilot.study_area()
    info = next(s for s in pilot.load_pilot()["study_areas"] if s["id"] == scope)
    if scope == "nit-raipur-campus":
        status = area["status"] if area["campus_boundary"] else "NOT STAGED"
    else:
        status = area["status"]
    return {"id": scope, "name": info["name"], "level": info["level"], "status": status, "matched": matched}


def infer_operation(plan: dict, text: str) -> dict | None:
    """
    The AI/ML capability (config/ai/capabilities.json) the request needs,
    with its current status. OSINT event/entity searches need none.
    """
    from .. import pilot

    capability_id, matched = _first_match(text, OPERATION_TERMS)
    target = (plan.get("target_class") or {}).get("class")
    change = (plan.get("change") or {}).get("class")
    if not capability_id:
        if plan["intent"] == "find-similar":
            capability_id = "image-similarity"
        elif plan["intent"] == "find-changes" and target == "vegetation":
            capability_id = "vegetation"
        elif plan["intent"] == "find-changes" and (change in ("construction", "road-development") or target == "building"):
            capability_id = "construction"
        elif plan["intent"] == "find-changes":
            capability_id = "change-detection"
        elif target == "vegetation":
            capability_id = "vegetation"
    if not capability_id:
        return None
    item = pilot.capability(capability_id)
    return {"id": capability_id, "name": item["name"], "status": item["status"], "fallback": item.get("fallback"), "matched": matched}


def _image_search_plan(plan: dict, what: str, lowered: str, now: datetime) -> dict:
    """
    Plan for semantic image search. A time phrase inside the description
    ("... since 2023") becomes the time filter and is removed from the text
    sent to the model; the NIT Raipur scope is kept so pilot requests report
    NOT STAGED (no pilot imagery). Nothing else is interpreted.
    """
    from .. import pilot

    plan["time"] = parse_time(lowered, now)
    if plan["time"] and plan["time"].get("matched"):
        what = what.replace(plan["time"]["matched"], " ")
    plan["study_area"] = parse_study_area(lowered)
    if plan["study_area"]["matched"]:
        what = re.sub(rf"\b(at|in|on|near|around)?\s*{re.escape(plan['study_area']['matched'])}", " ", what)
    what = " ".join(what.split()).strip(" ,.")
    plan["intent"] = "search-imagery"
    plan["semantic_text"] = what
    plan["target"] = {"kind": "imagery", "label": what}
    item = pilot.capability("semantic-search")
    plan["operation"] = {"id": "semantic-search", "name": item["name"], "status": item["status"],
                         "fallback": None, "matched": None}
    plan["notes"].append("Image search ranks staged satellite chips by RemoteCLIP model similarity to the description. "
                         "A high rank is not a detection and does not prove the object is present.")
    return plan


def parse(text: str, now: datetime | None = None) -> dict:
    """
    Build the query plan for one sentence. `now` can be passed in for
    repeatable tests. The plan is a plain dict that the UI shows as chips
    and the analyst may edit before running.
    """
    now = now or datetime.now(timezone.utc)
    original = text.strip()
    # Accent-free lower-case text (punctuation kept, so "4.5" and "200 m"
    # survive). Place names are matched separately by the gazetteer.
    lowered = unicodedata.normalize("NFKD", original).encode("ascii", "ignore").decode("ascii").lower()
    used_spans = []  # matched words, used to work out what was not understood

    def remember(matched):
        if matched:
            used_spans.append(matched.lower())

    plan = {
        "text": original, "intent": "unknown", "target": None, "change": None, "place": None,
        "distance": None, "reference": None, "time": None, "sensor": None, "filters": {},
        "evidence_type": None, "related_events": None, "unrecognised": [], "notes": [],
        "study_area": None, "operation": None, "target_class": None,
    }

    image_search = re.match(IMAGE_SEARCH, lowered)
    if image_search:
        return _image_search_plan(plan, image_search.group("what"), lowered, now)

    # Similarity ("find similar sites", "more like this").
    similar = re.search(SIMILAR_TERMS, lowered)
    if similar:
        remember(similar.group(0))

    change, change_words = _first_match(lowered, CHANGE_TERMS)
    events, event_words = _first_match(lowered, EVENT_TERMS)
    entities, entity_words = _first_match(lowered, ENTITY_TERMS)
    reference, reference_words = _first_match(lowered, REFERENCE_TERMS)
    sensor, sensor_words = _first_match(lowered, SENSOR_TERMS)

    # "new roads" is a change, so do not also treat "roads" as a reference.
    if change and change[0] == "road-development" and reference == "road":
        reference, reference_words = None, None
    # "port" inside "report", "power" inside "powerful" are avoided by \b in
    # the patterns; but "water" in a change phrase should not become a river reference.
    for value, words in ((change, change_words), (events, event_words), (entities, entity_words),
                         (reference, reference_words), (sensor, sensor_words)):
        if value:
            remember(words)

    # Decide the intent. Changes and similarity concern satellite analysis;
    # events and entities concern OSINT records.
    if similar:
        plan["intent"] = "find-similar"
        plan["target"] = {"kind": "similar", "label": "Similar sites"}
        plan["evidence_type"] = "INFERRED"
    elif change:
        plan["intent"] = "find-changes"
        plan["change"] = {"class": change[0], "direction": change[1], "matched": change_words}
        plan["target"] = {"kind": "change", "label": change[0]}
        plan["evidence_type"] = "OBSERVED"
        # "...where recent flood activity was reported": an OSINT condition
        # attached to a change search. Kept as context, used for fusion.
        if events:
            plan["related_events"] = {"types": events, "matched": event_words}
    elif events:
        plan["intent"] = "find-events"
        plan["target"] = {"kind": "event", "types": events, "label": event_words, "matched": event_words}
    elif entities:
        plan["intent"] = "find-entities"
        plan["target"] = {"kind": "entity", "types": entities, "label": entity_words, "matched": entity_words}
        plan["evidence_type"] = "GIS-DERIVED"

    # Study area: India by default, or the NIT Raipur pilot (study area or campus).
    plan["study_area"] = parse_study_area(lowered)
    if plan["study_area"]["matched"]:
        remember(plan["study_area"]["matched"])
    feature, feature_words = _first_match(lowered, CAMPUS_FEATURES)
    if feature:
        plan["reference"] = {"kind": f"campus:{feature}", "matched": feature_words,
                             "note": f"'{feature}' needs the campus building inventory, which is not staged."}
        remember(feature_words)
    in_pilot = plan["study_area"]["id"] != "india"

    # Place: "this location" means the analyst's map selection. Inside the
    # pilot, "Raipur" is part of the study-area name, not the city gazetteer point.
    here = re.search(HERE_TERMS, lowered)
    if here:
        plan["place"] = {"kind": "map-selection", "name": "Selected location", "matched": here.group(0)}
        remember(here.group(0))
    elif not in_pilot:
        place, place_words = places.find_in_text(original)
        if place:
            plan["place"] = {**place, "matched": place_words}
            remember(place_words)

    plan["distance"] = parse_distance(lowered)
    if plan["distance"]:
        remember(plan["distance"]["matched"])

    if reference and not plan["reference"]:
        plan["reference"] = {"kind": reference, "matched": reference_words}

    # A "near" relation with no distance needs a radius: use a default but say so.
    near = re.search(r"\b(near|nearby|around|close to|within)\b", lowered)
    if near and not plan["distance"] and (plan["reference"] or plan["place"]):
        plan["distance"] = {"value_m": DEFAULT_RADIUS_M, "matched": near.group(0), "assumed": True,
                            "note": f"No distance given; {DEFAULT_RADIUS_M // 1000} km used. Edit if needed."}
        remember(near.group(0))
    if plan["place"] and plan["place"].get("kind") == "city" and not plan["distance"] and plan["intent"] != "unknown":
        plan["distance"] = {"value_m": DEFAULT_RADIUS_M, "matched": None, "assumed": True,
                            "note": f"Search radius around {plan['place']['name']} not given; {DEFAULT_RADIUS_M // 1000} km used."}

    plan["time"] = parse_time(lowered, now)
    if plan["time"]:
        remember(plan["time"]["matched"])

    # Target class and the AI/ML capability the operation needs.
    target, target_words = _first_match(lowered, TARGET_CLASSES)
    if target:
        plan["target_class"] = {"class": target, "matched": target_words}
        remember(target_words)
    plan["operation"] = infer_operation(plan, lowered)
    if plan["operation"] and plan["operation"].get("matched"):
        remember(plan["operation"]["matched"])
    for word, note in QUALIFIERS.items():
        if re.search(rf"\b{word}\b", lowered):
            plan["notes"].append(note)
            remember(word)

    plan["sensor"] = sensor
    plan["filters"] = parse_filters(lowered)
    for item in plan["filters"].values():
        remember(item["matched"])

    # Anything left over that is not a stopword is reported, never guessed.
    # Compare in fully normalised form (no punctuation) on both sides.
    remaining = f" {places.normalise(lowered)} "
    for span in sorted((places.normalise(s) for s in used_spans), key=len, reverse=True):
        if span:
            remaining = remaining.replace(f" {span} ", " ")
    words = re.findall(r"[a-z][a-z\-]+", remaining)
    plan["unrecognised"] = [word for word in words if word not in STOPWORDS]

    if plan["intent"] == "unknown" and plan["operation"]:
        plan["intent"] = "analyse-imagery"  # an imagery capability with no change/event wording
        plan["target"] = {"kind": "capability", "label": plan["operation"]["name"]}
    if plan["intent"] == "unknown":
        if plan["place"] and not plan["unrecognised"]:
            plan["intent"] = "locate"
            plan["target"] = {"kind": "place", "label": plan["place"]["name"]}
        else:
            plan["notes"].append("Could not tell what to search for. Try naming an event type, a change type, or a place.")
    return plan
