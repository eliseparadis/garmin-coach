"""Sync Garmin Connect activities into the local training database.

Usage:
    python garmin_sync.py                 # incremental sync since last run
    python garmin_sync.py --backfill-days 400   # force a longer lookback (first run)

Only calls the Garmin API for activities newer than the last sync (or the
requested backfill window), then recomputes CTL/ATL/ACWR locally from the
cached data -- no need to hit the API again just to look at trends.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import garminconnect

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection, init_db
from calculations import trimp, compute_daily_metrics, readiness_streak

READINESS_LOOKBACK_DAYS = 7  # enough history to detect a 3+ day unhealthy streak
HRV_LOOKBACK_DAYS = 120  # one-time deep backfill so a 30-day rolling avg has history; incremental after

EBIKE_TYPE_KEYS = {"cycling_e_bike", "road_biking_e_bike", "mountain_biking_e_bike", "gravel_cycling_e_bike"}

TRIMP_SEX = os.environ.get("TRIMP_SEX", "female").strip().lower()


def login():
    tokens_dir = os.environ["GARMIN_TOKENS_DIR"]
    client = garminconnect.Garmin()
    client.login(tokenstore=tokens_dir)
    return client


def get_current_hr_max(conn, fallback_activity_max):
    row = conn.execute("SELECT MAX(max_hr) AS m FROM activities").fetchone()
    known = row["m"] if row and row["m"] else 0
    return max(known, fallback_activity_max or 0)


def rhr_for_date(client, cache, d: date):
    key = d.isoformat()
    if key in cache:
        return cache[key]
    try:
        data = client.get_rhr_day(key)
        metrics = data.get("allMetrics", {}).get("metricsMap", {}).get("WELLNESS_RESTING_HEART_RATE", [])
        value = metrics[0]["value"] if metrics else None
    except Exception:
        value = None
    cache[key] = value
    return value


def sync_activities(client, conn, since: date):
    cache_rhr = {}
    offset = 0
    batch = 25
    new_count = 0
    hit_boundary = False

    while not hit_boundary:
        acts = client.get_activities(offset, batch)
        if not acts:
            break
        for a in acts:
            start_local = a.get("startTimeLocal")
            if not start_local:
                continue
            start_dt = datetime.strptime(start_local, "%Y-%m-%d %H:%M:%S")
            if start_dt.date() < since:
                hit_boundary = True
                break

            activity_id = a["activityId"]
            existing = conn.execute("SELECT 1 FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
            if existing:
                continue

            type_key = (a.get("activityType") or {}).get("typeKey", "")
            is_ebike = 1 if type_key in EBIKE_TYPE_KEYS else 0

            rhr = rhr_for_date(client, cache_rhr, start_dt.date())
            hr_max_est = get_current_hr_max(conn, a.get("maxHR"))
            duration_min = (a.get("duration") or 0) / 60.0
            trimp_load = trimp(duration_min, a.get("averageHR"), rhr, hr_max_est, sex=TRIMP_SEX)

            conn.execute(
                """INSERT INTO activities (
                    activity_id, activity_type, name, start_time_local, start_time_utc,
                    distance_m, duration_s, moving_duration_s, elevation_gain_m, elevation_loss_m,
                    avg_hr, max_hr, avg_power, norm_power, training_stress_score,
                    activity_training_load, trimp_load, is_ebike, resting_hr_that_day,
                    raw_json, synced_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    activity_id, type_key, a.get("activityName"), start_local, a.get("startTimeGMT"),
                    a.get("distance"), a.get("duration"), a.get("movingDuration"),
                    a.get("elevationGain"), a.get("elevationLoss"),
                    a.get("averageHR"), a.get("maxHR"), a.get("avgPower"), a.get("normPower"),
                    a.get("trainingStressScore"), a.get("activityTrainingLoad"), trimp_load,
                    is_ebike, rhr, json.dumps(a), datetime.now().isoformat(),
                ),
            )
            new_count += 1
        offset += batch
        if offset > 2000:  # sanity cap
            break

    conn.commit()
    return new_count


def readiness_for_date(client, d: date):
    """Latest training-readiness reading for the given day, or (None, None).
    Garmin returns a list of readings taken through the day (device wakes,
    post-exercise resets, etc.), most recent first -- [0] is what the app
    itself would show as "current" for that day."""
    try:
        data = client.get_training_readiness(d.isoformat())
        if not data:
            return None, None
        latest = data[0]
        return latest.get("score"), latest.get("level")
    except Exception:
        return None, None


def sync_readiness(client, conn):
    """Backfill/refresh training_readiness on daily_metrics for the last
    READINESS_LOOKBACK_DAYS. Only re-fetches a past day if it has no reading
    yet; today is always re-fetched since Garmin updates it through the day."""
    today = date.today()
    for i in range(READINESS_LOOKBACK_DAYS):
        d = today - timedelta(days=i)
        row = conn.execute(
            "SELECT training_readiness_score FROM daily_metrics WHERE date = ?", (d.isoformat(),)
        ).fetchone()
        if row is None:
            continue  # no daily_metrics row for this date yet (predates first synced activity)
        if d != today and row["training_readiness_score"] is not None:
            continue
        score, level = readiness_for_date(client, d)
        if score is None:
            continue
        conn.execute(
            "UPDATE daily_metrics SET training_readiness_score = ?, training_readiness_level = ? WHERE date = ?",
            (score, level, d.isoformat()),
        )
    conn.commit()


def hrv_for_date(client, d: date):
    """Overnight HRV summary for the given day, or (None, None)."""
    try:
        data = client.get_hrv_data(d.isoformat())
        summary = (data or {}).get("hrvSummary") or {}
        return summary.get("lastNightAvg"), summary.get("status")
    except Exception:
        return None, None


def sync_hrv(client, conn):
    """Backfill/refresh hrv_last_night_avg on daily_metrics. First run does a deep
    HRV_LOOKBACK_DAYS backfill (one API call per missing day); later runs only
    re-fetch today (Garmin finalizes the overnight value after wake-up, so a day
    already populated never changes) or any day still missing."""
    today = date.today()
    for i in range(HRV_LOOKBACK_DAYS):
        d = today - timedelta(days=i)
        row = conn.execute(
            "SELECT hrv_last_night_avg FROM daily_metrics WHERE date = ?", (d.isoformat(),)
        ).fetchone()
        if row is None:
            continue  # no daily_metrics row for this date yet
        if d != today and row["hrv_last_night_avg"] is not None:
            continue
        value, status = hrv_for_date(client, d)
        if value is None:
            continue
        conn.execute(
            "UPDATE daily_metrics SET hrv_last_night_avg = ?, hrv_status = ? WHERE date = ?",
            (value, status, d.isoformat()),
        )
    conn.commit()


def readiness_trend(conn):
    rows = conn.execute(
        "SELECT training_readiness_level FROM daily_metrics WHERE date <= ? ORDER BY date DESC LIMIT ?",
        (date.today().isoformat(), READINESS_LOOKBACK_DAYS),
    ).fetchall()
    levels = [r["training_readiness_level"] for r in rows]
    streak = readiness_streak(levels)
    return {"consecutive_low_or_poor_days": streak, "unhealthy_trend": streak > 2}


def recompute_daily_metrics(conn):
    rows = conn.execute(
        "SELECT start_time_local, training_stress_score, trimp_load FROM activities ORDER BY start_time_local"
    ).fetchall()
    if not rows:
        return None

    daily_tss, daily_trimp = {}, {}
    for r in rows:
        d = datetime.strptime(r["start_time_local"], "%Y-%m-%d %H:%M:%S").date()
        daily_tss[d] = daily_tss.get(d, 0.0) + (r["training_stress_score"] or 0.0)
        daily_trimp[d] = daily_trimp.get(d, 0.0) + (r["trimp_load"] or 0.0)

    earliest = min(daily_tss.keys())
    today = date.today()
    metrics = compute_daily_metrics(daily_tss, daily_trimp, earliest, today)

    for d, m in metrics.items():
        conn.execute(
            """INSERT INTO daily_metrics (date, daily_tss, daily_trimp, ctl, atl, tsb,
                acute_load_7d, chronic_load_28d, acwr, acwr_status)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 daily_tss=excluded.daily_tss, daily_trimp=excluded.daily_trimp,
                 ctl=excluded.ctl, atl=excluded.atl, tsb=excluded.tsb,
                 acute_load_7d=excluded.acute_load_7d, chronic_load_28d=excluded.chronic_load_28d,
                 acwr=excluded.acwr, acwr_status=excluded.acwr_status""",
            (
                d.isoformat(), daily_tss.get(d, 0.0), daily_trimp.get(d, 0.0),
                m["ctl"], m["atl"], m["tsb"], m["acute_load_7d"], m["chronic_load_28d"],
                m["acwr"], m["acwr_status"],
            ),
        )
    conn.commit()
    return metrics.get(today)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill-days", type=int, default=None,
                         help="Force lookback this many days instead of using last sync date")
    args = parser.parse_args()

    init_db()
    conn = get_connection()
    client = login()

    row = conn.execute("SELECT value FROM sync_state WHERE key = 'last_activity_sync_date'").fetchone()
    if args.backfill_days:
        since = date.today() - timedelta(days=args.backfill_days)
    elif row:
        since = date.fromisoformat(row["value"]) - timedelta(days=1)  # small overlap for safety
    else:
        since = date.today() - timedelta(days=400)  # first run: generous lookback for CTL seeding

    new_count = sync_activities(client, conn, since)
    today_metrics = recompute_daily_metrics(conn)
    sync_readiness(client, conn)
    sync_hrv(client, conn)
    today_metrics = dict(conn.execute(
        "SELECT * FROM daily_metrics WHERE date = ?", (date.today().isoformat(),)
    ).fetchone())
    trend = readiness_trend(conn)

    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('last_activity_sync_date', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (date.today().isoformat(),),
    )
    conn.commit()

    last_activity = conn.execute(
        """SELECT activity_id, activity_type, name, start_time_local, distance_m, duration_s,
                  moving_duration_s, elevation_gain_m, avg_hr, max_hr, avg_power, norm_power,
                  training_stress_score, activity_training_load, trimp_load, is_ebike
           FROM activities ORDER BY start_time_local DESC LIMIT 1"""
    ).fetchone()

    print(json.dumps({
        "new_activities_synced": new_count,
        "since": since.isoformat(),
        "latest_activity": dict(last_activity) if last_activity else None,
        "today_metrics": today_metrics,
        "readiness_trend": trend,
    }, default=str, indent=2))


if __name__ == "__main__":
    main()
