"""Calibration of the weighted overall score onto the published index scale.

The weighted category mean ("raw" score) is an absolute measure: it moves only
when a repository's own evidence moves. Published on its own, it uses the
1..100 range badly — half the record landed between 50 and 69, the top decile
of open source sat in the high 70s, and a 96 was unreachable in practice
(the strictest checked criteria are not all simultaneously satisfiable for
every project shape).

The published health index therefore applies a fixed calibration curve to the
raw score, anchored to the empirical distribution of the public record
(47,516 inspected repositories, snapshot of 2026-08-02). Anchors place the
seven band floors at chosen percentiles of that snapshot, so a band states
where a repository stands within inspected open source:

    raw   0  26  40  50  60  68  79  90  100
    index 0  20  35  50  65  80  93  99  100

Between anchors the curve is a monotone cubic (Fritsch–Carlson PCHIP),
evaluated once at every integer raw score and frozen below as a lookup table —
the table, not the spline, is the specification. Raw 91..100 saturate at 100:
the top of the scale certifies "essentially all checked criteria", and the
last few raw points above 90 distinguish nothing a reader should act on.
Category scores are NOT calibrated; the curve describes (and is fitted to)
the overall index only.

The calibration is deliberately a constant of the metrics version, not a live
percentile: scores must not move because other repositories were inspected.
Recalibrating against a newer snapshot is a METRICS_VERSION bump like any
other scoring change.
"""

from __future__ import annotations

# Snapshot the anchors were fitted against — kept for the report's inputs.
CALIBRATION = "2026-08-02"

# index_for_raw[raw] for raw 0..100. Monotone; f(90)=99, f(91..100)=100.
_CALIBRATION_TABLE: tuple[int, ...] = (
    0, 1, 2, 2, 3, 4, 4, 5, 6, 7,
    7, 8, 9, 10, 10, 11, 12, 13, 13, 14,
    15, 16, 17, 17, 18, 19, 20, 21, 22, 23,
    24, 25, 26, 27, 28, 29, 30, 31, 33, 34,
    35, 36, 38, 39, 41, 42, 44, 45, 47, 48,
    50, 51, 53, 54, 56, 57, 59, 60, 62, 63,
    65, 67, 69, 71, 73, 75, 77, 78, 80, 81,
    83, 84, 86, 87, 88, 89, 90, 91, 92, 93,
    94, 94, 95, 96, 96, 97, 98, 98, 98, 99,
    99, 100, 100, 100, 100, 100, 100, 100, 100, 100,
    100,
)

assert len(_CALIBRATION_TABLE) == 101


def calibrate(raw: int) -> int:
    """Published health index for a raw weighted overall score (both 0..100)."""
    return _CALIBRATION_TABLE[max(0, min(100, raw))]
