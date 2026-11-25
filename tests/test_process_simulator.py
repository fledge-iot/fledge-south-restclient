"""Tests for the process-line simulator behind the site_snapshot fixture.

The simulator is what makes ``site_snapshot`` a *moving* endpoint rather
than a static file, so the properties worth pinning are the ones the rest
of the testing story leans on:

* deterministic - same seed + same tick sequence => same payload, which is
  what lets the checked-in fixture be a snapshot of the model
* quantized - values only change on a 15-second tick boundary
* live - consecutive ticks actually move
* structurally stable - the payload keeps the shape the mapping document
  (and any downstream consumer) is written against
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SIMULATOR_DIR = REPO_ROOT / "simulator"
if str(SIMULATOR_DIR) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_DIR))

import process_simulator as sim  # noqa: E402

PAYLOAD_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "payloads" / "site_snapshot.json"

START_TICK = sim.FIXTURE_START_TICK


def _new(seed: int = sim.FIXTURE_SEED):
    return sim.ProcessSimulator(seed=seed, start_tick=START_TICK)


def _data(payload: dict) -> dict:
    """Parse the stringified site document out of the snapshot row."""
    return json.loads(payload["value"][0]["Data"])


def _equipment(payload: dict, kind: str) -> list[dict]:
    return [
        item
        for line in _data(payload)["Lines"]
        for item in line["Equipment"]
        if item["Type"] == kind
    ]


# --- determinism ------------------------------------------------------------


def test_checked_in_fixture_matches_the_simulator():
    """The fixture is a snapshot of this model - regenerate it if this fails.

    See ``process_simulator.fixture_snapshot`` for the regeneration command.
    """
    assert json.loads(PAYLOAD_FIXTURE.read_text()) == sim.fixture_snapshot()


def test_same_seed_and_tick_sequence_reproduce_the_payload():
    assert _new().snapshot(START_TICK + 37) == _new().snapshot(START_TICK + 37)


def test_different_seeds_diverge():
    assert _new(seed=1).snapshot(START_TICK + 37) != _new(seed=2).snapshot(START_TICK + 37)


def test_stepping_tick_by_tick_matches_a_single_jump():
    """Catch-up after an idle period lands where continuous polling would."""
    stepped = _new()
    for tick in range(START_TICK + 1, START_TICK + 21):
        stepped.snapshot(tick)
    jumped = _new().snapshot(START_TICK + 20)
    assert stepped.snapshot(START_TICK + 20) == jumped


# --- quantization -----------------------------------------------------------


def test_values_are_stable_within_a_tick():
    site = _new()
    first = site.snapshot(START_TICK + 10)
    assert site.snapshot(START_TICK + 10) == first


def test_clock_going_backwards_does_not_rewind_the_model():
    site = _new()
    ahead = site.snapshot(START_TICK + 10)
    assert site.snapshot(START_TICK + 5) == ahead


def test_current_tick_advances_once_per_tick_seconds():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 8, 4, 13, 5, 45, tzinfo=timezone.utc)
    assert sim.current_tick(now) == sim.current_tick(now + timedelta(seconds=14))
    assert sim.current_tick(now + timedelta(seconds=15)) == sim.current_tick(now) + 1


def test_last_updated_tracks_the_tick_boundary():
    payload = _new().snapshot(START_TICK + 3)
    stamp = payload["value"][0]["LastUpdated"]
    boundary = sim.tick_time(START_TICK + 3).strftime("%Y-%m-%dT%H:%M:%S")
    assert stamp.startswith(boundary)
    # .NET round-trip format: 7 fractional digits, Zulu.
    assert len(stamp) == len(boundary) + len(".0000000Z")
    assert stamp.endswith("Z")


def test_consecutive_ticks_move():
    site = _new()
    a = site.snapshot(START_TICK + 10)
    b = site.snapshot(START_TICK + 11)
    assert a != b


# --- payload shape ----------------------------------------------------------


def test_odata_envelope_shape():
    payload = _new().snapshot(START_TICK + 5)
    assert payload["@odata.context"].endswith("#Snapshots")
    assert len(payload["value"]) == 1
    row = payload["value"][0]
    assert set(row) == {"SnapshotId", "Source", "LastUpdated", "Data"}
    assert isinstance(row["Data"], str), "Data must stay a stringified JSON document"


def test_lines_and_equipment():
    data = _data(_new().snapshot(START_TICK + 5))
    assert data["SiteId"] == sim.SITE_ID
    assert [ln["LineId"] for ln in data["Lines"]] == ["Line1", "Line2"]
    assert [e["EquipmentId"] for e in data["Lines"][0]["Equipment"]] == ["Tank01", "Pump01", "Valve01"]
    assert [e["EquipmentId"] for e in data["Lines"][1]["Equipment"]] == ["Tank02", "Pump02"]


def test_line_two_has_no_valve():
    """The two lines are deliberately asymmetric, so consumers must discover
    keys rather than assume them."""
    payload = _new().snapshot(START_TICK + 5)
    assert [v["EquipmentId"] for v in _equipment(payload, "Valve")] == ["Valve01"]


def test_value_keys_are_stable_across_ticks():
    site = _new()

    def keys(payload):
        return {
            (line["LineId"], item["EquipmentId"], group, key)
            for line in _data(payload)["Lines"]
            for item in line["Equipment"]
            for group in ("Readings", "Status")
            for key in item.get(group, {})
        }

    assert keys(site.snapshot(START_TICK + 5)) == keys(site.snapshot(START_TICK + 60))


# --- process behavior -------------------------------------------------------


def test_tank_levels_stay_in_range():
    site = _new()
    for tick in range(START_TICK + 1, START_TICK + 200):
        for tank in _equipment(site.snapshot(tick), "Tank"):
            assert 0.0 <= tank["Readings"]["Level"] <= 100.0


def test_pumps_both_run_and_stop():
    site = _new()
    seen = set()
    for tick in range(START_TICK + 1, START_TICK + 400):
        for pump in _equipment(site.snapshot(tick), "Pump"):
            seen.add(pump["Status"]["Running"])
    assert seen == {0, 1}, "expected pumps to both run and sit idle"


def test_stopped_pump_reports_no_flow():
    site = _new()
    for tick in range(START_TICK + 1, START_TICK + 400):
        for pump in _equipment(site.snapshot(tick), "Pump"):
            if not pump["Status"]["Running"]:
                assert pump["Readings"]["FlowRate"] == 0
                assert pump["Readings"]["MotorCurrent"] == 0
                return
    pytest.fail("no pump stopped within 400 ticks")


def test_total_volume_never_goes_backwards():
    site = _new()
    previous: dict[str, float] = {}
    grew = False
    for tick in range(START_TICK + 1, START_TICK + 200):
        for pump in _equipment(site.snapshot(tick), "Pump"):
            total = pump["Readings"]["TotalVolume"]
            prior = previous.get(pump["EquipmentId"])
            if prior is not None:
                assert total >= prior
                grew = grew or total > prior
            previous[pump["EquipmentId"]] = total
    assert grew, "expected at least one pump to accumulate volume"


def test_running_pump_has_discharge_pressure_and_current():
    site = _new()
    for tick in range(START_TICK + 1, START_TICK + 200):
        for pump in _equipment(site.snapshot(tick), "Pump"):
            if pump["Status"]["Running"]:
                assert pump["Readings"]["FlowRate"] > 0
                assert pump["Readings"]["DischargePressure"] > 1.0
                assert pump["Readings"]["MotorCurrent"] > 0
                return
    pytest.fail("no pump ran within 200 ticks")


def test_level_alarms_follow_the_level():
    site = _new()
    for tick in range(START_TICK + 1, START_TICK + 200):
        for tank in _equipment(site.snapshot(tick), "Tank"):
            level = tank["Readings"]["Level"]
            assert tank["Status"]["HighLevelAlarm"] == int(level > sim.HIGH_LEVEL_ALARM)
            assert tank["Status"]["LowLevelAlarm"] == int(level < sim.LOW_LEVEL_ALARM)


def test_valve_position_stays_within_its_limits():
    site = _new()
    for tick in range(START_TICK + 1, START_TICK + 200):
        for valve in _equipment(site.snapshot(tick), "Valve"):
            assert 20.0 <= valve["Readings"]["Position"] <= 90.0
