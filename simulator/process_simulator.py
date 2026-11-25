"""Deterministic process-line simulator behind the ``site_snapshot`` fixture.

Models a small, invented site the way a telemetry snapshot endpoint might
expose it: a single OData row whose ``Data`` field is a *stringified* JSON
document holding every line and piece of equipment on the site.

    {
      "@odata.context": ".../Telemetry/$metadata#Snapshots",
      "value": [
        {"SnapshotId": 1, "Source": "SITE-A", "LastUpdated": "...",
         "Data": "{\\n  \\"Lines\\": [ ... ], \\"SiteId\\": \\"SITE-A\\"}"}
      ]
    }

Each line is a tank filled by a pump and drained by a steady outflow:

* the pump starts when the tank level falls below ``PUMP_START_LEVEL`` and
  stops above ``PUMP_STOP_LEVEL``; while it runs it reports flow, discharge
  pressure and motor current, and accumulates ``TotalVolume``
* the tank reports level and a temperature that follows a daily cycle
* ``Line1`` also has an outlet valve whose position wanders and sets the
  outflow. ``Line2`` has no valve - a deliberate asymmetry that exercises the
  mapping's key-discovery (``from``) path.

**Everything here is invented.** Site, line and equipment names are generic
examples.

Time model
----------

Simulated state advances on a fixed 15-second **tick** (``TICK_SECONDS``).
``snapshot(tick)`` steps the model forward to that tick and renders the
payload, so every request landing inside the same 15-second window gets a
byte-identical response.

The model is deterministic: same ``seed`` + same ``start_tick`` + same
sequence of ``snapshot`` calls => same output. That is what lets
``tests/fixtures/payloads/site_snapshot.json`` be a checked-in snapshot of
this simulator rather than a hand-written file.
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from typing import Any

TICK_SECONDS = 15
"""Simulated values only change on a boundary of this many seconds."""

MAX_CATCHUP_TICKS = 240
"""Cap on how many ticks a single ``snapshot()`` will step (1 hour)."""

ODATA_CONTEXT = "http://odata.example.local:4444/Telemetry/$metadata#Snapshots"
SITE_ID = "SITE-A"

PUMP_START_LEVEL = 35.0
PUMP_STOP_LEVEL = 80.0
LOW_LEVEL_ALARM = 10.0
HIGH_LEVEL_ALARM = 90.0

# The checked-in fixture is a snapshot of this simulator: seed + start tick +
# render tick pin it exactly. `fixture_snapshot()` regenerates it; the tick is
# 2026-08-04T13:05:45Z, warmed up 400 ticks (100 min) from a cold start.
FIXTURE_SEED = 20260804
FIXTURE_START_TICK = 119_056_183
FIXTURE_TICK = 119_056_583


def current_tick(now: datetime | None = None) -> int:
    """Wall-clock tick index - floor(epoch seconds / TICK_SECONDS)."""
    now = now or datetime.now(tz=timezone.utc)
    return int(now.timestamp()) // TICK_SECONDS


def tick_time(tick: int) -> datetime:
    """UTC datetime at the start of ``tick``."""
    return datetime.fromtimestamp(tick * TICK_SECONDS, tz=timezone.utc)


def _dotnet_iso(dt: datetime, sub_ticks: int) -> str:
    """Format like .NET's round-trip serializer: 7 fractional digits + ``Z``.

    OData endpoints backed by .NET emit 100-nanosecond precision; reproducing
    it here keeps downstream timestamp handling honest. ``sub_ticks`` is the
    fractional part in 100 ns units.
    """
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{sub_ticks % 10_000_000:07d}Z"


def _r(value: float, places: int = 3) -> float:
    return round(float(value), places)


_LINE_SPECS: list[dict[str, Any]] = [
    {"line": "Line1", "tank": "Tank01", "pump": "Pump01", "valve": "Valve01",
     "pump_flow": 14.0, "base_outflow": 9.0},
    {"line": "Line2", "tank": "Tank02", "pump": "Pump02", "valve": None,
     "pump_flow": 11.0, "base_outflow": 7.5},
]

TANK_CAPACITY_M3 = 2.5


class _Line:
    """One tank, its fill pump and (optionally) its outlet valve."""

    def __init__(self, spec: dict[str, Any], rng: random.Random) -> None:
        self.line_id: str = spec["line"]
        self.tank_id: str = spec["tank"]
        self.pump_id: str = spec["pump"]
        self.valve_id: str | None = spec["valve"]
        self.pump_flow: float = spec["pump_flow"]
        self.base_outflow: float = spec["base_outflow"]

        self.level = rng.uniform(30.0, 70.0)
        self.pump_running = int(rng.random() < 0.5)
        self.total_volume = _r(rng.uniform(1_000.0, 5_000.0), 1)
        self.valve_position = rng.uniform(40.0, 60.0) if self.valve_id else None
        self.flow = 0.0
        self.pressure = 0.2
        self.current = 0.0

    def outflow(self) -> float:
        """m3/h leaving the tank; the valve scales it on lines that have one."""
        if self.valve_position is None:
            return self.base_outflow
        return self.base_outflow * (0.5 + self.valve_position / 100.0)

    def step(self, rng: random.Random) -> None:
        if self.valve_position is not None:
            self.valve_position = min(90.0, max(20.0, self.valve_position + rng.uniform(-3.0, 3.0)))

        if self.level < PUMP_START_LEVEL:
            self.pump_running = 1
        elif self.level > PUMP_STOP_LEVEL:
            self.pump_running = 0

        if self.pump_running:
            self.flow = max(0.0, self.pump_flow + rng.uniform(-0.6, 0.6))
            self.pressure = 3.0 + rng.uniform(-0.15, 0.15)
            self.current = 7.5 + self.flow * 0.08 + rng.uniform(-0.2, 0.2)
        else:
            self.flow = 0.0
            self.pressure = 0.2 + rng.uniform(-0.05, 0.05)
            self.current = 0.0

        hours = TICK_SECONDS / 3600.0
        self.total_volume += self.flow * hours
        net_m3 = (self.flow - self.outflow()) * hours
        self.level = min(100.0, max(0.0, self.level + 100.0 * net_m3 / TANK_CAPACITY_M3))

    def render(self, ambient: float, rng: random.Random) -> dict[str, Any]:
        equipment: list[dict[str, Any]] = [
            {
                "EquipmentId": self.tank_id,
                "Type": "Tank",
                "Readings": {
                    "Level": _r(self.level),
                    "Temperature": _r(ambient + 2.5 + rng.uniform(-0.3, 0.3)),
                },
                "Status": {
                    "HighLevelAlarm": int(self.level > HIGH_LEVEL_ALARM),
                    "LowLevelAlarm": int(self.level < LOW_LEVEL_ALARM),
                },
            },
            {
                "EquipmentId": self.pump_id,
                "Type": "Pump",
                "Readings": {
                    "DischargePressure": _r(self.pressure),
                    "FlowRate": _r(self.flow),
                    "MotorCurrent": _r(self.current),
                    "TotalVolume": _r(self.total_volume, 1),
                },
                "Status": {"Running": self.pump_running},
            },
        ]
        if self.valve_id is not None:
            equipment.append(
                {
                    "EquipmentId": self.valve_id,
                    "Type": "Valve",
                    "Readings": {"Position": _r(self.valve_position)},
                    "Status": {"Open": int(self.valve_position > 0)},
                }
            )
        return {"Equipment": equipment, "LineId": self.line_id}


class ProcessSimulator:
    """Tick-driven process-line model rendering the ``Snapshots`` payload."""

    def __init__(self, seed: int = FIXTURE_SEED, start_tick: int | None = None) -> None:
        self.seed = seed
        self._tick = current_tick() if start_tick is None else start_tick
        rng = random.Random(seed)
        self.lines = [_Line(spec, rng) for spec in _LINE_SPECS]

    # -- time ----------------------------------------------------------------

    def _step_rng(self, tick: int, stream: int = 0) -> random.Random:
        """A fresh RNG for one tick of one line, so lines don't move in lockstep."""
        return random.Random((self.seed * 1_000_003) ^ (tick * 977 + stream * 31))

    def advance_to(self, tick: int) -> None:
        """Step the model forward to ``tick``. Backwards jumps are ignored."""
        if tick <= self._tick:
            return
        steps = min(tick - self._tick, MAX_CATCHUP_TICKS)
        for offset in range(steps):
            step_tick = tick - steps + offset + 1
            for stream, line in enumerate(self.lines, start=1):
                line.step(self._step_rng(step_tick, stream))
        self._tick = tick

    def _ambient(self, tick: int) -> float:
        """Daily outdoor temperature in degrees C, peaking mid-afternoon."""
        seconds_of_day = (tick * TICK_SECONDS) % 86_400
        daily = math.sin(2 * math.pi * (seconds_of_day - 34_200) / 86_400)
        noise = self._step_rng(tick).uniform(-0.3, 0.3)
        return _r(18.0 + 6.0 * daily + noise)

    # -- rendering -----------------------------------------------------------

    def data_document(self, tick: int) -> dict[str, Any]:
        """The inner (pre-stringification) site document at ``tick``."""
        ambient = self._ambient(tick)
        return {
            "Lines": [
                line.render(ambient, self._step_rng(tick, 100 + n))
                for n, line in enumerate(self.lines)
            ],
            "SiteId": SITE_ID,
        }

    def snapshot(self, tick: int | None = None) -> dict[str, Any]:
        """Advance to ``tick`` (default: now) and render the OData payload."""
        tick = current_tick() if tick is None else tick
        self.advance_to(tick)
        # The model never rewinds, so a backwards clock (NTP step, a test
        # asking for an earlier tick) re-serves the current state rather than
        # pairing fresh state with a stale timestamp.
        tick = self._tick

        rng = self._step_rng(tick)
        # The row is written a fraction of a second after the tick boundary,
        # as a real collector would be.
        sub_ticks = rng.randrange(800_000, 9_500_000)
        return {
            "@odata.context": ODATA_CONTEXT,
            "value": [
                {
                    "SnapshotId": 1,
                    "Source": SITE_ID,
                    "LastUpdated": _dotnet_iso(tick_time(tick), sub_ticks),
                    "Data": json.dumps(self.data_document(tick), indent=2, sort_keys=True),
                }
            ],
        }


def fixture_snapshot() -> dict[str, Any]:
    """The canonical, reproducible payload checked in as the test fixture.

    Regenerate ``tests/fixtures/payloads/site_snapshot.json`` with::

        python3 -c "import sys, json; sys.path.insert(0, 'simulator'); \\
            import process_simulator as p; \\
            print(json.dumps(p.fixture_snapshot(), indent=2))" \\
            > tests/fixtures/payloads/site_snapshot.json

    Changing the model changes this payload - regenerate the expected
    readings alongside it (see ``docs/rest-testing.rst``).
    """
    sim = ProcessSimulator(seed=FIXTURE_SEED, start_tick=FIXTURE_START_TICK)
    return sim.snapshot(FIXTURE_TICK)


if __name__ == "__main__":  # pragma: no cover - manual inspection
    print(json.dumps(ProcessSimulator().snapshot(), indent=2))
