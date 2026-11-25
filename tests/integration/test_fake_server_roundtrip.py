"""Integration test: real ``requests`` against a local Flask fake.

Unlike the unit suite (which mocks the HTTP transport via ``responses``),
this test exercises the full plugin path against a real socket. Useful
for catching connection-layer regressions (TLS, headers, streaming
behavior) that mock-based tests can miss.

Deselected by default - run with ``pytest -m integration``.
"""
from __future__ import annotations

import copy
import json
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")
from werkzeug.serving import make_server

from python.fledge.plugins.south.restclient import restclient as plugin


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PAYLOADS_DIR = REPO_ROOT / "tests" / "fixtures" / "payloads"
MAPPINGS_DIR = REPO_ROOT / "tests" / "fixtures" / "mappings"
EXPECTED_DIR = REPO_ROOT / "tests" / "fixtures" / "expected"


class _FakeServer:
    """Werkzeug server in a thread, serving fixture payloads.

    Routes: ``/<name>.json`` returns the corresponding fixture payload.
    """

    def __init__(self) -> None:
        app = flask.Flask(__name__)

        @app.route("/<name>.json")
        def serve(name: str):  # type: ignore[no-redef]
            path = PAYLOADS_DIR / f"{name}.json"
            if not path.is_file():
                return flask.jsonify({"error": "not found"}), 404
            return app.response_class(path.read_text(), mimetype="application/json")

        port = _free_port()
        self.host = "127.0.0.1"
        self.port = port
        self.url_base = f"http://{self.host}:{port}"
        self._server = make_server(self.host, port, app)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        # Wait briefly for the server to become reachable
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("fake server failed to start")

    def stop(self) -> None:
        self._server.shutdown()
        self._thread.join(timeout=2.0)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_server():
    server = _FakeServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _build_config(url: str, mapping_text: str) -> dict:
    cfg = copy.deepcopy(plugin._DEFAULT_CONFIG)
    out = {key: {"value": item["default"]} for key, item in cfg.items()}
    out["url"]["value"] = url
    out["mappingSource"]["value"] = "inline"
    out["mappingInline"]["value"] = mapping_text
    out["followPagination"]["value"] = "false"
    return out


def _strip_keys(readings: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "key"} for r in readings]


@pytest.mark.parametrize(
    "fixture_name",
    sorted(p.stem for p in PAYLOADS_DIR.glob("*.json")),
)
def test_fake_server_roundtrip(fake_server, fixture_name):
    url = f"{fake_server.url_base}/{fixture_name}.json"
    mapping_text = (MAPPINGS_DIR / f"{fixture_name}.yaml").read_text()
    expected = json.loads((EXPECTED_DIR / f"{fixture_name}.json").read_text())

    handle = plugin.plugin_init(_build_config(url, mapping_text))
    try:
        readings = plugin.plugin_poll(handle)
    finally:
        plugin.plugin_shutdown(handle)

    for r in readings:
        uuid.UUID(r["key"])
    assert _strip_keys(readings) == expected
