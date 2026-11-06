#!/usr/bin/env python3
"""Read-only connector check: fetch the last N MyWhoosh activities and find the
matching Garmin Connect activity (recorded by the watch) by start time.

Nothing is uploaded, modified or deleted. Downloaded files are kept in
--out (default: ./connector_check) so they can be inspected / merged later.

Usage:
    python scripts/check_connectors.py --limit 5 [--tolerance 600] [--out DIR]
"""

import argparse
import io
import json
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
from fit_tool.fit_file import FitFile  # noqa: E402
from fit_tool.profile.messages.session_message import SessionMessage  # noqa: E402
from garminconnect import Garmin  # noqa: E402

from services.garmin_service import GarminService  # noqa: E402
from services.mywhoosh_service import MyWhooshService  # noqa: E402

log = logging.getLogger("check_connectors")


def fit_session_window(path):
    """Return (start_utc, duration_s) from the first session message of a FIT file."""
    for record in FitFile.from_file(path).records:
        msg = record.message
        if isinstance(msg, SessionMessage) and msg.start_time:
            start = datetime.fromtimestamp(msg.start_time / 1000, tz=timezone.utc)
            return start, float(msg.total_elapsed_time or msg.total_timer_time or 0)
    raise RuntimeError(f"No session message in {path}")


def garmin_start_utc(activity):
    """Garmin list entries carry 'startTimeGMT' as 'YYYY-MM-DD HH:MM:SS'."""
    value = activity.get("startTimeGMT")
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def download_garmin_fit(client, activity_id, out_dir):
    """Download the ORIGINAL file (a zip containing the device FIT) and extract it."""
    data = client.download_activity(activity_id, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        fit_names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
        if not fit_names:
            raise RuntimeError(f"No .fit in original download: {zf.namelist()}")
        path = os.path.join(out_dir, f"garmin_{activity_id}.fit")
        with open(path, "wb") as f:
            f.write(zf.read(fit_names[0]))
    return path


def is_mywhoosh_upload(path):
    """True if the Garmin activity is a MyWhoosh file we (or MyWhoosh) uploaded, not the watch recording."""
    with open(path, "rb") as f:
        return b"mywhoosh" in f.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=5, help="number of latest MyWhoosh activities")
    parser.add_argument("--tolerance", type=int, default=600, help="allowed seconds of the MyWhoosh ride outside the Garmin activity")
    parser.add_argument("--out", default="connector_check", help="directory for downloaded files")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    load_dotenv()
    os.makedirs(args.out, exist_ok=True)

    # --- MyWhoosh -----------------------------------------------------------
    mw = MyWhooshService(os.environ["MYWHOOSH_EMAIL"], os.environ["MYWHOOSH_PASSWORD"])
    mw.authenticate()
    activities = mw.get_activities(limit=args.limit)[: args.limit]
    print(f"[MyWhoosh] auth OK, got {len(activities)} activities")
    if activities:
        # Dump the raw listing once so the date field format is known.
        with open(os.path.join(args.out, "mywhoosh_activities.json"), "w") as f:
            json.dump(activities, f, indent=2, default=str)
        print(f"[MyWhoosh] keys of first activity: {sorted(activities[0].keys())}")

    # --- Garmin -------------------------------------------------------------
    garmin = GarminService(os.environ["GARMIN_USERNAME"], os.environ["GARMIN_PASSWORD"])
    garmin.authenticate()
    print("[Garmin]   auth OK")

    ok = 0
    for act in activities:
        act_id = act.get("id", act.get("_id"))
        print(f"\n--- MyWhoosh {act_id}  '{act.get('name', act.get('title', '?'))}'")
        try:
            mw_path = mw.download_activity(act)
            final = os.path.join(args.out, f"mywhoosh_{act_id}.fit")
            os.replace(mw_path, final)
            start, duration = fit_session_window(final)
            print(f"  MyWhoosh FIT: {final}  start={start:%Y-%m-%d %H:%M:%S}Z  dur={duration / 60:.1f} min")
        except Exception as e:  # keep going with the other activities
            print(f"  ✗ MyWhoosh download/parse failed: {e}")
            continue

        # Garmin list dates are local; query ±1 day around the UTC start to be safe.
        day = start.date()
        candidates = garmin.client.get_activities_by_date(
            (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat())
        # Match by time overlap, not start time: one watch recording can contain
        # several MyWhoosh rides (e.g. two routes ridden back to back).
        end = start + timedelta(seconds=duration)
        scored = []
        for g in candidates:
            g_start = garmin_start_utc(g)
            if g_start is None:
                continue
            g_end = g_start + timedelta(seconds=float(g.get("elapsedDuration") or g.get("duration") or 0))
            overlap = (min(end, g_end) - max(start, g_start)).total_seconds()
            scored.append((overlap, abs((g_start - start).total_seconds()), g_start, g))
        scored.sort(key=lambda s: -s[0])

        # Require the overlap to cover most of the MyWhoosh ride (tolerance for clock skew).
        matches = [s for s in scored if s[0] >= duration - args.tolerance]
        if not matches:
            best = f"best overlap {scored[0][0]:.0f}s of {duration:.0f}s" if scored else "no Garmin activities in range"
            print(f"  ✗ no Garmin activity covering this ride ({best})")
            continue

        found_watch = False
        for overlap, delta, g_start, g in matches:
            gid = g["activityId"]
            print(f"  Garmin {gid} '{g.get('activityName')}' start={g_start:%H:%M:%S}Z Δstart={delta:.0f}s "
                  f"overlap={overlap / 60:.1f}/{duration / 60:.1f} min "
                  f"type={g.get('activityType', {}).get('typeKey')} device={g.get('deviceId')}")
            try:
                g_path = download_garmin_fit(garmin.client, gid, args.out)
            except Exception as e:
                print(f"    ✗ download failed: {e}")
                continue
            source = "MyWhoosh upload" if is_mywhoosh_upload(g_path) else "watch recording"
            print(f"    ✓ downloaded {os.path.getsize(g_path):,} B -> {g_path}  [{source}]")
            found_watch |= source == "watch recording"
        if found_watch:
            ok += 1
        else:
            print("  ⚠ only MyWhoosh uploads matched, no watch recording")

    print(f"\nSummary: {ok}/{len(activities)} MyWhoosh activities matched to a watch recording")
    return 0 if activities and ok == len(activities) else 1


if __name__ == "__main__":
    sys.exit(main())
