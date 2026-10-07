"""Training-load math: HR-based TRIMP proxy, Coggan CTL/ATL/TSB, and ACWR.

See ../references/calculations.md for the full rationale and formulas.
"""
import math
from datetime import date, timedelta

CTL_DAYS = 42
ATL_DAYS = 7
ACWR_ACUTE_DAYS = 7
ACWR_CHRONIC_DAYS = 28

# Banister TRIMP, female coefficients (Morton et al.).
TRIMP_FEMALE_A = 0.86
TRIMP_FEMALE_B = 1.67


def trimp(duration_min: float, avg_hr: float, hr_rest: float, hr_max: float) -> float:
    """HR-reserve-based training impulse, used as a cross-activity-type load proxy."""
    if not all([duration_min, avg_hr, hr_rest, hr_max]) or hr_max <= hr_rest:
        return 0.0
    hrr = (avg_hr - hr_rest) / (hr_max - hr_rest)
    hrr = max(0.0, min(1.0, hrr))
    return duration_min * hrr * TRIMP_FEMALE_A * math.exp(TRIMP_FEMALE_B * hrr)


def ewma_series(daily_values: dict, time_constant_days: int, start: date, end: date) -> dict:
    """Coggan-style exponentially weighted moving average, seeded from the earliest
    available data point (not from zero) so early dates in the *requested* window
    aren't artificially suppressed. Always pass a `daily_values` dict that already
    includes several months of lookback before `start` -- see references/calculations.md
    for the bug this guards against (a wrong 16.0 instead of ~19.6 from under-seeding).
    """
    if not daily_values:
        return {}
    lambda_ = 1 - math.exp(-1 / time_constant_days)
    first_day = min(daily_values.keys())
    result = {}
    running = daily_values.get(first_day, 0.0)
    d = first_day
    while d <= end:
        today_value = daily_values.get(d, 0.0)
        running = running + (today_value - running) * lambda_
        if d >= start:
            result[d] = running
        d += timedelta(days=1)
    return result


def rolling_avg(daily_values: dict, window_days: int, on: date) -> float:
    total = 0.0
    for i in range(window_days):
        total += daily_values.get(on - timedelta(days=i), 0.0)
    return total / window_days


def rolling_avg_ignore_missing(daily_values: dict, window_days: int, on: date):
    """Like rolling_avg, but averages only over days that actually have a value in
    the window instead of treating missing days as zero -- correct for a metric like
    HRV where "no reading" must not drag the average down. Returns None if the
    window has no data at all (e.g. before HRV syncing started)."""
    vals = []
    for i in range(window_days):
        v = daily_values.get(on - timedelta(days=i))
        if v is not None:
            vals.append(v)
    return (sum(vals) / len(vals)) if vals else None


def acwr_status(ratio: float) -> str:
    if ratio < 0.8:
        return "Low"
    if ratio <= 1.3:
        return "Medium"
    return "High"


UNHEALTHY_READINESS_LEVELS = {"LOW", "POOR"}
UNHEALTHY_READINESS_STREAK_DAYS = 3  # "more than two days in a row"


def readiness_streak(levels_most_recent_first: list) -> int:
    """Count consecutive most-recent days with an unhealthy (LOW/POOR) readiness
    level. `levels_most_recent_first` should already be ordered newest date first
    and stop at the first gap/None so a missing sync day doesn't bridge a streak."""
    streak = 0
    for level in levels_most_recent_first:
        if level in UNHEALTHY_READINESS_LEVELS:
            streak += 1
        else:
            break
    return streak


def compute_daily_metrics(daily_tss: dict, daily_trimp: dict, start: date, end: date) -> dict:
    """Returns {date: {ctl, atl, tsb, acute_load_7d, chronic_load_28d, acwr, acwr_status}}."""
    ctl_series = ewma_series(daily_tss, CTL_DAYS, start, end)
    atl_series = ewma_series(daily_tss, ATL_DAYS, start, end)

    out = {}
    d = start
    while d <= end:
        ctl = ctl_series.get(d, 0.0)
        atl = atl_series.get(d, 0.0)
        acute = rolling_avg(daily_trimp, ACWR_ACUTE_DAYS, d)
        chronic = rolling_avg(daily_trimp, ACWR_CHRONIC_DAYS, d)
        ratio = (acute / chronic) if chronic > 0 else 0.0
        out[d] = {
            "ctl": round(ctl, 2),
            "atl": round(atl, 2),
            "tsb": round(ctl - atl, 2),
            "acute_load_7d": round(acute, 2),
            "chronic_load_28d": round(chronic, 2),
            "acwr": round(ratio, 3),
            "acwr_status": acwr_status(ratio) if chronic > 0 else None,
        }
        d += timedelta(days=1)
    return out
