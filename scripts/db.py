"""Shared SQLite connection helper for the coach scripts."""
import os
import sqlite3
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH = os.environ.get("TRAINING_DB_PATH", str(SCRIPT_DIR.parent.parent / "TrainingData" / "training.db"))


def get_connection() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    schema = (SCRIPT_DIR / "schema.sql").read_text()
    conn = get_connection()
    try:
        conn.executescript(schema)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to already-created tables that predate a schema change.
    CREATE TABLE IF NOT EXISTS in schema.sql only covers brand-new databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(daily_metrics)")}
    if "training_readiness_score" not in cols:
        conn.execute("ALTER TABLE daily_metrics ADD COLUMN training_readiness_score REAL")
    if "training_readiness_level" not in cols:
        conn.execute("ALTER TABLE daily_metrics ADD COLUMN training_readiness_level TEXT")
    if "hrv_last_night_avg" not in cols:
        conn.execute("ALTER TABLE daily_metrics ADD COLUMN hrv_last_night_avg REAL")
    if "hrv_status" not in cols:
        conn.execute("ALTER TABLE daily_metrics ADD COLUMN hrv_status TEXT")
    conn.commit()


if __name__ == "__main__":
    init_db()
    print(f"DB_INITIALIZED path={DB_PATH}")
