import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import oura_sync  # noqa: E402
import sleep_airtable_updater as updater  # noqa: E402


def period(**overrides):
    base = {
        "day": "2026-10-19",
        "type": "long_sleep",
        "bedtime_start": "2026-10-18T22:30:00-07:00",
        "bedtime_end": "2026-10-19T06:00:00-07:00",
        "latency": 600,
        "total_sleep_duration": 24000,
        "time_in_bed": 27000,
        "awake_time": 3000,
        "rem_sleep_duration": 5400,
        "light_sleep_duration": 13200,
        "deep_sleep_duration": 5400,
        "lowest_heart_rate": 52,
        "average_hrv": 41,
        "average_breath": 14.625,
    }
    base.update(overrides)
    return base


def test_build_record_maps_every_field():
    fields = oura_sync.build_record(period(), sleep_score=81)["fields"]
    assert fields[updater.FLD_NIGHT] == "2026-10-19"
    assert fields[updater.FLD_SOURCE] == "Oura"
    assert fields[updater.FLD_SLEEP_START] == "2026-10-18T22:40:00"  # bedtime + latency
    assert fields[updater.FLD_SLEEP_END] == "2026-10-19T06:00:00"
    assert fields[updater.FLD_TIME_ASLEEP] == 6.67
    assert fields[updater.FLD_TIME_IN_BED] == 7.5
    assert fields[updater.FLD_AWAKE] == 50
    assert fields[updater.FLD_REM] == 90
    assert fields[updater.FLD_CORE] == 220
    assert fields[updater.FLD_DEEP] == 90
    assert fields[oura_sync.FLD_RESTING_HR] == 52
    assert fields[oura_sync.FLD_HRV] == 41
    assert fields[oura_sync.FLD_RESP_RATE] == 14.6
    assert fields[oura_sync.FLD_SLEEP_SCORE] == 81


def test_build_record_converts_other_zones_to_pacific():
    fields = oura_sync.build_record(
        period(bedtime_start="2026-10-19T01:30:00-04:00", latency=0)
    )["fields"]
    assert fields[updater.FLD_SLEEP_START] == "2026-10-18T22:30:00"


def test_build_record_leaves_missing_values_out():
    fields = oura_sync.build_record(
        period(
            lowest_heart_rate=None,
            average_hrv=None,
            average_breath=None,
            rem_sleep_duration=None,
        )
    )["fields"]
    for field_id in (
        oura_sync.FLD_RESTING_HR,
        oura_sync.FLD_HRV,
        oura_sync.FLD_RESP_RATE,
        oura_sync.FLD_SLEEP_SCORE,
        updater.FLD_REM,
    ):
        assert field_id not in fields


def test_main_sleep_skips_naps_and_keeps_longest():
    periods = [
        period(type="late_nap", total_sleep_duration=1800),
        period(total_sleep_duration=20000),
        period(total_sleep_duration=25000),
        period(day="2026-10-18", type="deleted"),
    ]
    by_day = oura_sync.main_sleep_by_day(periods)
    assert list(by_day) == ["2026-10-19"]
    assert by_day["2026-10-19"]["total_sleep_duration"] == 25000


def test_save_tokens_writes_refresh_token_first(monkeypatch):
    calls = []
    monkeypatch.setattr(
        oura_sync, "keychain_set", lambda account, value: calls.append(account)
    )
    oura_sync.save_tokens(
        {"access_token": "a", "refresh_token": "r", "expires_in": 86400}
    )
    assert calls == ["refresh_token", "access_token"]


def test_keychain_set_rejects_quotes():
    try:
        oura_sync.keychain_set("refresh_token", 'bad"value')
    except ValueError:
        return
    raise AssertionError("expected ValueError")
