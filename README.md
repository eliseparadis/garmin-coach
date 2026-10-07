# Garmin Coach

A Claude Code skill that turns your Garmin Connect data (cycling, running,
lifting) into a coached training plan — TSS, CTL/ATL/TSB, ACWR injury-risk
tracking, RPE/pain check-ins, and weekly reviews grounded in your own training
history, not generic advice.

See [`SKILL.md`](SKILL.md) for the full behavior spec (what Claude does and
when), and [`references/calculations.md`](references/calculations.md) for the
training-load math.

## What you need

- Claude Code (or Claude Desktop) with this folder available as a skill
- A Garmin Connect account
- Python 3.9+
- (Optional but recommended) a Google Doc to use as a training journal — for
  lifting logs and freeform notes that Garmin doesn't capture

## Setup

### 1. Get the code and dependencies

```
git clone https://github.com/eliseparadis/garmin-coach.git ~/Documents/ClaudeCoach
cd ~/Documents/ClaudeCoach
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

### 2. Connect Garmin

Copy `.env.example` to `.env` and fill in:

- `GARMIN_USERNAME` / `GARMIN_PASSWORD` — your Garmin Connect login
- `GARMIN_TOKENS_DIR` — where session tokens get cached after first login
  (default `./.garmin_tokens`)
- `TRAINING_DB_PATH` — where your local SQLite database should live. Pick
  somewhere *outside* this repo (e.g. `~/Documents/TrainingData/training.db`)
  so your training data stays separate from the code and never ends up
  committed.
- `TRIMP_SEX` — `female` or `male`. Picks the correct Banister TRIMP
  coefficients for the ACWR calculation (see `references/calculations.md`) —
  this matters, the two aren't interchangeable.

Then run the first sync:

```
venv/bin/python scripts/garmin_sync.py --backfill-days 400
```

This logs into Garmin Connect and backfills your activity history (adjust
`--backfill-days` for more/less history). **MFA may be required the first
time** — follow the prompt. After this, session tokens are cached in
`GARMIN_TOKENS_DIR`, so future syncs won't ask for MFA again unless the tokens
expire.

### 3. Connect your notes (optional)

Garmin has no concept of lifting sets/reps/weight or freeform reflections, so
this skill can pull that from a journal you keep in a Google Doc instead:

1. Create a Google Doc for your training journal. Dated entries with a
   lifting log look like:
   ```
   September 23

   Hip hinges 2 x 8 x 35/
   Bulgarian split squats 2 x 6 x 15/ each side
   ```
   Prose-only entries (how you're feeling, context, injuries) work too —
   Claude reads both.
2. The first time you ask Claude to do a check-in or weekly review, tell it
   the doc's link in chat. Claude remembers it from then on (see the "Journal
   ingestion" section of `SKILL.md`) — there's no config file for this, it's
   just a normal part of the conversation the first time.

If you skip this step, everything else still works — you just won't get
lifting-log ingestion or journal-derived context (e.g. an injury mentioned in
prose before it shows up as a training-load change).

### 4. Start using it

Point Claude Code at this folder as a skill, then just talk to it:

- "sync my Garmin data"
- "how'd today's ride go?"
- "what's my week looking like?"

Sunday check-ins trigger a full weekly review automatically (see `SKILL.md`)
— no need to ask for one by name.
