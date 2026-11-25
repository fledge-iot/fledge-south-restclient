"""Unit tests for the EtherNet/IP-style read tag map."""
from __future__ import annotations

import pytest

from python.fledge.plugins.south.restclient.mapping_loader import MappingError
from python.fledge.plugins.south.restclient.tag_map import (
    attach_config_tags,
    mapping_defines_tags,
    parse_tags,
)


class TestParseTags:
    def test_none_and_empty_are_no_map(self):
        assert parse_tags(None) == []
        assert parse_tags("") == []
        assert parse_tags([]) == []
        assert parse_tags({"tags": []}) == []
        assert parse_tags('{"tags":[]}') == []

    def test_bare_array(self):
        assert parse_tags(
            [{"name": "speed", "asset": "motor", "datapoint": "RPM", "type": "UINT32"}]
        ) == [{"name": "speed", "asset": "motor", "datapoint": "RPM"}]

    def test_tags_wrapper_and_json_string(self):
        raw = '{"tags":[{"name":"t","program":"","asset":"A","datapoint":"d"}]}'
        assert parse_tags(raw) == [{"name": "t", "asset": "A", "datapoint": "d"}]

    def test_missing_name_rejected(self):
        with pytest.raises(MappingError, match="name must be a non-empty"):
            parse_tags([{"asset": "A"}])

    def test_unknown_cast_rejected(self):
        with pytest.raises(MappingError, match="cast must be one of"):
            parse_tags([{"name": "t", "cast": "REAL"}])

    def test_invalid_json_rejected(self):
        with pytest.raises(MappingError, match="not valid JSON"):
            parse_tags("{not json")

    def test_object_without_tags_key_rejected(self):
        with pytest.raises(MappingError, match="must contain a `tags` array"):
            parse_tags({"name": "t"})


class TestAttachConfigTags:
    def test_fills_empty_mapping(self):
        mapping = {"extractions": [{"asset": "a", "from": "@"}]}
        attach_config_tags(
            mapping, [{"name": "x", "asset": "X", "datapoint": "dx"}]
        )
        assert mapping["tags"][0]["name"] == "x"

    def test_does_not_override_mapping_tags(self):
        mapping = {
            "tags": [{"name": "from-doc"}],
            "extractions": [{"asset": "a", "from": "@"}],
        }
        attach_config_tags(mapping, [{"name": "from-config"}])
        assert mapping["tags"] == [{"name": "from-doc"}]

    def test_mapping_defines_tags(self):
        assert not mapping_defines_tags({"extractions": [{"from": "@"}]})
        assert mapping_defines_tags({"tags": [{"name": "a"}]})
        assert mapping_defines_tags({"extractions": [{"tags": [{"name": "a"}]}]})
