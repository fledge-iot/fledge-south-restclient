"""Tests for ``plugin_poll``."""
from __future__ import annotations

import copy
import json
import re
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import responses

from python.fledge.plugins.south.restclient import restclient as plugin


URL = "http://test-host.local/api/data"
ISO8601_TZ = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:?\d{2}|Z)$"
)


_FLAT_MAPPING = """
extractions:
  - asset: "weather"
    timestamp: "ts"
    datapoints:
      "temperature": { value: "temperature", cast: "float" }
      "humidity":    { value: "humidity",    cast: "int"   }
"""


def _fledge_config(**overrides) -> dict:
    cfg = copy.deepcopy(plugin._DEFAULT_CONFIG)
    out = {key: {"value": item["default"]} for key, item in cfg.items()}
    out["url"]["value"] = URL
    out["mappingInline"]["value"] = _FLAT_MAPPING
    out["followPagination"]["value"] = "false"
    for k, v in overrides.items():
        out[k] = {"value": v}
    return out


def _payloads_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "payloads"


@pytest.fixture
def handle():
    h = plugin.plugin_init(_fledge_config())
    try:
        yield h
    finally:
        plugin.plugin_shutdown(h)


class TestHappyPath:
    @responses.activate
    def test_returns_list_with_one_reading(self, handle):
        responses.add(
            responses.GET,
            URL,
            json={"temperature": 22.5, "humidity": 60, "ts": "2026-05-15T14:30:00Z"},
            status=200,
        )
        readings = plugin.plugin_poll(handle)
        assert isinstance(readings, list)
        assert len(readings) == 1
        r = readings[0]
        assert r["asset"] == "weather"
        assert ISO8601_TZ.match(r["timestamp"])
        uuid.UUID(r["key"])
        assert r["readings"] == {"temperature": 22.5, "humidity": 60}

    @responses.activate
    def test_asset_name_comes_from_mapping_not_config(self, handle):
        """Mapping says ``weather``; assetName config default ``restclient`` is unused."""
        responses.add(
            responses.GET,
            URL,
            json={"temperature": 1.0, "humidity": 2, "ts": "2026-05-15T14:30:00Z"},
            status=200,
        )
        readings = plugin.plugin_poll(handle)
        assert readings[0]["asset"] == "weather"


class TestMultipleAssets:
    @responses.activate
    def test_multiple_assets_in_one_list(self):
        payload = json.loads((_payloads_dir() / "batch_runs_single_active.json").read_text())
        mapping_text = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "mappings"
            / "batch_runs_single_active.yaml"
        ).read_text()
        cfg = _fledge_config(mappingInline=mapping_text)
        handle = plugin.plugin_init(cfg)
        try:
            responses.add(responses.GET, URL, json=payload, status=200)
            readings = plugin.plugin_poll(handle)
            assets = sorted(r["asset"] for r in readings)
            assert assets == ["EnergyMeter-1", "EnergyMeter-2"]
        finally:
            plugin.plugin_shutdown(handle)


class TestKeysAreUnique:
    @responses.activate
    def test_keys_unique_across_consecutive_polls(self, handle):
        responses.add(
            responses.GET,
            URL,
            json={"temperature": 1.0, "humidity": 2, "ts": "2026-05-15T14:30:00Z"},
            status=200,
        )
        responses.add(
            responses.GET,
            URL,
            json={"temperature": 1.0, "humidity": 2, "ts": "2026-05-15T14:30:00Z"},
            status=200,
        )
        first = plugin.plugin_poll(handle)
        second = plugin.plugin_poll(handle)
        assert first[0]["key"] != second[0]["key"]


class TestErrorPaths:
    @responses.activate
    def test_device_unreachable_returns_empty_no_stale(self, handle):
        responses.add(
            responses.GET, URL, body=requests.exceptions.ConnectionError("down")
        )
        assert plugin.plugin_poll(handle) == []

    @responses.activate
    def test_http_4xx_returns_empty(self, handle):
        responses.add(responses.GET, URL, json={"err": "x"}, status=401)
        assert plugin.plugin_poll(handle) == []

    @responses.activate
    def test_http_5xx_returns_empty(self, handle):
        responses.add(responses.GET, URL, status=503)
        assert plugin.plugin_poll(handle) == []

    @responses.activate
    def test_timeout_returns_empty(self, handle):
        responses.add(responses.GET, URL, body=requests.exceptions.Timeout("slow"))
        assert plugin.plugin_poll(handle) == []

    @responses.activate
    def test_invalid_json_returns_empty(self, handle):
        responses.add(responses.GET, URL, body="not json", status=200)
        assert plugin.plugin_poll(handle) == []

    @responses.activate
    def test_odata_error_envelope_returns_empty(self):
        cfg = _fledge_config(
            mappingInline="""
source:
  records: "value"
extractions:
  - asset: "x"
    timestamp: "ts"
    datapoints: {"y": {"value": "y"}}
"""
        )
        handle = plugin.plugin_init(cfg)
        try:
            responses.add(
                responses.GET,
                URL,
                json={"error": {"code": "AuthRequired", "message": "no"}},
                status=200,
            )
            assert plugin.plugin_poll(handle) == []
        finally:
            plugin.plugin_shutdown(handle)

    def test_unexpected_exception_returns_empty(self, handle):
        """Top-level catch-all: any unhandled error returns []."""
        with patch.object(plugin.acquisition, "fetch", side_effect=RuntimeError("boom")):
            assert plugin.plugin_poll(handle) == []

    def test_dead_handle_returns_empty(self):
        handle = plugin.plugin_init(_fledge_config())
        plugin.plugin_shutdown(handle)
        assert plugin.plugin_poll(handle) == []


class TestPartialExtraction:
    @responses.activate
    def test_missing_datapoint_dropped_others_survive(self, handle):
        """Partial mode (default): a datapoint whose JMESPath yields null is dropped."""
        responses.add(
            responses.GET,
            URL,
            json={"temperature": 22.5, "ts": "2026-05-15T14:30:00Z"},  # humidity missing
            status=200,
        )
        readings = plugin.plugin_poll(handle)
        assert readings[0]["readings"] == {"temperature": 22.5}


class TestDataTypes:
    @pytest.mark.parametrize(
        "cast,raw,expected,py_type",
        [
            ("int", 7, 7, int),
            ("int", "9", 9, int),
            ("float", 1.5, 1.5, float),
            ("float", "2.5", 2.5, float),
            ("string", 42, "42", str),
            ("bool", 1, True, bool),
            ("bool", "", False, bool),
        ],
    )
    @responses.activate
    def test_cast_roundtrip(self, cast, raw, expected, py_type):
        mapping = f"""
extractions:
  - asset: "x"
    timestamp: "ts"
    datapoints:
      "v": {{ value: "v", cast: "{cast}" }}
"""
        cfg = _fledge_config(mappingInline=mapping)
        handle = plugin.plugin_init(cfg)
        try:
            responses.add(
                responses.GET, URL, json={"v": raw, "ts": "2026-05-15T14:30:00Z"}, status=200
            )
            readings = plugin.plugin_poll(handle)
            v = readings[0]["readings"]["v"]
            assert v == expected
            assert type(v) is py_type
        finally:
            plugin.plugin_shutdown(handle)
