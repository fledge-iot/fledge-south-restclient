"""Long-lived Flask host that serves simulated time-series data shaped
like the plugin's fixture payloads.

Everything this host serves moves on a fixed **15-second tick**
(``process_simulator.TICK_SECONDS``). Requests landing inside the same tick
get a byte-identical response; values only change when the tick rolls
over. That mirrors a real cache endpoint with a poll rate, and
it means a south service polling faster than 15 s sees repeats rather
than noise.

Two kinds of endpoint:

* ``site_snapshot`` - driven by ``simulator/process_simulator.py``, a
  stateful model of two tank-and-pump lines. Values evolve from tick to
  tick: tank levels rise and fall, pumps start and stop, totals
  accumulate. This is an OData ``Snapshots`` collection with a stringified
  ``Data`` field.
* every other fixture under ``tests/fixtures/payloads/`` - loaded once at
  startup as a template and re-rendered per tick with:

  - every ISO 8601 timestamp shifted so the latest one in the document
    lines up with the current tick boundary (preserves intra-record
    offsets like ``StartTime`` relative to ``LastUpdated``)
  - every numeric value lightly jittered around its template value
    (booleans are left alone)
  - stringified JSON fields (the OData ``Data`` field) recursively
    transformed and re-serialized in the same compact form

Routes:

* ``GET /<name>.json`` - render fixture ``<name>``
* ``GET /``            - list available fixtures

See ``docs/rest-testing.rst`` for the full walkthrough.
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import flask

_SIM_DIR = Path(__file__).resolve().parent
if str(_SIM_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_DIR))

from process_simulator import TICK_SECONDS, ProcessSimulator, current_tick, tick_time

PAYLOADS = _SIM_DIR.parent / "tests" / "fixtures" / "payloads"

#: Fixture name served by the stateful process model rather than the generic
#: jitter renderer.
SIMULATED_FIXTURE = "site_snapshot"

ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)


def _parse_iso(value: str) -> datetime | None:
    if not ISO_RE.match(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_iso(dt: datetime, original: str) -> str:
    dt = dt.astimezone(timezone.utc)
    millis = dt.microsecond // 1000
    if original.endswith("Z"):
        return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"
    return dt.isoformat(timespec="milliseconds")


def _collect_timestamps(node: Any, out: list[datetime]) -> None:
    if isinstance(node, dict):
        for v in node.values():
            _collect_timestamps(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_timestamps(v, out)
    elif isinstance(node, str):
        ts = _parse_iso(node)
        if ts is not None:
            out.append(ts)
            return
        stripped = node.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                _collect_timestamps(json.loads(node), out)
            except (json.JSONDecodeError, ValueError):
                pass


def _jitter_float(value: float, rng: random.Random) -> float:
    span = max(abs(value) * 0.02, 0.05)
    return round(value + rng.uniform(-span, span), 4)


def _jitter_int(value: int, rng: random.Random) -> int:
    if value == 0:
        return rng.choice((-1, 0, 1))
    return value + rng.randint(-1, 1)


def _transform(node: Any, delta: timedelta, rng: random.Random) -> Any:
    if isinstance(node, dict):
        return {k: _transform(v, delta, rng) for k, v in node.items()}
    if isinstance(node, list):
        return [_transform(v, delta, rng) for v in node]
    if isinstance(node, bool):
        return node
    if isinstance(node, int):
        return _jitter_int(node, rng)
    if isinstance(node, float):
        return _jitter_float(node, rng)
    if isinstance(node, str):
        ts = _parse_iso(node)
        if ts is not None:
            return _format_iso(ts + delta, node)
        stripped = node.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                inner = json.loads(node)
            except (json.JSONDecodeError, ValueError):
                return node
            transformed = _transform(inner, delta, rng)
            return json.dumps(transformed, separators=(",", ":"))
        return node
    return node


def _render(name: str, template: Any, tick: int) -> Any:
    """Re-render ``template`` for ``tick``.

    Seeded on ``(name, tick)`` so every request inside a tick renders
    identically, and so two fixtures don't jitter in lockstep.
    """
    rng = random.Random(f"{name}:{tick}")
    stamps: list[datetime] = []
    _collect_timestamps(template, stamps)
    if stamps:
        anchor = max(stamps).astimezone(timezone.utc)
        delta = tick_time(tick) - anchor
    else:
        delta = timedelta(0)
    return _transform(copy.deepcopy(template), delta, rng)


def _load_templates() -> dict[str, Any]:
    return {
        path.stem: json.loads(path.read_text())
        for path in PAYLOADS.glob("*.json")
    }


TEMPLATES = _load_templates()

# SIM_SEED chooses the starting state (tank levels, which pumps are running).
# Unset uses the model's default seed, so every start looks the same.
_SEED = os.environ.get("SIM_SEED") or None
MODEL = ProcessSimulator(seed=int(_SEED)) if _SEED else ProcessSimulator()
# The simulator is stateful and Flask's dev server is threaded, so concurrent
# pollers must not interleave inside advance_to().
MODEL_LOCK = threading.Lock()

app = flask.Flask(__name__)


@app.route("/<name>.json")
@app.route("/<name>")
def serve(name: str):
    tick = current_tick()
    if name == SIMULATED_FIXTURE:
        with MODEL_LOCK:
            payload = MODEL.snapshot(tick)
    else:
        template = TEMPLATES.get(name)
        if template is None:
            return flask.jsonify({"error": "not found"}), 404
        payload = _render(name, template, tick)
    return app.response_class(json.dumps(payload), mimetype="application/json")


@app.route("/")
def index():
    return flask.jsonify(
        {
            "fixtures": sorted(set(TEMPLATES) | {SIMULATED_FIXTURE}),
            "simulated": SIMULATED_FIXTURE,
            "tickSeconds": TICK_SECONDS,
            "tick": current_tick(),
        }
    )


if __name__ == "__main__":
    # In Docker the app is served by gunicorn (see Dockerfile) - this path is
    # the direct `python3 simulator/serve_fixtures.py` run.
    app.run(
        host=os.environ.get("SIM_HOST", "0.0.0.0"),
        port=int(os.environ.get("SIM_PORT", "4444")),
        debug=False,
    )
