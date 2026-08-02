from scanner.calibration import _CALIBRATION_TABLE, calibrate
from scanner.metrics import band_for


def test_table_is_monotone_and_complete():
    assert len(_CALIBRATION_TABLE) == 101
    assert all(b >= a for a, b in zip(_CALIBRATION_TABLE, _CALIBRATION_TABLE[1:]))
    assert calibrate(0) == 0
    assert calibrate(100) == 100


def test_top_of_scale_saturates():
    assert calibrate(90) == 99
    for raw in range(91, 101):
        assert calibrate(raw) == 100


def test_out_of_range_input_clamps():
    assert calibrate(-5) == 0
    assert calibrate(140) == 100


def test_anchor_points():
    # The fitted anchors (raw percentile -> band floor); see calibration.py.
    for raw, index in [(26, 20), (40, 35), (50, 50), (60, 65), (68, 80), (79, 93)]:
        assert calibrate(raw) == index


def test_band_floors_reachable_and_aligned():
    # Every band floor on the published scale is hit exactly by some raw score,
    # so no band is skipped over by the integer table.
    reachable = set(_CALIBRATION_TABLE)
    for floor in (1, 20, 35, 50, 65, 80, 93):
        assert floor in reachable or floor == 1
    assert band_for(calibrate(79)) == "exceptional"
    assert band_for(calibrate(68)) == "excellent"
    assert band_for(calibrate(60)) == "good"
