"""Change engine logic on SYNTHETIC tile sequences (no imagery needed)."""

from lumon.change import engine


def look(date, land_class, water=0.0, vegetation=0.0, open_share=0.0, red=0.10, blue=0.06, scene="S2A_x"):
    """One TEST FIXTURE tile-look."""
    return {"date": date, "scene_id": f"{scene}_{date}", "land_class": land_class, "valid_fraction": 1.0 if land_class else 0.3,
            "fractions": {"water": water, "vegetation": vegetation, "open": open_share} if land_class else {},
            "features": {"B04_mean": red, "B02_mean": blue}}


def yearly(classes_by_year, satellites=("S2A", "S2B")):
    """TEST FIXTURE: two dry-season looks (Feb, Apr) per year with the given class."""
    items = []
    for year, land_class in classes_by_year.items():
        shares = {"vegetation": dict(vegetation=0.9, open_share=0.1), "open": dict(open_share=0.9, vegetation=0.1),
                  "water": dict(water=0.9, open_share=0.1)}[land_class]
        image_class = {"open": "bare"}.get(land_class, land_class)
        for month, satellite in zip(("02", "04"), satellites):
            items.append(look(f"{year}-{month}-10", image_class, scene=satellite, **shares))
    return items


def test_composites_ignore_monsoon_and_need_two_looks():
    sequence = yearly({2020: "vegetation"}) + [look("2020-09-10", "bare", open_share=1.0), look("2021-02-10", "vegetation", vegetation=1.0)]
    composites = engine.build_composites(sequence)
    assert composites[2020]["land_class"] == "vegetation"  # September (monsoon) not used
    assert composites[2021]["land_class"] is None  # only one clear look


def test_cloudy_looks_are_counted_not_used():
    sequence = yearly({2020: "vegetation"}) + [look("2020-03-10", None)]
    composite = engine.build_composites(sequence)[2020]
    assert composite["looks"] == 2 and composite["masked"] == 1


def test_persistent_transition_and_honest_window():
    sequence = yearly({2019: "vegetation", 2020: "vegetation", 2021: "open", 2022: "open"})
    composites = engine.build_composites(sequence)
    engine.split_open_land({"t": composites}, {2021: 1.6, 2022: 1.6})
    found = engine.find_transition(composites)
    assert found["from_class"] == "vegetation" and found["to_class"] == "soil"
    assert engine.refine_dates(sequence, found) == ("2020-04-10", "2021-02-10")


def test_one_year_flip_is_not_a_change():
    composites = engine.build_composites(yearly({2019: "vegetation", 2020: "vegetation", 2021: "open", 2022: "vegetation", 2023: "vegetation"}))
    engine.split_open_land({"t": composites}, {})
    assert engine.find_transition(composites) is None


def test_open_land_split_is_relative_to_same_year():
    composites = {"t": {2020: {"land_class": "open", "red_blue": 1.30}, 2021: {"land_class": "open", "red_blue": 1.30}}}
    engine.split_open_land(composites, {2020: 1.60, 2021: 1.35})
    assert composites["t"][2020]["land_class"] == "built"  # much greyer than typical that year
    assert composites["t"][2021]["land_class"] == "soil"  # typical colour that year


def _group(classes, satellites=("S2A", "S2B"), copies=2):
    composites = engine.build_composites(yearly(classes, satellites))
    engine.split_open_land({"t": composites}, {})
    return [engine.find_transition(composites)] * copies


def test_gates_pass_for_clear_persistent_change():
    gates = {g["gate"]: g["passed"] for g in engine.run_gates(_group({2019: "vegetation", 2020: "vegetation", 2021: "water", 2022: "water"}), False)}
    assert all(gates.values())


def test_single_satellite_fails_radiometric_gate():
    group = _group({2019: "vegetation", 2020: "vegetation", 2021: "water", 2022: "water"}, satellites=("S2A", "S2A"))
    gates = {g["gate"]: g["passed"] for g in engine.run_gates(group, False)}
    assert gates["3 radiometric"] is False


def test_single_tile_fails_size_gate():
    group = _group({2019: "vegetation", 2020: "vegetation", 2021: "water", 2022: "water"}, copies=1)
    gates = {g["gate"]: g["passed"] for g in engine.run_gates(group, False)}
    assert gates["5 size/shape"] is False


def test_classification_and_elongation():
    assert engine.classify("water", "soil", False) == ("water-extent", "contraction")
    assert engine.classify("soil", "water", False) == ("water-extent", "expansion")
    assert engine.classify("soil", "built", True)[0] == "road-development"
    assert engine.classify("vegetation", "soil", False) == ("clearance", "disappearance")
    assert engine.is_elongated([f"a:0:{c}" for c in range(6)])
    assert not engine.is_elongated(["a:0:0", "a:0:1", "a:1:0", "a:1:1"])


def test_score_is_bounded():
    score = engine.uncalibrated_score(_group({2019: "vegetation", 2020: "vegetation", 2021: "water", 2022: "water", 2023: "water"}))
    assert 0 <= score <= 1
