#!/usr/bin/env python3
"""Fetch sleep periods from the Oura API v2 and insert them into Airtable.

Sibling of sleep_airtable_updater.py, which imports Health Auto Export files.
Both write Source "Oura", so the shared (Night, Source) dedup keeps them from
writing the same night twice. Insert only: existing rows are never changed.

Commands:
    oura_sync.py auth-url              print the Oura consent URL
    oura_sync.py auth-code <url>       finish login with the redirected URL
    oura_sync.py sync [--dry-run] [--start-date D] [--end-date D]

Secrets live in the login keychain, service "oura-api", accounts
client_id, client_secret, refresh_token and access_token. Oura refresh tokens
are single use, so a new one is saved before anything else happens.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import sleep_airtable_updater as updater  # noqa: E402

AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
API_BASE = "https://api.ouraring.com/v2/usercollection"
REDIRECT_URI = "http://localhost:8765/callback"
SCOPES = "daily heartrate"

KEYCHAIN_SERVICE = "oura-api"
STATE_DIR = Path.home() / ".local/state/oura-sync"
PKCE_FILE = STATE_DIR / "pkce.json"

TIMEOUT = 30
DEFAULT_LOOKBACK_DAYS = 7
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# Sleep-table fields the export path never fills
FLD_RESTING_HR = "fldpSlAtSm0afXeqc"
FLD_HRV = "fld42Oum8WJHAJiwF"
FLD_RESP_RATE = "fldBvnsCyHhonEZLt"
FLD_SLEEP_SCORE = "fldjy6HX3QohYLhqg"


# --- keychain ---------------------------------------------------------------


def keychain_get(account):
    """Return a keychain value, or None if the item does not exist."""
    result = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            account,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def keychain_set(account, value):
    """Create or replace a keychain value.

    Goes through `security -i` on stdin so the value never appears in the
    process list, and never through the bare -w prompt, which truncates.
    """
    if any(c in value for c in '"\\\n'):
        raise ValueError(f"refusing to store {account}: unexpected characters")
    command = (
        f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a {account} -w "{value}"\n'
    )
    result = subprocess.run(
        ["security", "-i"], input=command, capture_output=True, text=True
    )
    if result.returncode != 0 or keychain_get(account) != value:
        raise RuntimeError(
            f"could not save {account} to the keychain: {result.stderr.strip()}"
        )


def client_credentials():
    client_id = keychain_get("client_id")
    client_secret = keychain_get("client_secret")
    if not client_id or not client_secret:
        raise EnvironmentError(
            f"Oura client_id/client_secret missing from keychain service {KEYCHAIN_SERVICE}."
        )
    return client_id, client_secret


# --- OAuth ------------------------------------------------------------------


def auth_url():
    """Build the consent URL and save the PKCE verifier and state for auth-code."""
    client_id, _ = client_credentials()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=")
    state = secrets.token_urlsafe(16)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PKCE_FILE.write_text(json.dumps({"verifier": verifier, "state": state}))
    os.chmod(PKCE_FILE, 0o600)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge.decode(),
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def save_tokens(payload):
    """Save tokens from a token response: refresh token first, it is single use."""
    keychain_set("refresh_token", payload["refresh_token"])
    expires_at = int(time.time()) + int(payload.get("expires_in", 0)) - 300
    keychain_set("access_token", f"{payload['access_token']}|{expires_at}")


def token_request(data):
    client_id, client_secret = client_credentials()
    resp = requests.post(
        TOKEN_URL,
        data={**data, "client_id": client_id, "client_secret": client_secret},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Oura token request failed: {resp.status_code} {resp.text[:300]}"
        )
    return resp.json()


def auth_code(redirected_url):
    """Exchange the code in the redirected URL for tokens."""
    query = parse_qs(urlparse(redirected_url).query)
    if "error" in query:
        raise RuntimeError(f"Oura returned an error: {query['error'][0]}")
    pkce = json.loads(PKCE_FILE.read_text())
    if query.get("state", [""])[0] != pkce["state"]:
        raise RuntimeError("state does not match; run auth-url again")

    payload = token_request(
        {
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": pkce["verifier"],
        }
    )
    save_tokens(payload)
    PKCE_FILE.unlink()


def access_token():
    """Return a valid access token, refreshing it only when it has expired."""
    cached = keychain_get("access_token")
    if cached and "|" in cached:
        token, expires_at = cached.rsplit("|", 1)
        if time.time() < int(expires_at):
            return token

    refresh = keychain_get("refresh_token")
    if not refresh:
        raise EnvironmentError(
            "No Oura refresh token. Run auth-url and auth-code first."
        )
    payload = token_request({"grant_type": "refresh_token", "refresh_token": refresh})
    save_tokens(payload)
    return payload["access_token"]


# --- API --------------------------------------------------------------------


def fetch_collection(name, start, end, token):
    """Fetch every document in a usercollection between two dates (inclusive)."""
    docs = []
    params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
    while True:
        resp = requests.get(
            f"{API_BASE}/{name}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Oura {name} request failed: {resp.status_code} {resp.text[:300]}"
            )
        body = resp.json()
        docs.extend(body.get("data", []))
        if not body.get("next_token"):
            return docs
        params["next_token"] = body["next_token"]


def main_sleep_by_day(periods):
    """Keep one long_sleep period per day, the longest if there are several."""
    by_day = {}
    for p in periods:
        if p.get("type") != "long_sleep":
            continue
        day = p["day"]
        if day in by_day:
            print(
                f"[WARN]   {day}  more than one long_sleep period; keeping the longest"
            )
            if (p.get("total_sleep_duration") or 0) <= (
                by_day[day].get("total_sleep_duration") or 0
            ):
                continue
        by_day[day] = p
    return by_day


def to_local_string(iso_value):
    return (
        datetime.fromisoformat(iso_value)
        .astimezone(LOCAL_TZ)
        .strftime("%Y-%m-%dT%H:%M:%S")
    )


def build_record(period, sleep_score=None):
    """Map an Oura sleep period to an Airtable Sleep record."""
    fields = {
        updater.FLD_NIGHT: period["day"],
        updater.FLD_SOURCE: "Oura",
    }

    start = datetime.fromisoformat(period["bedtime_start"])
    start += timedelta(seconds=period.get("latency") or 0)
    fields[updater.FLD_SLEEP_START] = start.astimezone(LOCAL_TZ).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    fields[updater.FLD_SLEEP_END] = to_local_string(period["bedtime_end"])

    fields[updater.FLD_TIME_ASLEEP] = round(
        (period.get("total_sleep_duration") or 0) / 3600, 2
    )
    if period.get("time_in_bed"):
        fields[updater.FLD_TIME_IN_BED] = round(period["time_in_bed"] / 3600, 2)

    minutes = {
        updater.FLD_AWAKE: period.get("awake_time"),
        updater.FLD_REM: period.get("rem_sleep_duration"),
        updater.FLD_CORE: period.get("light_sleep_duration"),
        updater.FLD_DEEP: period.get("deep_sleep_duration"),
    }
    for field_id, seconds in minutes.items():
        if seconds:
            fields[field_id] = round(seconds / 60)

    if period.get("lowest_heart_rate") is not None:
        fields[FLD_RESTING_HR] = period["lowest_heart_rate"]
    if period.get("average_hrv") is not None:
        fields[FLD_HRV] = period["average_hrv"]
    if period.get("average_breath") is not None:
        fields[FLD_RESP_RATE] = round(period["average_breath"], 1)
    if sleep_score is not None:
        fields[FLD_SLEEP_SCORE] = sleep_score

    return {"fields": fields}


def sync(start, end, dry_run=False):
    """Insert Oura sleep for each day in [start, end] that the table lacks."""
    token = access_token()
    # Oura files a night under the day you wake up; ask for one extra day on
    # each side so a boundary night is never cut off, then filter.
    periods = fetch_collection(
        "sleep", start - timedelta(days=1), end + timedelta(days=1), token
    )
    scores = {
        d["day"]: d.get("score")
        for d in fetch_collection("daily_sleep", start, end + timedelta(days=1), token)
    }

    by_day = {
        day: p
        for day, p in main_sleep_by_day(periods).items()
        if start.isoformat() <= day <= end.isoformat()
    }
    existing = updater.fetch_existing_records()

    to_insert = []
    skipped = 0
    for day in sorted(by_day):
        if (day, "Oura") in existing:
            print(f"[SKIP]   {day}  already exists (Oura)")
            skipped += 1
            continue
        record = build_record(by_day[day], scores.get(day))
        hours = record["fields"][updater.FLD_TIME_ASLEEP]
        to_insert.append((day, hours, record))
        tag = "[DRY-RUN]" if dry_run else "[NEW]   "
        print(f"{tag} {day}  {hours:.2f}h sleep  score {scores.get(day)}")
        if dry_run:
            print(f"          Record: {json.dumps(record)}")

    if dry_run:
        print(
            f"\nDry run. {len(by_day)} nights: {len(to_insert)} would be inserted, {skipped} skipped."
        )
        return 0

    errors = updater.insert_records([r for _, _, r in to_insert]) if to_insert else []
    for err in errors:
        print(f"[ERROR]  Airtable insert: {err}")
    inserted = len(to_insert) if not errors else 0
    print(
        f"\nDone. {len(by_day)} nights: {inserted} inserted, {skipped} skipped, {len(errors)} errors."
    )
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description="Sync Oura sleep into Airtable")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("auth-url", help="Print the Oura consent URL")
    code = sub.add_parser("auth-code", help="Finish login with the redirected URL")
    code.add_argument("url")
    s = sub.add_parser("sync", help="Insert missing nights")
    s.add_argument(
        "--dry-run", action="store_true", help="Fetch and preview, no Airtable writes"
    )
    s.add_argument(
        "--start-date", help=f"YYYY-MM-DD (default: {DEFAULT_LOOKBACK_DAYS} days ago)"
    )
    s.add_argument("--end-date", help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    try:
        if args.command == "auth-url":
            print(auth_url())
            return 0
        if args.command == "auth-code":
            auth_code(args.url)
            print("Oura login saved.")
            return 0

        today = datetime.now(LOCAL_TZ).date()
        end = date.fromisoformat(args.end_date) if args.end_date else today
        start = (
            date.fromisoformat(args.start_date)
            if args.start_date
            else end - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        )
        return sync(start, end, dry_run=args.dry_run)
    except (EnvironmentError, RuntimeError, ValueError, requests.RequestException) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
