"""RF04-09: огромные числа из IPC/истории не роняют дедупликацию OverflowError."""

import math

import pytest

from backend import auto_deduplication as ad


@pytest.mark.parametrize(
    "value", [10**1000, -(10**1000), 10**400, "1" + "0" * 400], ids=["huge", "huge-neg", "400-digits", "400-digit-str"]
)
def test_threshold_that_overflows_float_falls_back_to_default(value):
    assert ad._sanitize_threshold(value) == ad.DEFAULT_DEDUP_THRESHOLD


@pytest.mark.parametrize("value", [float("inf"), float("nan"), "abc", None, 1e400])
def test_threshold_non_finite_or_garbage_still_falls_back(value):
    assert ad._sanitize_threshold(value) == ad.DEFAULT_DEDUP_THRESHOLD


@pytest.mark.parametrize("value", [0.5, 0.85, 0.9, 1.0])
def test_threshold_valid_values_are_kept(value):
    assert ad._sanitize_threshold(value) == value


@pytest.mark.parametrize("value", [0.0, 0.3, 0.49, -0.5, 1.5])
def test_threshold_unsafe_values_fall_back_to_default(value):
    """G3-MEM-03 (R02): порог ниже безопасной границы (0.0 = «всё дубликат») или
    выше 1.0 не прижимается к границе, а заменяется default."""
    assert ad._sanitize_threshold(value) == ad.DEFAULT_DEDUP_THRESHOLD


@pytest.mark.parametrize("ts", [10**1000, -(10**1000)], ids=["huge", "huge-neg"])
def test_timestamp_sort_key_survives_oversized_int(ts):
    key = ad._parse_ts_for_sort({"ts": ts})
    assert key == 0.0 and math.isfinite(key)
