"""Synthetic numerical verification only; not fitted/observed ship performance."""

import math

import pytest

from tools.energy_math import cubic_power, integrate_power, segment_energy, split_groups


@pytest.mark.parametrize("speed,expected", [(0, 0), (2, 1), (4, 8), (6, 27)])
def test_cubic_synthetic_coefficient(speed, expected):
    assert cubic_power(speed, .125) == expected


@pytest.mark.parametrize("bad", [None, True, "2", -1, math.inf, math.nan, 10 ** 1000])
def test_cubic_rejects_bad_speed(bad):
    with pytest.raises(ValueError):
        cubic_power(bad, .125)


@pytest.mark.parametrize("bad", [None, False, "2", -1, math.inf, math.nan])
def test_cubic_rejects_bad_coefficient(bad):
    with pytest.raises(ValueError):
        cubic_power(2, bad)


def test_cubic_overflow_rejected():
    with pytest.raises(ValueError):
        cubic_power(1e200, 1)


def test_waiting_auxiliary_once_and_average_same_scope():
    propulsion = segment_energy(8, 4, .5, .125, 2, scope="propulsion")
    total = segment_energy(8, 4, .5, .125, 2, scope="total")
    assert propulsion["duration_h"] == total["duration_h"] == 2.5
    assert propulsion["energy_kwh"] == 16
    assert total["auxiliary_kwh"] == 5 and total["energy_kwh"] == 21
    assert total["energy_kwh"] == total["power_kw"] * total["duration_h"]
    assert propulsion["peak_power_kw"] == 8
    assert total["peak_power_kw"] == 10


def test_zero_distance_waiting_is_auxiliary_only():
    result = segment_energy(0, 0, 1, .125, 2, scope="total")
    assert result["energy_kwh"] == 2
    assert segment_energy(0, 0, 0, .125, 2, scope="total")["energy_kwh"] == 0


@pytest.mark.parametrize("args", [(8, 0, .5, .125, 2), (8, 4, -1, .125, 2),
                                   (8, 4, .5, .125, None), (8, 4, .5, .125, -1)])
def test_segment_invalid_inputs(args):
    with pytest.raises(ValueError):
        segment_energy(*args, scope="total")


def test_explicit_scope_required():
    with pytest.raises(ValueError):
        segment_energy(8, 4, .5, .125, 2, scope="unknown")


def test_kwh_integral_constant_power():
    result = integrate_power([(0, 12), (1800, 12), (3600, 12)], max_gap_seconds=1800)
    assert result["positive_area_kwh"] == result["signed_area_kwh"] == 12
    assert result["negative_area_kwh"] == 0 and result["used_intervals"] == 2


@pytest.mark.parametrize("powers,pos,neg", [((10, -10), 2.5, 2.5),
    ((10, -20), 5/3, 20/3), ((-10, 20), 20/3, 5/3), ((-10, -20), 0, 15)])
def test_linear_zero_crossing_areas(powers, pos, neg):
    result = integrate_power([(0, powers[0]), (3600, powers[1])], max_gap_seconds=3600)
    assert result["positive_area_kwh"] == pytest.approx(pos)
    assert result["negative_area_kwh"] == pytest.approx(neg)
    assert result["signed_area_kwh"] == pytest.approx(pos - neg)


def test_missing_break_and_large_gap_do_not_bridge():
    result = integrate_power([(0, 10), (5, None), (10, 10), (15, 10), (1000, 10),
                              (1005, 10)], max_gap_seconds=5)
    assert result["used_intervals"] == 2 and result["skipped_gaps"] == 1
    assert result["missing_breaks"] == 1 and result["covered_seconds"] == 10
    assert result["signed_area_kwh"] == pytest.approx(10 * 10 / 3600)


@pytest.mark.parametrize("samples", [[(0, 1), (0, 2)], [(5, 1), (0, 2)],
    [(0, 1), (None, None), (0, 2)], [(0, 1), (5, None), (4, 2)],
    [(0, 1), (5, math.inf)], [(True, 1)], [(None, math.inf)]])
def test_ambiguous_invalid_samples_refused(samples):
    with pytest.raises(ValueError):
        integrate_power(samples, max_gap_seconds=10)


@pytest.mark.parametrize("gap", [0, -1, None, True, math.inf])
def test_invalid_gap_refused(gap):
    with pytest.raises(ValueError):
        integrate_power([(0, 1), (5, 1)], max_gap_seconds=gap)


def test_group_isolation_preserves_entire_group_and_order():
    records = [{"group_id": group, "id": i} for i, group in enumerate(["a", "b", "a", "b"])]
    train, validation = split_groups(records, train_groups=["a"], validation_groups=["b"])
    assert [r["id"] for r in train] == [0, 2] and [r["id"] for r in validation] == [1, 3]


@pytest.mark.parametrize("train,validation", [(["a"], ["a"]), ([], ["b"]),
    (["a"], []), (["a"], ["c"]), (["a", "c"], ["b"])])
def test_group_leak_or_unassigned_groups_refused(train, validation):
    with pytest.raises(ValueError):
        split_groups([{"group_id": "a"}, {"group_id": "b"}], train_groups=train, validation_groups=validation)
