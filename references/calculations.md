# Training load calculations

Full detail behind the numbers in `scripts/calculations.py`. Read this when the user
asks *why* a number looks the way it does, or if a metric looks off and needs debugging.

## TSS and Normalized Power: sourced from Garmin, not recomputed

Garmin Connect activity records already include `normPower` and `trainingStressScore`
per ride (it auto-detects FTP internally — `ftpAutoDetected: true` on the user profile,
though the raw FTP watts value isn't exposed through the API we found). Rather than
re-deriving NP/TSS from raw power streams with a second, possibly-diverging FTP
estimate, `garmin_sync.py` stores Garmin's own values as-is. This is simpler and avoids
a source of disagreement between "our number" and "the number on your head unit."

## CTL / ATL / TSB — Coggan Performance Management Chart

Standard exponentially-weighted moving average of daily TSS:

- **CTL** (Chronic Training Load / "fitness"): 42-day time constant
- **ATL** (Acute Training Load / "fatigue"): 7-day time constant
- **TSB** (Training Stress Balance / "form"): CTL − ATL

```
lambda = 1 - e^(-1 / time_constant_days)
today's_value = yesterday's_value + (today's_TSS - yesterday's_value) * lambda
```

**Seeding matters.** The EWMA must be seeded from the *earliest available* daily TSS
value, not from zero at the start of whatever window you happen to be displaying. Starting
the recursion too close to the target date suppresses CTL artificially — this produced a
wrong 16.0 instead of the correct ~19.6 for the same day once before. `ewma_series()` in
`calculations.py` always seeds from `min(daily_values.keys())`, and `garmin_sync.py`
requests a 400-day backfill on first run specifically so there's enough history to seed
from. If CTL looks suspiciously low, check whether the daily_tss dict passed in actually
has months of lookback, not just the display window.

## ACWR — Acute:Chronic Workload Ratio

This is a different calculation from CTL/ATL on purpose: it's a **true rolling average**,
not an EWMA ratio.

```
acute_load_7d   = simple rolling 7-day average of daily load
chronic_load_28d = simple rolling 28-day average of daily load
ACWR = acute_load_7d / chronic_load_28d
```

Using ATL/CTL (the EWMA versions) as a stand-in for this ratio runs structurally too high
and over-flags days as "High" — don't do that, even though it's tempting since ATL/CTL
are already computed. This inconsistency broke progress tracking once before, so the
rolling-average version is the only one that should ever be called "ACWR."

**Status thresholds:**
- `< 0.8` → Low
- `0.8 – 1.3` → Medium (the target range)
- `> 1.3` → High (elevated injury risk — worth a mention in coaching notes)

### What feeds the ACWR "load" number

ACWR needs a load metric that's comparable across *all* activity types — cycling,
running, lifting — not just power-based cycling TSS. The original methodology (from a
prior conversation) called for Strava's Relative Effort score as that cross-activity
proxy. This skill deliberately avoids that Strava dependency and instead computes an
HR-based proxy directly from Garmin data: the **Banister TRIMP** (Training Impulse),
using sex-specific coefficients (Morton et al.) selected by the `TRIMP_SEX` env var
(`female` or `male` — see `.env.example`; defaults to `female`):

```
HRR = (avg_HR - resting_HR) / (HR_max - resting_HR)     # heart rate reserve, clamped to [0,1]

# TRIMP_SEX=female (default)
TRIMP = duration_minutes * HRR * 0.86 * e^(1.67 * HRR)

# TRIMP_SEX=male
TRIMP = duration_minutes * HRR * 0.64 * e^(1.92 * HRR)
```

The two coefficient pairs aren't interchangeable — using the wrong one systematically
skews TRIMP, and since TRIMP feeds ACWR directly, that skews the Low/Medium/High
injury-risk read too. Set `TRIMP_SEX` correctly for whoever the data belongs to; don't
leave it on the default without checking.

- `resting_HR` is pulled per-activity-date from Garmin's daily wellness data
  (`get_rhr_day`).
- `HR_max` isn't available as a direct profile field either, so it's estimated as the
  highest `maxHR` observed across all synced activities (a practical fitness-test-free
  proxy — updates naturally as new highs occur). If this drifts oddly, sanity-check it
  against `SELECT MAX(max_hr) FROM activities`.
- This TRIMP number, not TSS, is what feeds `acute_load_7d` / `chronic_load_28d` / ACWR.
  TSS/NP (Garmin's own) only feeds CTL/ATL.

## E-bike rule

E-bike rides (`activity_type` in `EBIKE_TYPE_KEYS` in `garmin_sync.py`) are **included**
in weekly load/fatigue totals and TRIMP/ACWR — the body still does real cardiovascular
and muscular work. They're flagged with `is_ebike = 1` so they can be **excluded** from
pace/performance comparisons (average speed, power-per-effort trends, etc.), where mixing
e-bike and analog rides would be misleading.

## Training readiness

Pulled from Garmin's own `get_training_readiness(date)` (not something we compute) —
score 0-100 plus a level string (`POOR` / `LOW` / `MODERATE` / `HIGH` / etc.). Garmin
returns multiple readings per day as the watch updates (wake-up, post-exercise reset,
manual refresh); `[0]` is the most recent, which is what's stored as that day's value
in `daily_metrics.training_readiness_score`/`training_readiness_level` — matches what
the Garmin app itself would show as "current."

`garmin_sync.py`'s `sync_readiness()` re-fetches the last `READINESS_LOOKBACK_DAYS`
(7) days each run: today is always refreshed (it changes through the day), past days
only if they don't have a reading yet, since a closed day's value doesn't change.

**Unhealthy-trend flag**: `readiness_streak()` in `calculations.py` counts consecutive
most-recent days at `LOW` or `POOR`, walking backward from today and stopping at the
first day that isn't (or has no reading, e.g. a missed sync day — a gap doesn't bridge
a streak). Flagged as `unhealthy_trend: true` once that streak exceeds 2 days — added
2026-09-05 after two nights of poor sleep visibly showed up as a same-day readiness
crash (41 -> 29 -> 11) alongside a hard, high-vert ride; the user wanted more than a
single bad day to trigger a flag, since one rough night is normal, but a stretch of
several isn't. Surfaced in `garmin_sync.py`'s and `coach.py dashboard`'s output as
`readiness_trend`.

## HRV (heart rate variability)

Pulled from Garmin's `get_hrv_data(date)` — stores `hrvSummary.lastNightAvg` (ms) and
`hrvSummary.status` (e.g. BALANCED/UNBALANCED/LOW) as that day's value in
`daily_metrics.hrv_last_night_avg`/`hrv_status`. Garmin also returns its own
`weeklyAvg` and a `baseline` band, but we don't store those — the user specifically
wants a **rolling 30-day average**, computed ourselves, not Garmin's 7-day one.

`garmin_sync.py`'s `sync_hrv()` does a one-time deep backfill (`HRV_LOOKBACK_DAYS` =
120) so the 30-day rolling average has real history from the start; after that it
only re-fetches today (the overnight value is finalized once and doesn't change
retroactively) or any day still missing.

The 30-day average itself is **not stored** — `coach.py`'s `attach_hrv_rolling_avg()`
computes it at read time via `rolling_avg_ignore_missing()` in `calculations.py`,
which skips days with no reading instead of treating them as zero (unlike
`rolling_avg`, used for TRIMP/ACWR, where a missing day legitimately means zero
training load — a missing HRV reading means no data, not zero HRV). Added
2026-09-14 for the weekly review's HRV chart: daily HRV plotted against this rolling
30-day baseline, single axis (same units, no dual-axis exception needed here unlike
the CTL/ATL/ACWR chart).

## Units

The user works in imperial units (miles, feet, lbs) based on how they log lifting and
rides. Garmin's API returns metric (meters). Convert distance (× 0.000621371 for miles)
and elevation (× 3.28084 for feet) when presenting to the user, but keep the database
in the raw metric units Garmin provides — convert at display time, not storage time.
