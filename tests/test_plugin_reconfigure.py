"""Tests for ``plugin_reconfigure``."""
from __future__ import annotations

import copy

import pytest

from python.fledge.plugins.south.restclient import restclient as plugin


_MINIMAL_MAPPING = """
extractions:
  - asset: "a"
    timestamp: "ts"
    datapoints:
      "x": { value: "x" }
"""

_ALTERNATE_MAPPING = """
extractions:
  - asset: "b"
    timestamp: "ts"
    datapoints:
      "y": { value: "y" }
"""


def _fledge_config(**overrides) -> dict:
    cfg = copy.deepcopy(plugin._DEFAULT_CONFIG)
    out = {key: {"value": item["default"]} for key, item in cfg.items()}
    out["mappingInline"]["value"] = _MINIMAL_MAPPING
    for k, v in overrides.items():
        out[k] = {"value": v}
    return out


class TestPluginReconfigure:
    def test_returns_new_handle(self):
        h1 = plugin.plugin_init(_fledge_config())
        h2 = plugin.plugin_reconfigure(h1, _fledge_config(url="http://other.local/"))
        try:
            assert h2 is not h1
            assert h2["alive"] is True
            assert h2["config"]["url"] == "http://other.local/"
        finally:
            plugin.plugin_shutdown(h2)

    def test_reload_mapping(self):
        h1 = plugin.plugin_init(_fledge_config())
        h2 = plugin.plugin_reconfigure(h1, _fledge_config(mappingInline=_ALTERNATE_MAPPING))
        try:
            assert h2["mapping"]["extractions"][0]["asset"] == "b"
        finally:
            plugin.plugin_shutdown(h2)

    def test_closes_old_session(self):
        h1 = plugin.plugin_init(_fledge_config())
        h2 = plugin.plugin_reconfigure(h1, _fledge_config(url="http://other.local/"))
        try:
            assert h1["alive"] is False
        finally:
            plugin.plugin_shutdown(h2)

    def test_invalid_new_config_keeps_old_handle(self):
        h1 = plugin.plugin_init(_fledge_config())
        bad = _fledge_config()
        bad["mappingInline"]["value"] = "extractions: not-a-list"
        h2 = plugin.plugin_reconfigure(h1, bad)
        try:
            # Should return the *original* handle when new mapping is invalid
            assert h2 is h1
            assert h1["alive"] is True
            assert h1["mapping"]["extractions"][0]["asset"] == "a"
        finally:
            plugin.plugin_shutdown(h2)
