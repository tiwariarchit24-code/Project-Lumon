"""
Execute a (possibly analyst-edited) query plan.

THE QUERY PATH:
  plan -> capability check -> search area -> filters (type, time, numbers)
       -> spatial relation -> ranking -> results with "why it matched"

The capability check runs first and says plainly what cannot be answered
(for example "no vessel source is integrated" or "no imagery is staged
for that area") instead of returning an empty list that looks like
"nothing happened".
"""


from .. import db, settings
from ..geo import boundary, geometry
from ..imagery import aoi as aoi_module
from ..sources import registry

# Event types that have at least one implemented source, and which source.
EVENT_TYPE_SOURCES = {
    "earthquake": ["usgs-earthquakes", "gdacs-alerts", "nasa-eonet"],
    "fire-detection": ["nasa-firms"],
    "wildfire": ["nasa-eonet", "gdacs-alerts"],
    "flood": ["gdacs-alerts", "nasa-eonet"],
    "cyclone": ["gdacs-alerts"],
    "severe-storm": ["nasa-eonet"],
    "landslide": ["nasa-eonet"],
    "drought": ["gdacs-alerts", "nasa-eonet"],
    "volcano": ["gdacs-alerts", "nasa-eonet"],
    "disaster-alert": ["gdacs-alerts"],
    "natural-event": ["nasa-eonet"],
    "aircraft-position": ["opensky-aircraft"],
    "launch": ["launch-library"],
    "satellite-position": ["celestrak-satellites"],
    "internet-outage-signal": ["ioda-outages"],
    "weather-current": ["open-meteo-current"],
    "geomagnetic-kp": ["noaa-swpc-kp"],
}
ENTITY_TYPE_SOURCES = {"airport": ["ourairports"], "port": ["natural-earth-ports"], "power-plant": ["wri-power-plants"]}


# ---------------------------------------------------------------------------
# Search area
# ---------------------------------------------------------------------------

def search_area(plan: dict) -> dict:
    """
    Work out WHERE to search. Returns one of:
      {"kind": "operating-area"}                     all of India
      {"kind": "polygon", "geometry", "bbox", "name"} a state or district
      {"kind": "circle", "lon", "lat", "radius_m", "name"}
    """
    place = plan.get("place")
    distance = (plan.get("distance") or {}).get("value_m")
    study = plan.get("study_area") or {}
    if study.get("id", "india") != "india":
        # NIT Raipur pilot: its (possibly PROVISIONAL) study-area polygon.
        from .. import pilot
        area = pilot.study_area()
        if area["geometry"]:
            return {"kind": "polygon", "geometry": area["geometry"], "bbox": geometry.bbox(area["geometry"]),
                    "name": f"{study.get('name')} ({area['status']})"}
        return {"kind": "unresolved", "name": study.get("name")}
    if not place:
        return {"kind": "operating-area", "name": "India operating area"}
    if place.get("kind") in ("state", "district") and not (plan.get("distance") and not plan["distance"].get("assumed")):
        dataset = "india-states" if place["kind"] == "state" else "india-districts"
        collection = boundary.load_boundary(dataset) or {"features": []}
        for feature in collection["features"]:
            if feature["properties"]["name"] == place["name"]:
                return {"kind": "polygon", "geometry": feature["geometry"], "bbox": geometry.bbox(feature["geometry"]), "name": place["name"]}
    if place.get("lon") is None:
        return {"kind": "unresolved", "name": place.get("name")}
    return {"kind": "circle", "lon": place["lon"], "lat": place["lat"], "radius_m": distance or 50_000, "name": place.get("name")}


def _area_prefilter_bbox(area: dict) -> list[float] | None:
    """A rough bbox for SQL pre-filtering (1 degree latitude ~ 111 km)."""
    if area["kind"] == "polygon":
        return area["bbox"]
    if area["kind"] == "circle":
        pad = area["radius_m"] / 111_000 * 1.5
        return [area["lon"] - pad, area["lat"] - pad, area["lon"] + pad, area["lat"] + pad]
    return None


def _in_area(area: dict, lon: float | None, lat: float | None) -> tuple[bool, float | None]:
    """(inside?, distance from centre in metres or None)."""
    if area["kind"] == "operating-area":
        return True, None
    if lon is None:
        return False, None
    if area["kind"] == "polygon":
        return geometry.point_in_geometry(lon, lat, area["geometry"]), None
    distance = geometry.haversine_m(area["lon"], area["lat"], lon, lat)
    return distance <= area["radius_m"], distance


# ---------------------------------------------------------------------------
# Capability check
# ---------------------------------------------------------------------------

def capability(plan: dict, connection) -> dict:
    """
    Can this plan be answered with what is staged? Returns

      {"state": "SUPPORTED" | "PARTIAL" | "UNSUPPORTED",
       "supported": bool,       # False only for UNSUPPORTED (nothing runs)
       "issues":   [...],       # why it cannot be answered at all
       "partial":  [...],       # parts that need a source/key that is not available
       "warnings": [...]}       # caveats (stale data, AOI-only coverage...)

    PARTIAL means the plan runs, but some source that should contribute is
    missing (needs a key, failed, not implemented), so results may be
    incomplete. UNSUPPORTED means no source can answer it at all, and the
    plan is refused (for example vessels: no vessel source exists).
    """
    issues, warnings, partial = [], [], []
    intent = plan.get("intent")
    sources = {s["id"]: s for s in registry.describe_all(connection)}

    if intent == "unknown":
        issues.append("The question was not understood well enough to search.")
    if plan.get("unrecognised"):
        warnings.append(f"Ignored words: {', '.join(plan['unrecognised'])}")
    if (plan.get("place") or {}).get("kind") == "map-selection" and plan["place"].get("lon") is None:
        issues.append("The plan refers to a selected location, but nothing is selected on the map.")
    if not boundary.status()["staged"]:
        warnings.append("India boundary not staged: results are not scoped to India.")

    if intent == "find-events":
        for event_type in plan["target"]["types"]:
            providers = EVENT_TYPE_SOURCES.get(event_type)
            if not providers:
                issues.append(f"No implemented source provides '{event_type}' records.")
                continue
            for source_id in providers:
                source = sources.get(source_id, {})
                status = source.get("freshness_status", "NOT STAGED")
                if status == "KEY REQUIRED":
                    partial.append(f"'{event_type}' needs {source.get('name', source_id)}: KEY REQUIRED ({source.get('key_env')} in .env.local).")
                elif status in ("UNAVAILABLE", "NOT STAGED", "NOT IMPLEMENTED"):
                    partial.append(f"'{event_type}' source {source.get('name', source_id)} is {status}.")
                elif status == "STALE":
                    warnings.append(f"{source.get('name', source_id)} data is {source.get('freshness_label')}.")
            if not any(sources.get(s, {}).get("record_count", 0) > 0 for s in providers):
                warnings.append(f"No '{event_type}' records are currently staged.")
    if intent == "find-entities":
        for entity_type in plan["target"]["types"]:
            if not ENTITY_TYPE_SOURCES.get(entity_type):
                issues.append(f"No implemented source provides '{entity_type}' entities.")
    if intent in ("find-changes", "find-similar"):
        staged = connection.execute("SELECT COUNT(*) FROM scenes WHERE quality_status IN ('usable','degraded')").fetchone()[0]
        if staged == 0:
            issues.append("NO IMAGERY STAGED: change and similarity search need staged satellite imagery.")
        else:
            names = ", ".join(a["name"] for a in aoi_module.load_aois())
            warnings.append(f"Satellite analysis covers staged AOIs only ({names}), not all of India.")
        if plan.get("sensor") and plan["sensor"] != "sentinel-2":
            issues.append(f"Sensor '{plan['sensor']}' is not staged; only Sentinel-2 L2A is available.")
        if intent == "find-similar" and not plan.get("examples"):
            if similar_image_example(plan):
                warnings.append("Image-to-image similarity uses RemoteCLIP embeddings of the image chip at the selected location "
                                "(model similarity: looking alike is not evidence of the same objects or activity).")
            else:
                issues.append("Similarity search needs example sites: select a tile or change event, then use MORE LIKE THIS.")
    if plan.get("reference") and plan["reference"]["kind"] in ("river", "lake") and intent == "find-changes":
        distance = (plan.get("distance") or {}).get("value_m") or 0
        if distance and distance < 2000:
            warnings.append("Water reference uses the water mask derived from the AOI imagery (10 m pixels), "
                            "not a named-river dataset; Natural Earth rivers are too generalised for distances under 2 km.")
    if plan.get("reference") and plan["reference"]["kind"] in ("coast", "road", "railway") and intent != "find-changes":
        warnings.append(f"Spatial relation to '{plan['reference']['kind']}' is not applied for this search type yet.")
    if intent == "search-imagery":
        if not plan.get("semantic_text"):
            issues.append("No image description given (e.g. 'show satellite images of an airport runway').")
        elif (plan.get("study_area") or {"id": "india"})["id"] == "india":
            from ..imagery import semantic
            retrieval = semantic.status()
            if retrieval["state"] in ("NOT STAGED", "UNAVAILABLE") and not semantic.model_problems():
                issues.append(f"Semantic image search is {retrieval['state']}: " + "; ".join(retrieval["reasons"]))
            elif retrieval["state"] in ("PARTIAL", "INDEXING"):
                partial.append(f"Image index is {retrieval['state']}: {retrieval['scenes_indexed']} of {retrieval['scenes_eligible']} eligible scenes indexed.")
            names = ", ".join(a["name"] for a in aoi_module.load_aois())
            warnings.append(f"Image search covers the staged AOI imagery only ({names}), not all of India.")
            warnings.append("Scores are RemoteCLIP cosine similarity, not probabilities or detections.")
    not_staged = _pilot_and_model_checks(plan, connection, issues, partial, warnings)

    # UNSUPPORTED: nothing can answer it. NOT STAGED: understood and designed
    # for, but the data/model it needs is not staged, so nothing runs and no
    # results are invented. PARTIAL: runs, but something it needs is missing.
    if issues:
        state = "UNSUPPORTED"
    elif not_staged:
        state = "NOT STAGED"
        issues.extend(not_staged)
    else:
        state = "PARTIAL" if partial else "SUPPORTED"
    return {"state": state, "supported": not issues, "issues": issues, "partial": partial, "warnings": warnings}


def _pilot_and_model_checks(plan: dict, connection, issues: list, partial: list, warnings: list) -> list[str]:
    """
    Checks for the NIT Raipur pilot scope and for the AI/ML capability the
    plan needs. Returns NOT STAGED reasons (empty when the plan can run).

    - Inside the pilot, imagery operations need pilot imagery and (for the
      campus) a campus boundary; neither is staged, so they report NOT
      STAGED. OSINT event/entity searches still run on the study-area
      polygon, with a warning when that polygon is PROVISIONAL.
    - In the India scope, a change/similarity request whose learned model is
      NOT STAGED runs the existing non-ML fallback and is reported PARTIAL.
    """
    from .. import pilot

    reasons = []
    study = plan.get("study_area") or {"id": "india"}
    operation = plan.get("operation")
    reference = (plan.get("reference") or {}).get("kind", "")
    if reference.startswith("campus:"):
        reasons.append(plan["reference"].get("note") or "Campus features need the building inventory, which is not staged.")

    if study["id"] != "india":
        area = pilot.study_area()
        if area["status"] == "PROVISIONAL":
            warnings.append("NIT Raipur study area is PROVISIONAL (search area around Raipur's gazetteer point), not the campus boundary.")
        if study["id"] == "nit-raipur-campus" and not area["campus_boundary"]:
            reasons.append("NIT Raipur Campus boundary is NOT STAGED (expected at data/pilots/nit-raipur/campus-boundary.geojson).")
        if plan.get("intent") in ("find-changes", "find-similar", "analyse-imagery", "search-imagery"):
            staged = connection.execute("SELECT COUNT(*) FROM scenes WHERE aoi_id = ? AND quality_status IN ('usable','degraded')",
                                        (pilot.load_pilot()["id"],)).fetchone()[0]
            if not staged:
                reasons.append("No imagery is staged for the NIT Raipur pilot.")
            if operation and operation["status"] == "NOT STAGED":
                model = pilot.capability(operation["id"]).get("model")
                reasons.append(f"{operation['name']} is NOT STAGED" + (f" (candidate model '{model}' not deployed)." if model else "."))
            # The India-wide checks above (staged AOIs, similarity examples)
            # describe the demo AOI, not the pilot; keep only pilot reasons.
            issues[:] = [i for i in issues if not i.startswith(("NO IMAGERY STAGED", "Similarity search needs"))]
            warnings[:] = [w for w in warnings if not w.startswith(("Satellite analysis covers staged AOIs only", "Image search covers"))]
        return reasons

    if plan.get("intent") == "find-changes" and operation and operation["status"] != "NOT STAGED":
        # A staged learned model is never run silently from the command bar:
        # these results are the rule-based baseline, and the answer says so.
        warnings.append(f"Results come from the RULE-BASED change engine (baseline). {operation['name']} (model) is staged but "
                        "runs per scene pair in Change Explorer; its MODEL-GENERATED CANDIDATE CHANGES are listed there, separately.")
    if operation and operation["status"] == "NOT STAGED":
        if operation.get("fallback") and plan.get("intent") in ("find-changes", "find-similar"):
            partial.append(f"Learned {operation['name']} is NOT STAGED; results come from the non-ML fallback: {operation['fallback']}.")
        elif plan.get("intent") == "analyse-imagery" or not operation.get("fallback"):
            reasons.append(f"{operation['name']} is NOT STAGED; there is no non-ML fallback for it.")
    return reasons


# ---------------------------------------------------------------------------
# Searches
# ---------------------------------------------------------------------------

def _time_clause(plan: dict, column: str) -> tuple[str, list]:
    time = plan.get("time") or {}
    clauses, params = [], []
    if time.get("start"):
        clauses.append(f"{column} >= ?")
        params.append(time["start"])
    if time.get("end"):
        clauses.append(f"{column} < ?")
        params.append(time["end"])
    return (" AND ".join(clauses), params)


def search_events(plan: dict, connection, limit: int = 200) -> list[dict]:
    """Events of the requested types inside the area and time window."""
    area = search_area(plan)
    types = plan["target"]["types"]
    sql = f"SELECT * FROM events WHERE type IN ({','.join('?' * len(types))})"
    params = list(types)
    time_sql, time_params = _time_clause(plan, "start_time")
    if time_sql:
        sql += " AND " + time_sql
        params += time_params
    box = _area_prefilter_bbox(area)
    if box:
        sql += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        params += [box[0], box[2], box[1], box[3]]
    min_magnitude = (plan.get("filters") or {}).get("min_magnitude")
    results = []
    for row in connection.execute(sql + " ORDER BY start_time DESC", params):
        event = db.row_to_dict(row, ["attributes"])
        if min_magnitude and (event["attributes"].get("magnitude") or 0) < min_magnitude["value"]:
            continue
        inside, distance = _in_area(area, event["lon"], event["lat"])
        nonspatial = event["lon"] is None and event["attributes"].get("scope") in ("national", "global")
        if not inside and not (nonspatial and area["kind"] == "operating-area"):
            continue
        why = [f"type {event['type']}"]
        if plan.get("time"):
            why.append(f"time {event['start_time'][:10]} within window")
        if distance is not None:
            why.append(f"{distance / 1000:.1f} km from {area['name']}")
        elif area["kind"] == "polygon":
            why.append(f"inside {area['name']}")
        if min_magnitude:
            why.append(f"magnitude {event['attributes'].get('magnitude')} >= {min_magnitude['value']}")
        results.append({"kind": "event", "id": event["id"], "title": event["title"], "type": event["type"],
                        "category": event["category"], "time": event["start_time"], "lon": event["lon"], "lat": event["lat"],
                        "source_id": event["source_id"], "evidence_type": event["evidence_type"],
                        "distance_m": distance, "why": why})
    if area["kind"] == "circle":
        results.sort(key=lambda r: r["distance_m"] if r["distance_m"] is not None else 1e12)
    return results[:limit]


def search_entities(plan: dict, connection, limit: int = 200) -> list[dict]:
    """Entities of the requested types inside the area, nearest first."""
    area = search_area(plan)
    types = plan["target"]["types"]
    sql = f"SELECT * FROM entities WHERE type IN ({','.join('?' * len(types))})"
    params = list(types)
    box = _area_prefilter_bbox(area)
    if box:
        sql += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        params += [box[0], box[2], box[1], box[3]]
    results = []
    for row in connection.execute(sql, params):
        entity = db.row_to_dict(row, ["attributes"])
        inside, distance = _in_area(area, entity["lon"], entity["lat"])
        if not inside:
            continue
        why = [f"type {entity['type']}"]
        if distance is not None:
            why.append(f"{distance / 1000:.1f} km from {area['name']}")
        elif area["kind"] == "polygon":
            why.append(f"inside {area['name']}")
        results.append({"kind": "entity", "id": entity["id"], "title": entity["name"], "type": entity["type"],
                        "category": entity["category"], "lon": entity["lon"], "lat": entity["lat"],
                        "source_id": entity["source_id"], "evidence_type": "GIS-DERIVED", "distance_m": distance, "why": why})
    results.sort(key=lambda r: (r["distance_m"] if r["distance_m"] is not None else 0, r["title"]))
    return results[:limit]


def _persistent_water_tiles(connection, aoi_id: str) -> list[tuple[float, float]]:
    """
    Tiles classified as water in at least 75 % of their clear observations:
    our own imagery-derived water reference for 'near river' questions.
    """
    rows = connection.execute(
        """SELECT o.tile_id, SUM(o.land_class = 'water') AS water, COUNT(o.land_class) AS clear
           FROM tile_observations o JOIN tiles t ON t.id = o.tile_id
           WHERE t.aoi_id = ? GROUP BY o.tile_id""", (aoi_id,))
    tiles = {row["id"]: (row["lon"], row["lat"]) for row in connection.execute("SELECT id, lon, lat FROM tiles WHERE aoi_id = ?", (aoi_id,))}
    return [tiles[row["tile_id"]] for row in rows if row["clear"] and row["water"] / row["clear"] >= 0.75 and row["tile_id"] in tiles]


def search_changes(plan: dict, connection, limit: int = 100) -> list[dict]:
    """
    Accepted change candidates matching class/direction, area, time and
    (optionally) distance to persistent water. Related OSINT events
    (e.g. floods) near each change are attached as fusion context.
    """
    area = search_area(plan)
    change = plan.get("change") or {}
    sql = "SELECT * FROM change_candidates WHERE status = 'accepted'"
    params = []
    if plan.get("include_suppressed"):
        sql = "SELECT * FROM change_candidates WHERE 1 = 1"
    if change.get("class") and change["class"] != "any":
        sql += " AND change_class = ?"
        params.append(change["class"])
    if change.get("direction"):
        sql += " AND direction = ?"
        params.append(change["direction"])
    time_sql, time_params = _time_clause(plan, "earliest_supported_after")
    if time_sql:
        sql += " AND " + time_sql
        params += time_params

    water_cache = {}
    reference = plan.get("reference") or {}
    max_distance = (plan.get("distance") or {}).get("value_m")
    related = plan.get("related_events")
    results = []
    for row in connection.execute(sql + " ORDER BY score DESC", params):
        candidate = db.row_to_dict(row, ["gate_results", "tile_ids"])
        inside, distance = _in_area(area, candidate["lon"], candidate["lat"])
        if area["kind"] == "circle" and plan["distance"] and plan["distance"].get("assumed") and reference:
            inside = True  # an assumed radius around a reference feature is applied below, not here
        if not inside:
            continue
        why = [f"{candidate['change_class']} ({candidate['from_class']} -> {candidate['to_class']})",
               f"first seen {candidate['earliest_supported_after']}, last clear before {candidate['last_clean_before']}"]
        if reference.get("kind") in ("river", "lake"):
            if candidate["aoi_id"] not in water_cache:
                water_cache[candidate["aoi_id"]] = _persistent_water_tiles(connection, candidate["aoi_id"])
            water = water_cache[candidate["aoi_id"]]
            if not water:
                continue
            nearest = min(geometry.haversine_m(candidate["lon"], candidate["lat"], lon, lat) for lon, lat in water)
            if max_distance and nearest > max_distance:
                continue
            why.append(f"{nearest:.0f} m from persistent water (imagery-derived)")
        context = []
        if related:
            types = related["types"]
            for event in connection.execute(
                    f"SELECT id, title, type, start_time, lon, lat FROM events WHERE type IN ({','.join('?' * len(types))}) AND lon IS NOT NULL",
                    types):
                d = geometry.haversine_m(candidate["lon"], candidate["lat"], event["lon"], event["lat"])
                if d <= 50_000:
                    context.append({"id": event["id"], "title": event["title"], "time": event["start_time"], "distance_m": round(d)})
            why.append(f"{len(context)} related {'/'.join(types)} event(s) within 50 km" if context else
                       f"no staged {'/'.join(types)} events within 50 km")
        results.append({"kind": "change", "id": candidate["id"], "title": f"{candidate['change_class']} - {candidate['aoi_id']}",
                        "type": candidate["change_class"], "time": candidate["earliest_supported_after"],
                        "lon": candidate["lon"], "lat": candidate["lat"], "score": candidate["score"],
                        "score_kind": candidate["score_kind"], "status": candidate["status"],
                        "review_state": candidate["review_state"], "area_m2": candidate["area_m2"],
                        "evidence_type": "OBSERVED", "why": why, "related_events": context})
    if related and any(r["related_events"] for r in results):
        results.sort(key=lambda r: (-len(r["related_events"]), -(r["score"] or 0)))
    return results[:limit]


def search_imagery(plan: dict, limit: int = 30) -> dict:
    """
    Semantic image search (RemoteCLIP) as query results. Each result keeps
    its scene id, acquisition date, chip footprint and provenance; the score
    is model similarity, labelled as such.
    """
    from ..imagery import semantic

    time_filter = plan.get("time") or {}
    found = semantic.search(plan["semantic_text"], limit=limit, start=time_filter.get("start"), end=time_filter.get("end"))
    results = []
    for item in found.pop("results"):
        results.append({
            **item, "kind": "chip", "title": f"{item['acquired_at'][:10]} · {item['chip_km']:.2f} km chip · {item['scene_id']}",
            "type": "image-chip", "time": item["acquired_at"], "evidence_type": "MODEL SIMILARITY",
            "why": [f"rank {item['rank']} of {found['chips_searched']} chips", f"cosine similarity {item['score']:.3f} (not a probability)"],
        })
    return {**found, "results": results}


def similar_image_example(plan: dict) -> str | None:
    """
    The indexed image chip under the analyst's selected location, when
    "similar to this location" can be answered by RemoteCLIP image
    similarity (India scope, model staged and index READY/PARTIAL).
    Otherwise None, and the request is handled exactly as before.
    """
    from ..imagery import semantic

    place = plan.get("place") or {}
    if place.get("kind") != "map-selection" or place.get("lon") is None:
        return None
    if (plan.get("study_area") or {"id": "india"})["id"] != "india":
        return None
    if semantic.model_problems() or semantic.status()["state"] not in ("READY", "PARTIAL"):
        return None
    chips = semantic.chips_at(place["lon"], place["lat"])
    return chips[0]["id"] if chips else None


def search_similar_images(plan: dict, limit: int = 30) -> dict:
    """Image-to-image similarity (RemoteCLIP) for the chip at the selected location, as query results."""
    from ..imagery import semantic

    example = similar_image_example(plan)
    time_filter = plan.get("time") or {}
    found = semantic.similar([example], scope="other-places", limit=limit,
                             start=time_filter.get("start"), end=time_filter.get("end"))
    results = [{
        **item, "title": f"{item['acquired_at'][:10]} · {item['chip_km']:.2f} km chip · {item['scene_id']}",
        "type": "image-chip", "time": item["acquired_at"], "evidence_type": "MODEL SIMILARITY", "query": f"image like {example}",
        "why": [f"rank {item['rank']} of {found['chips_searched']} chips", f"image cosine similarity {item['score']:.3f} (not a probability)"],
    } for item in found["results"]]
    return {
        "query": f"image like chip {example}", "model": found["model"], "score_kind": found["score_kind"],
        "chips_searched": found["chips_searched"], "scenes_searched": len({r["scene_id"] for r in results}),
        "score_distribution": found["score_distribution"], "examples": found["examples"],
        "timing_ms": {"model_load": 0.0, "text_encode": 0.0, "rank": found["timing_ms"]["total"], "total": found["timing_ms"]["total"]},
        "note": found["note"], "results": results,
    }


def run(plan: dict) -> dict:
    """Run a plan. Always returns the plan, the capability report and results."""
    connection = db.connect()
    report = capability(plan, connection)
    results = []
    semantic_run = None
    if report["supported"]:
        intent = plan["intent"]
        if intent == "find-events":
            results = search_events(plan, connection)
        elif intent == "find-entities":
            results = search_entities(plan, connection)
        elif intent == "find-changes":
            results = search_changes(plan, connection)
        elif intent == "find-similar" and similar_image_example(plan):
            semantic_run = search_similar_images(plan)
            results = semantic_run.pop("results")
        elif intent == "search-imagery":
            semantic_run = search_imagery(plan)
            results = semantic_run.pop("results")
        elif intent == "locate":
            results = [{"kind": "place", "id": plan["place"]["name"], "title": plan["place"]["name"], "type": plan["place"]["kind"],
                        "lon": plan["place"]["lon"], "lat": plan["place"]["lat"], "bbox": plan["place"].get("bbox"),
                        "evidence_type": "GIS-DERIVED", "why": ["gazetteer match"]}]
    connection.close()
    area = search_area(plan) if plan.get("intent") not in (None, "unknown") else {"kind": "none"}
    area.pop("geometry", None)
    response = {"plan": plan, "capability": report, "area": area, "results": results, "count": len(results),
                "mode": settings.operating_mode()}
    if semantic_run is not None:
        response["semantic"] = semantic_run  # model version, timing, chips searched, score distribution
    return response
