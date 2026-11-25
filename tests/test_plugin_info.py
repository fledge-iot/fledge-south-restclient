"""Tests for ``plugin_info``."""
from __future__ import annotations

from python.fledge.plugins.south.restclient import restclient as plugin


_REQUIRED_ITEM_KEYS = {"description", "type", "default"}


class TestPluginInfo:
    def test_returns_required_top_level_keys(self):
        info = plugin.plugin_info()
        assert info["name"] == "restclient"
        assert info["type"] == "south"
        assert info["mode"] == "poll"
        assert info["interface"] == "2.0"
        assert isinstance(info["version"], str) and info["version"]
        assert isinstance(info["config"], dict) and info["config"]

    def test_every_config_item_has_required_metadata(self):
        for name, item in plugin.plugin_info()["config"].items():
            missing = _REQUIRED_ITEM_KEYS - set(item)
            assert not missing, f"item {name!r} missing required keys: {missing}"
            assert "displayName" in item, f"item {name!r} missing displayName"

    def test_plugin_item_is_readonly_and_matches_name(self):
        item = plugin.plugin_info()["config"]["plugin"]
        assert item["readonly"] == "true"
        assert item["default"] == "restclient"

    def test_tags_to_read_item_present(self):
        item = plugin.plugin_info()["config"]["tagsToRead"]
        assert item["type"] == "JSON"
        assert item["group"] == "Mapping"
        assert item["default"] == '{"tags":[]}'

    def test_assetName_item_present(self):
        item = plugin.plugin_info()["config"]["assetName"]
        assert item["type"] == "string"
        assert item["default"] == "restclient"

    def test_auth_fields_have_validity_rules(self):
        cfg = plugin.plugin_info()["config"]
        assert cfg["username"]["validity"] == 'authType == "basic"'
        assert cfg["password"]["validity"] == 'authType == "basic"'
        assert cfg["bearerToken"]["validity"] == 'authType == "bearer"'
        assert cfg["apiKeyHeader"]["validity"] == 'authType == "apiKey"'
        assert cfg["apiKeyValue"]["validity"] == 'authType == "apiKey"'

    def test_no_pollinterval_item(self):
        """Fledge supplies pollInterval via service config, not plugin config."""
        cfg = plugin.plugin_info()["config"]
        assert "pollInterval" not in cfg
        assert "interval" not in cfg

    def test_enumerations_have_options(self):
        cfg = plugin.plugin_info()["config"]
        assert set(cfg["authType"]["options"]) == {"none", "basic", "bearer", "apiKey"}
        assert set(cfg["responseFormat"]["options"]) == {
            "auto",
            "odata_v4",
            "raw_array",
            "raw_object",
        }
        assert set(cfg["mappingSource"]["options"]) == {"inline", "file"}
        assert set(cfg["readingStructure"]["options"]) == {
            "reading_per_asset",
            "reading_per_tag",
            "single_reading",
        }
        assert set(cfg["errorHandling"]["options"]) == {"partial", "strict"}
