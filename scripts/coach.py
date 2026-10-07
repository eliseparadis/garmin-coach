"""CLI for everything that doesn't need a live Garmin API call: post-activity
feedback, journal ingestion, goals, weekly plans, and dashboard/trend queries
against the local database. Claude calls this after reading the journal doc
or asking the post-ride questions -- it never touches Garmin itself.

Subcommands:
  feedback          Record RPE / social-ride / pain for an activity
  log-lifting       Record a lifting session parsed from the journal doc
  journal-mark-synced   Mark a journal date as ingested (prose-only days)
  journal-last-synced   Get the last ingested journal date
  goal-add / goal-list / goal-update
  plan-set / plan-get / plan-review
  dashboard         Latest activity + feedback + weekly aggregate + goals + plan
  trend             daily_metrics series for charting CTL/ATL/ACWR over time
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection, init_db
from calculations import readiness_streak, rolling_avg_ignore_missing

READINESS_LOOKBACK_DAYS = 7
HRV_AVG_WINDOW_DAYS = 30


def cmd_feedback(args):
    conn = get_connection()
    conn.execute(
        """INSERT INTO activity_feedback (activity_id, rpe, is_social_ride, had_pain, pain_note, logged_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(activity_id) DO UPDATE SET
             rpe=excluded.rpe, is_social_ride=excluded.is_social_ride,
             had_pain=excluded.had_pain, pain_note=excluded.pain_note, logged_at=excluded.logged_at""",
        (args.activity_id, args.rpe, args.social, int(args.pain), args.pain_note, datetime.now().isoformat()),
    )
    conn.commit()
    print(json.dumps({"ok": True}))


def cmd_log_lifting(args):
    conn = get_connection()
    exercises = json.loads(args.exercises_json)
    cur = conn.execute(
        """INSERT INTO lifting_sessions (date, raw_note, synced_at) VALUES (?,?,?)
           ON CONFLICT(date) DO UPDATE SET raw_note=excluded.raw_note, synced_at=excluded.synced_at""",
        (args.date, args.note, datetime.now().isoformat()),
    )
    session_id = conn.execute("SELECT id FROM lifting_sessions WHERE date = ?", (args.date,)).fetchone()["id"]
    conn.execute("DELETE FROM lifting_sets WHERE session_id = ?", (session_id,))
    for ex in exercises:
        conn.execute(
            """INSERT INTO lifting_sets (session_id, exercise_name, sets, reps, weight_lbs, band, side, notes)
               VALUES (?,?,?,?,?,?,?,?)""",
            (session_id, ex.get("exercise_name"), ex.get("sets"), ex.get("reps"), ex.get("weight_lbs"),
             ex.get("band"), ex.get("side"), ex.get("notes")),
        )
    conn.execute(
        "INSERT INTO journal_sync_state (date, ingested_at) VALUES (?,?) "
        "ON CONFLICT(date) DO UPDATE SET ingested_at=excluded.ingested_at",
        (args.date, datetime.now().isoformat()),
    )
    conn.commit()
    print(json.dumps({"ok": True, "session_id": session_id, "exercises_logged": len(exercises)}))


def cmd_journal_mark_synced(args):
    conn = get_connection()
    conn.execute(
        "INSERT INTO journal_sync_state (date, ingested_at) VALUES (?,?) "
        "ON CONFLICT(date) DO UPDATE SET ingested_at=excluded.ingested_at",
        (args.date, datetime.now().isoformat()),
    )
    conn.commit()
    print(json.dumps({"ok": True}))


def cmd_journal_last_synced(_args):
    conn = get_connection()
    row = conn.execute("SELECT MAX(date) AS d FROM journal_sync_state").fetchone()
    print(json.dumps({"last_synced_date": row["d"]}))


def cmd_goal_add(args):
    conn = get_connection()
    now = datetime.now().isoformat()
    cur = conn.execute(
        """INSERT INTO goals (created_at, updated_at, goal_type, description, target_date, target_value, notes)
           VALUES (?,?,?,?,?,?,?)""",
        (now, now, args.type, args.description, args.target_date, args.target_value, args.notes),
    )
    conn.commit()
    print(json.dumps({"ok": True, "goal_id": cur.lastrowid}))


def cmd_goal_list(args):
    conn = get_connection()
    q = "SELECT * FROM goals"
    params = ()
    if args.status:
        q += " WHERE status = ?"
        params = (args.status,)
    rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    print(json.dumps(rows, indent=2))


def cmd_goal_update(args):
    conn = get_connection()
    fields, params = [], []
    for f in ("status", "notes", "description", "target_date", "target_value"):
        v = getattr(args, f)
        if v is not None:
            fields.append(f"{f} = ?")
            params.append(v)
    fields.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(args.id)
    conn.execute(f"UPDATE goals SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()
    print(json.dumps({"ok": True}))


def cmd_plan_set(args):
    conn = get_connection()
    conn.execute(
        """INSERT INTO weekly_plans (week_start, plan_text, created_at) VALUES (?,?,?)
           ON CONFLICT(week_start) DO UPDATE SET plan_text=excluded.plan_text""",
        (args.week_start, args.text, datetime.now().isoformat()),
    )
    conn.commit()
    print(json.dumps({"ok": True}))


def cmd_plan_get(args):
    conn = get_connection()
    row = conn.execute("SELECT * FROM weekly_plans WHERE week_start = ?", (args.week_start,)).fetchone()
    print(json.dumps(dict(row) if row else None))


def cmd_plan_review(args):
    conn = get_connection()
    conn.execute(
        "UPDATE weekly_plans SET reviewed_at = ?, review_notes = ? WHERE week_start = ?",
        (datetime.now().isoformat(), args.notes, args.week_start),
    )
    conn.commit()
    print(json.dumps({"ok": True}))


def attach_hrv_rolling_avg(conn, rows):
    """Adds hrv_30d_avg to each daily_metrics row dict -- the trailing 30-day
    average HRV as of that date, ignoring days with no reading (see
    rolling_avg_ignore_missing). Pulls extra lookback before the rows' own range
    so the average is populated even for the earliest requested date."""
    if not rows:
        return
    dates = [date.fromisoformat(r["date"]) for r in rows]
    lookback_start = min(dates) - timedelta(days=HRV_AVG_WINDOW_DAYS)
    extra = conn.execute(
        "SELECT date, hrv_last_night_avg FROM daily_metrics WHERE date >= ? AND date <= ?",
        (lookback_start.isoformat(), max(dates).isoformat()),
    ).fetchall()
    daily_hrv = {date.fromisoformat(r["date"]): r["hrv_last_night_avg"] for r in extra}
    for r in rows:
        avg = rolling_avg_ignore_missing(daily_hrv, HRV_AVG_WINDOW_DAYS, date.fromisoformat(r["date"]))
        r["hrv_30d_avg"] = round(avg, 1) if avg is not None else None


def cmd_dashboard(_args):
    conn = get_connection()
    latest = conn.execute(
        """SELECT a.*, f.rpe, f.is_social_ride, f.had_pain, f.pain_note
           FROM activities a LEFT JOIN activity_feedback f ON f.activity_id = a.activity_id
           ORDER BY a.start_time_local DESC LIMIT 1"""
    ).fetchone()
    latest_d = dict(latest) if latest else None
    if latest_d:
        latest_d.pop("raw_json", None)

    week_ago = (date.today() - timedelta(days=7)).isoformat()
    weekly = conn.execute(
        """SELECT activity_type, COUNT(*) n, SUM(duration_s) total_duration_s,
                  SUM(distance_m) total_distance_m, SUM(elevation_gain_m) total_elevation_m,
                  SUM(training_stress_score) total_tss
           FROM activities WHERE start_time_local >= ? GROUP BY activity_type"""
        , (week_ago,)
    ).fetchall()

    today_metrics = conn.execute(
        "SELECT * FROM daily_metrics WHERE date = ?", (date.today().isoformat(),)
    ).fetchone()

    goals = [dict(r) for r in conn.execute("SELECT * FROM goals WHERE status = 'active'").fetchall()]

    this_monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    plan = conn.execute("SELECT * FROM weekly_plans WHERE week_start = ?", (this_monday,)).fetchone()

    readiness_rows = conn.execute(
        "SELECT training_readiness_level FROM daily_metrics WHERE date <= ? ORDER BY date DESC LIMIT ?",
        (date.today().isoformat(), READINESS_LOOKBACK_DAYS),
    ).fetchall()
    streak = readiness_streak([r["training_readiness_level"] for r in readiness_rows])

    today_metrics_d = dict(today_metrics) if today_metrics else None
    if today_metrics_d:
        attach_hrv_rolling_avg(conn, [today_metrics_d])

    print(json.dumps({
        "latest_activity": latest_d,
        "weekly_by_type": [dict(r) for r in weekly],
        "today_metrics": today_metrics_d,
        "readiness_trend": {"consecutive_low_or_poor_days": streak, "unhealthy_trend": streak > 2},
        "active_goals": goals,
        "current_week_plan": dict(plan) if plan else None,
    }, default=str, indent=2))


def cmd_backup_check(_args):
    conn = get_connection()
    row = conn.execute("SELECT value FROM sync_state WHERE key = 'last_drive_backup_date'").fetchone()
    stale = row is None or date.fromisoformat(row["value"]) <= date.today() - timedelta(days=7)
    print(json.dumps({"needs_backup": stale, "last_backup_date": row["value"] if row else None}))


def cmd_backup_mark(_args):
    conn = get_connection()
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES ('last_drive_backup_date', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (date.today().isoformat(),),
    )
    conn.commit()
    print(json.dumps({"ok": True}))


def cmd_trend(args):
    conn = get_connection()
    since = (date.today() - timedelta(days=args.days)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM daily_metrics WHERE date >= ? ORDER BY date", (since,)
    ).fetchall()]
    attach_hrv_rolling_avg(conn, rows)
    print(json.dumps(rows, indent=2))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("feedback")
    p.add_argument("--activity-id", type=int, required=True)
    p.add_argument("--rpe", type=int, required=True)
    p.add_argument("--social", type=lambda x: None if x is None else x.lower() == "true", default=None)
    p.add_argument("--pain", type=lambda x: x.lower() == "true", required=True)
    p.add_argument("--pain-note", default=None)
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("log-lifting")
    p.add_argument("--date", required=True)
    p.add_argument("--exercises-json", required=True, help='JSON list of {exercise_name, sets, reps, weight_lbs, band, side, notes}')
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_log_lifting)

    p = sub.add_parser("journal-mark-synced")
    p.add_argument("--date", required=True)
    p.set_defaults(func=cmd_journal_mark_synced)

    p = sub.add_parser("journal-last-synced")
    p.set_defaults(func=cmd_journal_last_synced)

    p = sub.add_parser("goal-add")
    p.add_argument("--type", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--target-date", default=None)
    p.add_argument("--target-value", type=float, default=None)
    p.add_argument("--notes", default=None)
    p.set_defaults(func=cmd_goal_add)

    p = sub.add_parser("goal-list")
    p.add_argument("--status", default=None)
    p.set_defaults(func=cmd_goal_list)

    p = sub.add_parser("goal-update")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--status", default=None)
    p.add_argument("--notes", default=None)
    p.add_argument("--description", default=None)
    p.add_argument("--target-date", default=None)
    p.add_argument("--target-value", type=float, default=None)
    p.set_defaults(func=cmd_goal_update)

    p = sub.add_parser("plan-set")
    p.add_argument("--week-start", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_plan_set)

    p = sub.add_parser("plan-get")
    p.add_argument("--week-start", required=True)
    p.set_defaults(func=cmd_plan_get)

    p = sub.add_parser("plan-review")
    p.add_argument("--week-start", required=True)
    p.add_argument("--notes", required=True)
    p.set_defaults(func=cmd_plan_review)

    p = sub.add_parser("dashboard")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("backup-check")
    p.set_defaults(func=cmd_backup_check)

    p = sub.add_parser("backup-mark")
    p.set_defaults(func=cmd_backup_mark)

    p = sub.add_parser("trend")
    p.add_argument("--days", type=int, default=90)
    p.set_defaults(func=cmd_trend)

    init_db()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
