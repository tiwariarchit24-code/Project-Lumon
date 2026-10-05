"""Source normalisation with small SYNTHETIC TEST FIXTURES shaped like the real APIs."""

import json
import math
from datetime import datetime, timezone

from lumon.sources import gdacs_alerts, ioda_outages, noaa_swpc_kp, usgs_earthquakes
from lumon.sources.common import iso_from_text

CONTEXT = {"bbox": [60, -10, 100, 40], "now": datetime(2026, 10, 4, tzinfo=timezone.utc), "places": [], "key": None}


def test_usgs_reviewed_vs_automatic():
    fixture = {"features": [  # TEST FIXTURE
        {"id": "t1", "geometry": {"coordinates": [75.0, 15.0, 10.0]},
         "properties": {"mag": 4.2, "place": "synthetic place", "time": 1790000000000, "status": "reviewed", "magType": "mb"}},
        {"id": "t2", "geometry": {"coordinates": [76.0, 16.0, 5.0]},
         "properties": {"mag": 2.6, "place": None, "time": 1790000100000, "status": "automatic", "magType": "ml"}},
    ]}
    records = usgs_earthquakes.normalize(json.dumps(fixture).encode(), CONTEXT)
    assert [r["evidence_type"] for r in records] == ["VERIFIED RECORD", "OBSERVED"]
    assert records[0]["start_time"] == "2026-09-21T14:13:20Z"
    assert records[0]["attributes"]["depth_km"] == 10.0
    assert "location text not given" in records[1]["title"]


def test_gdacs_alert_is_inferred_and_utc():
    fixture = {"features": [{  # TEST FIXTURE
        "geometry": {"type": "Point", "coordinates": [75, 15]},
        "properties": {"eventtype": "FL", "eventid": 1, "episodeid": 2, "name": "Synthetic flood", "alertlevel": "Green",
                       "fromdate": "2026-10-01T06:00:00", "todate": "2026-10-03T06:00:00", "iscurrent": "true", "url": {}},
    }]}
    record = gdacs_alerts.normalize(json.dumps(fixture).encode(), CONTEXT)[0]
    assert record["type"] == "flood" and record["evidence_type"] == "INFERRED"
    assert record["start_time"] == "2026-10-01T06:00:00Z"


def test_ioda_events_are_national_scope():
    fixture = {"data": [{"datasource": "bgp", "start": 1790000000, "duration": 600, "method": "x", "score": 1.0}]}  # TEST FIXTURE
    record = ioda_outages.normalize(json.dumps(fixture).encode(), CONTEXT)[0]
    assert record["scope"] == "national" and record["geometry"] is None
    assert record["end_time"] == "2026-09-21T14:23:20Z"


def test_swpc_supports_both_table_and_object_formats():
    table = [["time_tag", "Kp"], ["2026-10-01 00:00:00.000", "5.33"]]  # TEST FIXTURE (old format)
    objects = [{"time_tag": "2026-10-01T00:00:00", "Kp": 2.0}]  # TEST FIXTURE (new format)
    old = noaa_swpc_kp.normalize(json.dumps(table).encode(), CONTEXT)[0]
    new = noaa_swpc_kp.normalize(json.dumps(objects).encode(), CONTEXT)[0]
    assert old["attributes"]["kp"] == 5.33 and "storm" in old["title"]
    assert new["scope"] == "global"


def test_time_without_zone_is_refused_when_not_documented_utc():
    assert iso_from_text("2026-10-01T06:00:00", assume_utc=False) is None
    assert iso_from_text("2026-10-01T06:00:00+05:30") == "2026-10-01T00:30:00Z"


def test_satellite_frame_conversion():
    """TEME -> geodetic: a point above the north pole, and a point on the equator."""
    from lumon.sources import celestrak_satellites as c
    jd = 2461317.5  # an arbitrary date (2026-10-04 00:00 UT)
    lon, lat, altitude = c.teme_to_geodetic((0.0, 0.0, 6356.7523 + 400.0), jd)  # polar radius + 400 km
    assert abs(lat - 90) < 1e-6 and abs(altitude - 400) < 0.01
    # A point on the TEME x-axis appears at longitude = -GMST (Earth has rotated under it).
    theta = c.gmst_radians(jd)
    lon, lat, altitude = c.teme_to_geodetic((6378.137 + 500.0, 0.0, 0.0), jd)
    expected = math.degrees(-theta)
    expected = (expected + 180) % 360 - 180
    assert abs(lon - expected) < 1e-6 and abs(lat) < 1e-9 and abs(altitude - 500) < 1e-6
