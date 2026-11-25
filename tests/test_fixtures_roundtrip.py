"""Parameterized fixture roundtrip - the primary regression net.

For every fixture under ``tests/fixtures/payloads/``:
  1. Load the payload, the mapping document, and the expected readings.
  2. Build a Fledge-shaped config from ``_DEFAULT_CONFIG`` with the URL
     pointed at a test host and ``mappingInline`` set from the fixture.
  3. Mock the HTTP transport via ``responses`` so the configured URL
     returns the payload.
  4. Run ``plugin_init -> plugin_poll -> plugin_shutdown``.
  5. Strip ``key`` fields, then assert the emitted readings list equals
     the expected list.
  6. Assert each ``key`` parses as a UUID and each ``timestamp`` matches
     an ISO-8601-with-offset regex.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path

import pytest
import responses

from python.fledge.plugins.south.restclient import restclient as plugin


REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOADS_DIR = REPO_ROOT / "tests" / "fixtures" / "payloads"
MAPPINGS_DIR = REPO_ROOT / "tests" / "fixtures" / "mappings"
EXPECTED_DIR = REPO_ROOT / "tests" / "fixtures" / "expected"

ISO8601_TZ = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)$"
)

TEST_URL = "http://test-host.local/api/data"


def _fixture_names() -> list[str]:
    return sorted(p.stem for p in PAYLOADS_DIR.glob("*.json"))


def _load_fixture(name: str) -> dict:
    return {
        "name": name,
        "payload": json.loads((PAYLOADS_DIR / f"{name}.json").read_text()),
        "mapping_text": (MAPPINGS_DIR / f"{name}.yaml").read_text(),
        "expected": json.loads((EXPECTED_DIR / f"{name}.json").read_text()),
    }


def _build_config(mapping_text: str) -> dict:
    """Fledge-shaped config: every item is ``{"value": ...}``."""
    cfg = copy.deepcopy(plugin._DEFAULT_CONFIG)
    out = {key: {"value": item["default"]} for key, item in cfg.items()}
    out["url"]["value"] = TEST_URL
    out["mappingSource"]["value"] = "inline"
    out["mappingInline"]["value"] = mapping_text
    # Pagination would require multiple mocked endpoints; single-page tests only here.
    out["followPagination"]["value"] = "false"
    return out


def _strip_keys(readings: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if k != "key"} for r in readings]


@pytest.fixture(params=_fixture_names(), ids=lambda n: n)
def fixture(request):
    return _load_fixture(request.param)


@responses.activate
def test_fixture_roundtrip(fixture):
    """End-to-end roundtrip through plugin_init -> plugin_poll -> plugin_shutdown."""
    responses.add(responses.GET, TEST_URL, json=fixture["payload"], status=200)

    config = _build_config(fixture["mapping_text"])
    handle = plugin.plugin_init(config)
    try:
        readings = plugin.plugin_poll(handle)
    finally:
        plugin.plugin_shutdown(handle)

    assert isinstance(readings, list)

    for r in readings:
        assert "key" in r
        uuid.UUID(r["key"])
        assert ISO8601_TZ.match(r["timestamp"]), (
            f"timestamp {r['timestamp']!r} does not match ISO-8601-with-offset"
        )

    assert _strip_keys(readings) == fixture["expected"], (
        f"readings for fixture {fixture['name']!r} do not match expected"
    )


def test_shorthand_and_explicit_expected_are_byte_identical():
    """The shorthand desugars exactly to the explicit form.

    Both fixtures share a payload and reach `expected/` by different code
    paths (`_expand_from` vs the explicit datapoints loop), so equal bytes
    here is a real equivalence assertion, not a tautology.
    """
    shorthand = (EXPECTED_DIR / "passthrough_shorthand.json").read_bytes()
    explicit = (EXPECTED_DIR / "passthrough_explicit.json").read_bytes()
    assert shorthand == explicit


def test_example_mappings_are_valid():
    """Every shipped example mapping must load and validate."""
    from python.fledge.plugins.south.restclient import mapping_loader

    examples = sorted((REPO_ROOT / "examples").glob("*.yaml"))
    assert examples, "no example mappings found"
    for path in examples:
        mapping_loader.load_from_file(path)


def test_example_mapping_matches_fixture_mapping():
    """The site snapshot example is regression-tested through the `site_snapshot` triple.

    That only holds while the two documents say the same thing, so pin them
    together here. Comments and formatting may differ; the parsed mapping
    may not. If this fails, copy `examples/site-snapshot-full-mapping.yaml` over
    the fixture mapping (keeping its header) and regenerate the expected
    readings - see docs/rest-testing.rst.
    """
    from python.fledge.plugins.south.restclient import mapping_loader

    example = mapping_loader.load_from_file(REPO_ROOT / "examples" / "site-snapshot-full-mapping.yaml")
    fixture = mapping_loader.load_from_file(MAPPINGS_DIR / "site_snapshot.yaml")
    assert example == fixture
