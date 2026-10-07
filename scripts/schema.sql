-- Core synced Garmin activities (cycling, running, strength, everything).
CREATE TABLE IF NOT EXISTS activities (
    activity_id         INTEGER PRIMARY KEY,
    activity_type        TEXT NOT NULL,
    name                  TEXT,
    start_time_local      TEXT NOT NULL,
    start_time_utc        TEXT,
    distance_m            REAL,
    duration_s             REAL,
    moving_duration_s      REAL,
    elevation_gain_m       REAL,
    elevation_loss_m       REAL,
    avg_hr                 REAL,
    max_hr                  REAL,
    avg_power               REAL,
    norm_power               REAL,
    training_stress_score    REAL,  -- Garmin's own TSS (power-based, mainly cycling)
    activity_training_load   REAL,  -- Garmin's own EPOC-based load
    trimp_load                REAL,  -- our HR-based Banister TRIMP proxy, all activity types
    is_ebike                  INTEGER NOT NULL DEFAULT 0,
    resting_hr_that_day        REAL,
    raw_json                    TEXT,  -- full Garmin payload, for future-proofing
    synced_at                    TEXT NOT NULL
);

-- Post-activity Q&A: RPE, social ride flag, pain.
CREATE TABLE IF NOT EXISTS activity_feedback (
    activity_id      INTEGER PRIMARY KEY REFERENCES activities(activity_id),
    rpe                INTEGER,          -- 1-10 Borg CR10
    is_social_ride      INTEGER,          -- 1/0/NULL, cycling only
    had_pain              INTEGER NOT NULL,
    pain_note              TEXT,
    logged_at              TEXT NOT NULL
);

-- Daily CTL/ATL/TSB/ACWR rollup, one row per calendar date.
CREATE TABLE IF NOT EXISTS daily_metrics (
    date                 TEXT PRIMARY KEY,
    daily_tss              REAL NOT NULL DEFAULT 0,     -- sum of Garmin trainingStressScore that day
    daily_trimp             REAL NOT NULL DEFAULT 0,     -- sum of trimp_load that day, all activity types
    ctl                       REAL,   -- 42-day EWMA of daily_tss
    atl                       REAL,   -- 7-day EWMA of daily_tss
    tsb                       REAL,   -- ctl - atl
    acute_load_7d              REAL,  -- true rolling 7-day avg of daily_trimp
    chronic_load_28d            REAL, -- true rolling 28-day avg of daily_trimp
    acwr                         REAL, -- acute_load_7d / chronic_load_28d
    acwr_status                   TEXT, -- Low (<0.8) / Medium (0.8-1.3) / High (>1.3)
    training_readiness_score       REAL, -- Garmin's 0-100 score, most recent reading of the day
    training_readiness_level        TEXT, -- Garmin's level string, e.g. LOW / MODERATE / HIGH / POOR
    hrv_last_night_avg               REAL, -- Garmin's overnight average HRV (ms), from get_hrv_data
    hrv_status                        TEXT  -- Garmin's status string, e.g. BALANCED / UNBALANCED / LOW
);

-- Lifting sessions parsed from the journal Google Doc (Garmin doesn't capture set/rep/weight).
CREATE TABLE IF NOT EXISTS lifting_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    raw_note         TEXT,     -- freeform prose from that day's entry, if any
    synced_at         TEXT NOT NULL,
    UNIQUE(date)
);

CREATE TABLE IF NOT EXISTS lifting_sets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         INTEGER NOT NULL REFERENCES lifting_sessions(id),
    exercise_name        TEXT NOT NULL,
    sets                   INTEGER,
    reps                    INTEGER,
    weight_lbs               REAL,
    band                      TEXT,
    side                       TEXT,     -- 'each side' / 'left' / 'right' / NULL
    notes                       TEXT
);

-- Journal entries already ingested, so we don't reprocess the whole doc every time.
CREATE TABLE IF NOT EXISTS journal_sync_state (
    date          TEXT PRIMARY KEY,
    ingested_at     TEXT NOT NULL
);

-- Cycling/running goals, remembered across sessions and updated as they change.
CREATE TABLE IF NOT EXISTS goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    goal_type          TEXT NOT NULL,   -- 'event' / 'ftp' / 'volume' / 'process' / 'injury_prevention'
    description          TEXT NOT NULL,
    target_date            TEXT,
    target_value             REAL,
    status                    TEXT NOT NULL DEFAULT 'active',  -- active/completed/abandoned
    notes                      TEXT
);

-- Weekly plans, one row per week (Monday start date), created/reviewed each Sunday.
CREATE TABLE IF NOT EXISTS weekly_plans (
    week_start        TEXT PRIMARY KEY,   -- Monday, ISO date
    plan_text            TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    reviewed_at              TEXT,
    review_notes               TEXT
);

-- Simple key/value store for sync bookkeeping.
CREATE TABLE IF NOT EXISTS sync_state (
    key      TEXT PRIMARY KEY,
    value      TEXT
);
