"""Build a small JSON export of the training database for upload to Google Drive.

The Drive tools available to Claude only accept inline content (no
upload-from-local-path option), so a backup has to pass through Claude's own
context. Base64-encoding the raw SQLite file turned out to tokenize close to
1 token per character (base64 doesn't compress well under BPE tokenization,
unlike English or JSON text), making even a gzip-compressed binary backup
cost well over 100k tokens round-trip -- impractical for a routine operation.

A plain JSON export of the core tables (skipping the bulky raw_json blobs,
which are Garmin's full payload kept locally for future-proofing, not needed
for a usable backup) is dramatically cheaper to move as text, and is also
more useful on its own -- readable directly in Drive from a phone, unlike a
binary .db file.

Usage:
    python prepare_backup.py
Prints the path to the JSON file it created.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection

OUT_PATH = "/tmp/training_backup.json"
ROLLING_WINDOW_DAYS = 180

# daily_metrics is deliberately excluded -- it's fully recomputable from activities
# (see recompute_daily_metrics() in garmin_sync.py), so backing it up would just be
# ~80KB of derived data adding nothing a restore needs.

ROUND1 = {"distance_m", "duration_s", "moving_duration_s", "elevation_gain_m", "elevation_loss_m",
          "avg_hr", "max_hr", "avg_power", "norm_power", "training_stress_score",
          "activity_training_load", "trimp_load", "resting_hr_that_day"}


def main():
    conn = get_connection()
    cutoff = (date.today() - timedelta(days=ROLLING_WINDOW_DAYS)).isoformat()
    data = {"exported_at": date.today().isoformat(), "rolling_window_days": ROLLING_WINDOW_DAYS, "window_cutoff": cutoff}

    activities = conn.execute(
        """SELECT activity_id, activity_type, name, start_time_local, distance_m, duration_s,
                  moving_duration_s, elevation_gain_m, elevation_loss_m, avg_hr, max_hr,
                  avg_power, norm_power, training_stress_score, activity_training_load,
                  trimp_load, is_ebike, resting_hr_that_day
           FROM activities WHERE start_time_local >= ? ORDER BY start_time_local""",
        (cutoff,),
    ).fetchall()
    rows = []
    for r in activities:
        d = dict(r)
        for k in ROUND1:
            if d.get(k) is not None:
                d[k] = round(d[k], 1)
        rows.append(d)
    data["activities"] = rows
    activity_ids = [r["activity_id"] for r in rows]

    # goals and weekly_plans aren't per-day time series and stay small regardless
    # of training history length, so they're kept in full rather than windowed.
    data["goals"] = [dict(r) for r in conn.execute("SELECT * FROM goals").fetchall()]
    data["weekly_plans"] = [dict(r) for r in conn.execute("SELECT * FROM weekly_plans").fetchall()]

    if activity_ids:
        placeholders = ",".join("?" * len(activity_ids))
        data["activity_feedback"] = [
            dict(r) for r in conn.execute(
                f"SELECT * FROM activity_feedback WHERE activity_id IN ({placeholders})", activity_ids
            ).fetchall()
        ]
    else:
        data["activity_feedback"] = []

    lifting_sessions = conn.execute(
        "SELECT * FROM lifting_sessions WHERE date >= ? ORDER BY date", (cutoff,)
    ).fetchall()
    data["lifting_sessions"] = [dict(r) for r in lifting_sessions]
    session_ids = [r["id"] for r in lifting_sessions]
    if session_ids:
        placeholders = ",".join("?" * len(session_ids))
        data["lifting_sets"] = [
            dict(r) for r in conn.execute(
                f"SELECT * FROM lifting_sets WHERE session_id IN ({placeholders})", session_ids
            ).fetchall()
        ]
    else:
        data["lifting_sets"] = []

    Path(OUT_PATH).write_text(json.dumps(data, default=str))
    size = Path(OUT_PATH).stat().st_size
    print(f"BACKUP_READY path={OUT_PATH} size_bytes={size} window_days={ROLLING_WINDOW_DAYS} activities={len(rows)}")


if __name__ == "__main__":
    main()
