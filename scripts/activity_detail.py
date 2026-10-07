"""On-demand Garmin API calls for a single activity's interval/lap power and
power-curve data -- used right after a ride, when building the daily summary
visualizations. Kept separate from garmin_sync.py so routine syncs don't pay
this extra API cost for every historical activity, only the one just ridden.

Usage:
    python activity_detail.py --activity-id 23903105383
"""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

import garminconnect

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--activity-id", type=int, required=True)
    args = parser.parse_args()

    client = garminconnect.Garmin()
    client.login(tokenstore=os.environ["GARMIN_TOKENS_DIR"])

    splits = client.get_activity_splits(args.activity_id)
    laps = []
    for lap in splits.get("lapDTOs", []):
        laps.append({
            "lap_index": lap.get("lapIndex"),
            "duration_s": lap.get("duration"),
            "distance_m": lap.get("distance"),
            "avg_power": lap.get("averagePower"),
            "max_power": lap.get("maxPower"),
            "norm_power": lap.get("normalizedPower"),
            "avg_hr": lap.get("averageHR"),
            "max_hr": lap.get("maxHR"),
        })

    # The power curve (maxAvgPower_<seconds>) is already present in the activity
    # summary we stored as raw_json during garmin_sync.py -- no extra API call needed.
    conn = get_connection()
    row = conn.execute("SELECT raw_json FROM activities WHERE activity_id = ?", (args.activity_id,)).fetchone()
    power_curve = {}
    if row and row["raw_json"]:
        activity = json.loads(row["raw_json"])
        power_curve = {
            k.replace("maxAvgPower_", "") + "s": v
            for k, v in activity.items()
            if k.startswith("maxAvgPower_") and v is not None
        }

    print(json.dumps({"laps": laps, "power_curve_watts_by_seconds": power_curve}, indent=2))


if __name__ == "__main__":
    main()
