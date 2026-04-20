#!/usr/bin/env python3
"""Parse Health Auto Export JSON and upsert sleep records into Airtable."""

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.airtable.com/v0/appBmQA2p3z2Fdofa/tbl9nIjgF1dE079YK"


def _headers():
    """Build Airtable auth headers from the current environment.

    Called at request time so this module is safe to import without
    AIRTABLE_API_KEY set — e.g. when sleep_watcher.py imports process_file()
    before the env is fully loaded. Raises EnvironmentError if the key is
    missing when an actual API call is attempted.
    """
    api_key = os.getenv("AIRTABLE_API_KEY")
    if not api_key:
        raise EnvironmentError("AIRTABLE_API_KEY not set. Check your .env file.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

# Field IDs
FLD_NIGHT = "fldfvdS2NyzqpUoys"
FLD_SLEEP_START = "fldvZFHHyCdKBuaB8"
FLD_SLEEP_END = "fldUPMVUEMJlQUFIC"
FLD_TIME_ASLEEP = "fldYps5TPDV1BkwVG"
FLD_TIME_IN_BED = "flddMsToI6WkvTFLN"
FLD_AWAKE = "fldsBQ5In6BgnzKc9"
FLD_REM = "fldP2pxz4gtqIelMr"
FLD_CORE = "fldCCG56EQAGOEXr2"
FLD_DEEP = "fldDls5eNT2QuQgIS"
FLD_SOURCE = "fld6xBTjUvS3wC1vg"

# State mapping from Health Auto Export values to normalized states
STATE_MAP = {
    "In Bed": "inBed",
    "Awake": "awake",
    "Core": "asleepCore",
    "Deep": "asleepDeep",
    "REM": "asleepREM",
    "Asleep": "asleepUnspecified",
    "Unspecified": "asleepUnspecified",
}

# Precedence for resolving overlapping states (higher index wins)
STATE_PRECEDENCE = {
    "inBed": 0,
    "asleepUnspecified": 1,
    "asleepCore": 2,
    "asleepDeep": 3,
    "asleepREM": 4,
    "awake": 5,
}

SLEEP_STATES = {"asleepCore", "asleepDeep", "asleepREM", "asleepUnspecified"}

# Local timezone for night bucketing
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except ImportError:
    import pytz
    LOCAL_TZ = pytz.timezone("America/Los_Angeles")


def parse_timestamp(ts_str):
    """Parse Health Auto Export timestamp like '2026-03-21 21:34:58 -0700'."""
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S %z")


def to_local(dt):
    """Convert a datetime to local time."""
    return dt.astimezone(LOCAL_TZ)


def get_night_date(dt_local):
    """Determine which night a local datetime belongs to using 3 PM anchor.

    Night of date D = [D-1 15:00, D 14:59:59].
    So if local time is before 3 PM, it belongs to today's night.
    If local time is 3 PM or later, it belongs to tomorrow's night.
    """
    if dt_local.hour < 15:
        return dt_local.date()
    else:
        return (dt_local + timedelta(days=1)).date()


def parse_samples(json_path):
    """Parse the Health Auto Export JSON and return sleep samples."""
    with open(json_path, "r") as f:
        data = json.load(f)

    metrics = data.get("data", {}).get("metrics", [])
    sleep_metric = None
    for m in metrics:
        if m.get("name") == "sleep_analysis":
            sleep_metric = m
            break

    if not sleep_metric:
        return []

    samples = []
    for entry in sleep_metric.get("data", []):
        value = entry.get("value", "")
        normalized = STATE_MAP.get(value)
        if normalized is None:
            continue

        try:
            start = parse_timestamp(entry["start"])
            end = parse_timestamp(entry["end"])
        except (KeyError, ValueError):
            continue

        if end <= start:
            continue

        source = entry.get("source", "Unknown")
        samples.append({
            "start": start,
            "end": end,
            "state": normalized,
            "source": source,
        })

    return samples


def bucket_by_night_and_source(samples):
    """Group samples by (night_date, source)."""
    buckets = defaultdict(list)
    for s in samples:
        local_start = to_local(s["start"])
        local_end = to_local(s["end"])
        # Use the start time for bucketing
        night = get_night_date(local_start)
        key = (night, s["source"])
        buckets[key].append(s)
    return buckets


def resolve_timeline(samples):
    """Build a resolved timeline from overlapping samples.

    Returns a list of (start, end, resolved_state) tuples.
    """
    if not samples:
        return []

    # Collect all boundary timestamps
    boundaries = set()
    for s in samples:
        boundaries.add(s["start"])
        boundaries.add(s["end"])
    boundaries = sorted(boundaries)

    if len(boundaries) < 2:
        return []

    resolved = []
    for i in range(len(boundaries) - 1):
        seg_start = boundaries[i]
        seg_end = boundaries[i + 1]
        if seg_start >= seg_end:
            continue

        # Find all states covering this segment
        covering_states = []
        for s in samples:
            if s["start"] <= seg_start and s["end"] >= seg_end:
                covering_states.append(s["state"])

        if not covering_states:
            continue

        # Resolve by precedence
        best = max(covering_states, key=lambda st: STATE_PRECEDENCE.get(st, -1))
        resolved.append((seg_start, seg_end, best))

    # Merge adjacent segments with the same state
    merged = []
    for seg in resolved:
        if merged and merged[-1][2] == seg[2] and merged[-1][1] == seg[0]:
            merged[-1] = (merged[-1][0], seg[1], seg[2])
        else:
            merged.append(list(seg))

    return [(s, e, st) for s, e, st in merged]


def compute_metrics(samples, resolved_timeline):
    """Compute sleep metrics from samples and resolved timeline."""
    # Time in bed: union of all raw inBed intervals
    in_bed_intervals = [(s["start"], s["end"]) for s in samples if s["state"] == "inBed"]
    time_in_bed = union_duration(in_bed_intervals)

    # From resolved timeline
    total_sleep = timedelta()
    awake_time = timedelta()
    core_minutes = timedelta()
    deep_minutes = timedelta()
    rem_minutes = timedelta()

    for start, end, state in resolved_timeline:
        duration = end - start
        if state in SLEEP_STATES:
            total_sleep += duration
        if state == "awake":
            awake_time += duration
        if state == "asleepCore":
            core_minutes += duration
        if state == "asleepDeep":
            deep_minutes += duration
        if state == "asleepREM":
            rem_minutes += duration

    # Sleep start/end: earliest start and latest end of asleep segments
    sleep_starts = [s for s, e, st in resolved_timeline if st in SLEEP_STATES]
    sleep_ends = [e for s, e, st in resolved_timeline if st in SLEEP_STATES]

    sleep_start = min(sleep_starts) if sleep_starts else None
    sleep_end = max(sleep_ends) if sleep_ends else None

    return {
        "total_sleep": total_sleep,
        "time_in_bed": time_in_bed,
        "awake_time": awake_time,
        "core": core_minutes,
        "deep": deep_minutes,
        "rem": rem_minutes,
        "sleep_start": sleep_start,
        "sleep_end": sleep_end,
    }


def union_duration(intervals):
    """Compute the union duration of a list of (start, end) intervals."""
    if not intervals:
        return timedelta()

    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    total = timedelta()
    for start, end in merged:
        total += end - start
    return total


def build_airtable_record(night_date, source, metrics):
    """Build an Airtable record dict from computed metrics."""
    fields = {
        FLD_NIGHT: night_date.isoformat(),
        FLD_SOURCE: source,
    }

    if metrics["sleep_start"]:
        fields[FLD_SLEEP_START] = to_local(metrics["sleep_start"]).strftime("%Y-%m-%dT%H:%M:%S")
    if metrics["sleep_end"]:
        fields[FLD_SLEEP_END] = to_local(metrics["sleep_end"]).strftime("%Y-%m-%dT%H:%M:%S")

    total_hours = metrics["total_sleep"].total_seconds() / 3600
    fields[FLD_TIME_ASLEEP] = round(total_hours, 2)

    bed_hours = metrics["time_in_bed"].total_seconds() / 3600
    if bed_hours > 0:
        fields[FLD_TIME_IN_BED] = round(bed_hours, 2)

    awake_min = round(metrics["awake_time"].total_seconds() / 60)
    if awake_min > 0:
        fields[FLD_AWAKE] = awake_min

    # Only include stage fields if they have data
    rem_min = round(metrics["rem"].total_seconds() / 60)
    core_min = round(metrics["core"].total_seconds() / 60)
    deep_min = round(metrics["deep"].total_seconds() / 60)

    if rem_min > 0:
        fields[FLD_REM] = rem_min
    if core_min > 0:
        fields[FLD_CORE] = core_min
    if deep_min > 0:
        fields[FLD_DEEP] = deep_min

    return {"fields": fields}


def fetch_existing_records():
    """Fetch all existing (Night, Source) pairs from Airtable."""
    existing = set()
    # returnFieldsByFieldId=true makes Airtable key the response fields by ID
    # instead of name, so our FLD_* constants work correctly for lookup.
    params = {
        "fields[]": [FLD_NIGHT, FLD_SOURCE],
        "returnFieldsByFieldId": "true",
    }
    offset = None

    while True:
        if offset:
            params["offset"] = offset
        resp = requests.get(BASE_URL, headers=_headers(), params=params)
        resp.raise_for_status()
        data = resp.json()

        for record in data.get("records", []):
            fields = record.get("fields", {})
            night = fields.get(FLD_NIGHT, "")
            source = fields.get(FLD_SOURCE, "")
            if night and source:
                existing.add((night, source))

        offset = data.get("offset")
        if not offset:
            break

    return existing


def insert_records(records):
    """Insert records into Airtable in batches of 10."""
    errors = []
    for i in range(0, len(records), 10):
        batch = records[i:i + 10]
        payload = {"records": batch}
        resp = requests.post(BASE_URL, headers=_headers(), json=payload)
        if resp.status_code != 200:
            errors.append(f"Batch {i // 10 + 1}: {resp.status_code} {resp.text}")
        else:
            resp.raise_for_status()
    return errors


def process_file(input_path, dry_run=False, start_date=None, end_date=None):
    """Parse a Health Auto Export JSON file and sync sleep records to Airtable.

    This is the primary callable entry point, designed for in-process use by
    sleep_watcher.py. Unlike main(), it raises exceptions on failure instead of
    calling sys.exit(), so the caller (the long-running watcher process) stays alive.

    Args:
        input_path: Path to the Health Auto Export JSON file (str or Path).
        dry_run: If True, parse and preview without writing to Airtable.
        start_date: Optional date string "YYYY-MM-DD" (inclusive lower bound).
        end_date: Optional date string "YYYY-MM-DD" (inclusive upper bound).

    Returns:
        (inserted, skipped, errors) tuple of counts.

    Raises:
        FileNotFoundError: Input file does not exist.
        EnvironmentError: AIRTABLE_API_KEY not set (raised inside _headers()).
        OSError: Could not acquire the updater lock.
        requests.RequestException: Airtable API call failed.
    """
    input_path = str(input_path)

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    # Parse date filters
    start_filter = None
    end_filter = None
    if start_date:
        start_filter = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end_filter = datetime.strptime(end_date, "%Y-%m-%d").date()

    # Parse samples
    samples = parse_samples(input_path)
    if not samples:
        print("No sleep_analysis samples found in input file.")
        return 0, 0, 0

    # Bucket by night and source
    buckets = bucket_by_night_and_source(samples)

    # Filter by date range
    if start_filter or end_filter:
        filtered = {}
        for (night, source), night_samples in buckets.items():
            if start_filter and night < start_filter:
                continue
            if end_filter and night > end_filter:
                continue
            filtered[(night, source)] = night_samples
        buckets = filtered

    if not buckets:
        print("No nights to process after filtering.")
        return 0, 0, 0

    # Acquire file lock to prevent concurrent inserts (e.g., watcher + manual run)
    lock_path = Path(__file__).resolve().parent / ".updater.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
    except OSError as e:
        raise OSError(f"Could not acquire lock: {e}") from e

    try:
        # Fetch existing records for dedup
        if not dry_run:
            existing = fetch_existing_records()
        else:
            existing = set()

        # Process each night
        inserted = 0
        skipped = 0
        errors = 0
        to_insert = []

        for (night, source) in sorted(buckets.keys()):
            night_samples = buckets[(night, source)]
            night_str = night.isoformat()

            # Dedup check
            if (night_str, source) in existing:
                print(f"[SKIP]   {night_str}  already exists ({source})")
                skipped += 1
                continue

            try:
                # Resolve timeline
                timeline = resolve_timeline(night_samples)
                if not timeline:
                    continue

                # Check if there are any sleep segments
                has_sleep = any(st in SLEEP_STATES for _, _, st in timeline)
                if not has_sleep:
                    continue

                metrics = compute_metrics(night_samples, timeline)
                record = build_airtable_record(night, source, metrics)

                total_h = metrics["total_sleep"].total_seconds() / 3600
                rem_m = round(metrics["rem"].total_seconds() / 60)
                core_m = round(metrics["core"].total_seconds() / 60)
                deep_m = round(metrics["deep"].total_seconds() / 60)

                detail = f"{total_h:.2f}h sleep"
                if rem_m > 0:
                    detail += f" | REM {rem_m}min"
                if core_m > 0:
                    detail += f" | Core {core_m}min"
                if deep_m > 0:
                    detail += f" | Deep {deep_m}min"

                if dry_run:
                    print(f"[DRY-RUN] {night_str}  {detail}  ({source})")
                    print(f"          Record: {json.dumps(record, indent=2, default=str)}")
                else:
                    to_insert.append((night_str, source, detail, record))

            except Exception as e:
                print(f"[ERROR]  {night_str}  {e}")
                errors += 1

        # Batch insert
        if not dry_run and to_insert:
            records = [r for _, _, _, r in to_insert]
            insert_errors = insert_records(records)

            if insert_errors:
                for err in insert_errors:
                    print(f"[ERROR]  Airtable insert: {err}")
                errors += len(insert_errors)
            else:
                for night_str, source, detail, _ in to_insert:
                    print(f"[INSERT] {night_str}  {detail}  ({source})")
                    inserted += 1

        total_processed = inserted + skipped + errors
        if dry_run:
            total_processed = len(buckets)
            print(f"\nDry run. Processed {total_processed} nights: {total_processed - skipped} would be inserted, {skipped} skipped.")
        else:
            print(f"\nDone. Processed {total_processed} nights: {inserted} inserted, {skipped} skipped, {errors} errors.")

        return inserted, skipped, errors

    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def main():
    """CLI entry point. Parses arguments and delegates to process_file()."""
    parser = argparse.ArgumentParser(description="Parse sleep data and upload to Airtable")
    parser.add_argument("--input", required=True, help="Path to Health Auto Export JSON file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to Airtable")
    parser.add_argument("--start-date", help="Filter: start date (YYYY-MM-DD, inclusive)")
    parser.add_argument("--end-date", help="Filter: end date (YYYY-MM-DD, inclusive)")
    args = parser.parse_args()

    try:
        return process_file(
            args.input,
            dry_run=args.dry_run,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except EnvironmentError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch existing records: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
