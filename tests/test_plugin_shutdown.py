"""Tests for ``plugin_shutdown``."""
from __future__ import annotations

import copy
from unittest.mock import MagicMock

from python.fledge.plugins.south.restclient import restclient as plugin


_MINIMAL_MAPPING = """
extractions:
  - asset: "a"
    timestamp: "ts"
    datapoints:
      "x": { value: "x" }
"""


def _fledge_config() -> dict:
    cfg = copy.deepcopy(plugin._DEFAULT_CONFIG)
    out = {key: {"value": item["default"]} for key, item in cfg.items()}
    out["mappingInline"]["value"] = _MINIMAL_MAPPING
    return out


class TestPluginShutdown:
    def test_closes_session(self):
        handle = plugin.plugin_init(_fledge_config())
        handle["session"] = MagicMock()
        plugin.plugin_shutdown(handle)
        handle["session"].close.assert_called_once()
        assert handle["alive"] is False

    def test_idempotent(self):
        handle = plugin.plugin_init(_fledge_config())
        handle["session"] = MagicMock()
        plugin.plugin_shutdown(handle)
        plugin.plugin_shutdown(handle)
        # close called exactly once across two shutdowns
        handle["session"].close.assert_called_once()

    def test_safe_with_none_handle(self):
        # Should not raise
        plugin.plugin_shutdown(None)  # type: ignore[arg-type]

    def test_safe_with_empty_dict(self):
        plugin.plugin_shutdown({})

    def test_session_close_failure_is_swallowed(self):
        handle = plugin.plugin_init(_fledge_config())
        handle["session"] = MagicMock()
        handle["session"].close.side_effect = RuntimeError("oops")
        # Must not raise - shutdown is best-effort
        plugin.plugin_shutdown(handle)
        assert handle["alive"] is False
