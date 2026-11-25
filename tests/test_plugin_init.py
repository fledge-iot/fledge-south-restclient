"""Tests for ``plugin_init``."""
from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import patch

import pytest

from python.fledge.plugins.south.restclient import restclient as plugin
from python.fledge.plugins.south.restclient.mapping_loader import MappingError


_MINIMAL_MAPPING = """
extractions:
  - asset: "a"
    timestamp: "ts"
    datapoints:
      "x": { value: "x" }
"""


def _fledge_config(**overrides) -> dict:
    cfg = copy.deepcopy(plugin._DEFAULT_CONFIG)
    out = {key: {"value": item["default"]} for key, item in cfg.items()}
    out["mappingInline"]["value"] = _MINIMAL_MAPPING
    for k, v in overrides.items():
        out[k] = {"value": v}
    return out


class TestPluginInit:
    def test_valid_config_returns_handle(self):
        handle = plugin.plugin_init(_fledge_config())
        try:
            assert handle is not None
            assert handle["alive"] is True
            assert "session" in handle
            assert "mapping" in handle
            assert handle["mapping"]["extractions"][0]["asset"] == "a"
        finally:
            plugin.plugin_shutdown(handle)

    def test_handle_carries_unwrapped_config_values(self):
        handle = plugin.plugin_init(_fledge_config(url="http://example.com/"))
        try:
            assert handle["config"]["url"] == "http://example.com/"
            assert handle["config"]["authType"] == "none"
        finally:
            plugin.plugin_shutdown(handle)

    def test_does_not_open_http_connection(self):
        """plugin_init must not contact the network - lazy connect via plugin_poll."""
        with patch.object(plugin.acquisition, "fetch") as mock_fetch:
            handle = plugin.plugin_init(_fledge_config())
            try:
                assert not mock_fetch.called
            finally:
                plugin.plugin_shutdown(handle)

    def test_invalid_mapping_inline_raises_mapping_error(self):
        bad = _fledge_config()
        bad["mappingInline"]["value"] = "extractions: not-a-list"
        with pytest.raises(MappingError):
            plugin.plugin_init(bad)

    def test_missing_mapping_file_raises(self, tmp_path: Path):
        cfg = _fledge_config(mappingSource="file", mappingFile=str(tmp_path / "missing.yaml"))
        with pytest.raises(MappingError, match="not found"):
            plugin.plugin_init(cfg)

    def test_file_source_loads_mapping(self, tmp_path: Path):
        mapping_file = tmp_path / "m.yaml"
        mapping_file.write_text(_MINIMAL_MAPPING)
        cfg = _fledge_config(mappingSource="file", mappingFile=str(mapping_file))
        handle = plugin.plugin_init(cfg)
        try:
            assert handle["mapping"]["extractions"][0]["asset"] == "a"
        finally:
            plugin.plugin_shutdown(handle)

    def test_empty_inline_mapping_raises(self):
        bad = _fledge_config()
        bad["mappingInline"]["value"] = "{}"
        with pytest.raises(MappingError):
            plugin.plugin_init(bad)

    def test_invalid_tags_to_read_raises_mapping_error(self):
        """South-plugin-prompt item 12: bad tag map is a hard init error."""
        bad = _fledge_config(tagsToRead='{"tags":[{"asset":"no-name"}]}')
        with pytest.raises(MappingError, match="name must be a non-empty"):
            plugin.plugin_init(bad)

    def test_tags_to_read_attached_when_mapping_has_none(self):
        handle = plugin.plugin_init(
            _fledge_config(
                mappingInline="""
extractions:
  - asset: "fallback"
    timestamp: "ts"
    from: "@"
""",
                tagsToRead='{"tags":[{"name":"x","asset":"X","datapoint":"dx"}]}',
            )
        )
        try:
            assert handle["mapping"]["tags"] == [
                {"name": "x", "asset": "X", "datapoint": "dx"}
            ]
        finally:
            plugin.plugin_shutdown(handle)

    def test_plain_values_config_also_accepted(self):
        """plugin_init should also accept config values directly (not Fledge-wrapped)."""
        cfg = {key: item["default"] for key, item in plugin._DEFAULT_CONFIG.items()}
        cfg["mappingInline"] = _MINIMAL_MAPPING
        handle = plugin.plugin_init(cfg)
        try:
            assert handle["config"]["url"] == "http://localhost/"
        finally:
            plugin.plugin_shutdown(handle)
