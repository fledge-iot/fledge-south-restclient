"""Type hints from the mapping, attached to emitted readings as OMFHint."""
from __future__ import annotations

import json

import pytest

from python.fledge.plugins.south.restclient import (
    emission,
    extraction,
    mapping_loader,
    omf_hints,
)
from python.fledge.plugins.south.restclient.mapping_loader import MappingError
from python.fledge.plugins.south.restclient.tag_map import attach_config_tags, parse_tags

TS = "2026-11-24 15:40:08.209751+00:00"
FLOAT64 = {"number": "float64"}


def _hint(reading: dict) -> dict:
    return json.loads(reading["readings"][omf_hints.HINT_DATAPOINT])


class TestValidation:
    def test_tag_hint_is_kept(self):
        [tag] = parse_tags([{"name": "a", "datapoint": "flow_rate", "omfHint": FLOAT64}])
        assert tag["omfHint"] == FLOAT64

    @pytest.mark.parametrize(
        "bad",
        [{}, "float64", ["number"], {"number": ["float64"]}, {"number": True}, {"name": "x"}],
    )
    def test_bad_tag_hint_is_a_mapping_error(self, bad):
        with pytest.raises(MappingError, match="omfHint"):
            parse_tags([{"name": "a", "omfHint": bad}])

    def test_tagsToRead_json_carries_hints(self):
        mapping = {"extractions": []}
        attach_config_tags(
            mapping,
            json.dumps({"tags": [{"name": "a", "asset": "A", "datapoint": "d", "omfHint": FLOAT64}]}),
        )
        assert mapping["tags"][0]["omfHint"] == FLOAT64

    def test_mapping_level_hints(self):
        mapping = mapping_loader.load_from_text(
            "omfHints:\n  flow_rate: {number: float64}\n"
            "extractions:\n  - {asset: A, timestamp: t, from: '@'}\n"
        )
        assert mapping["omfHints"] == {"flow_rate": FLOAT64}

    @pytest.mark.parametrize("bad", ["[]", "{flow_rate: float64}", "{'': {number: float64}}"])
    def test_bad_mapping_hints(self, bad):
        with pytest.raises(MappingError, match="omfHints"):
            mapping_loader.load_from_text(
                f"omfHints: {bad}\nextractions:\n  - {{asset: A, timestamp: t, from: '@'}}\n"
            )


class TestResolver:
    def test_tag_hint_beats_mapping_hint(self):
        resolver = omf_hints.build_resolver(
            {
                "omfHints": {"flow_rate": {"number": "float32"}},
                "tags": [{"name": "a", "asset": "A", "datapoint": "flow_rate", "omfHint": FLOAT64}],
                "extractions": [],
            }
        )
        assert omf_hints.lookup(resolver, "A", "flow_rate") == FLOAT64
        assert omf_hints.lookup(resolver, "B", "flow_rate") == {"number": "float32"}
        assert omf_hints.lookup(resolver, "B", "other") is None

    def test_tag_without_asset_hints_every_asset(self):
        resolver = omf_hints.build_resolver(
            {"extractions": [{"tags": [{"name": "src", "omfHint": FLOAT64}]}]}
        )
        assert omf_hints.lookup(resolver, "anything", "src") == FLOAT64


class TestEmission:
    TUPLES = [
        ("A", "flow_rate", 0, TS),
        ("A", "running", 0, TS),
        ("B", "flow_rate", 0.5, TS),
    ]
    RESOLVER = {(None, "flow_rate"): FLOAT64}

    def test_per_asset(self):
        a, b = emission.group(self.TUPLES, hints=self.RESOLVER)
        assert _hint(a) == {"datapoint": [{"name": "flow_rate", "number": "float64"}]}
        assert _hint(b) == {"datapoint": [{"name": "flow_rate", "number": "float64"}]}
        assert a["readings"]["flow_rate"] == 0  # the value itself is untouched

    def test_per_tag_only_hinted_readings_get_a_hint(self):
        out = emission.group(self.TUPLES, emission.READING_PER_TAG, hints=self.RESOLVER)
        assert [omf_hints.HINT_DATAPOINT in r["readings"] for r in out] == [True, False, True]

    def test_single_reading(self):
        [r] = emission.group(self.TUPLES, emission.SINGLE_READING, "all", hints=self.RESOLVER)
        assert _hint(r) == {"datapoint": [{"name": "flow_rate", "number": "float64"}]}

    def test_no_hints_no_hint_datapoint(self):
        for r in emission.group(self.TUPLES):
            assert omf_hints.HINT_DATAPOINT not in r["readings"]

    def test_source_OMFHint_datapoint_is_not_overwritten(self):
        tuples = [("A", "flow_rate", 0, TS), ("A", "OMFHint", "from source", TS)]
        [r] = emission.group(tuples, hints=self.RESOLVER)
        assert r["readings"]["OMFHint"] == "from source"


def test_flatten_with_tag_map_hints():
    """Site snapshot shape: flattened keys, a tag map, an idle analog reading 0."""
    payload = {
        "value": [
            {
                "LastUpdated": "2026-11-24T15:40:08.2097515Z",
                "Data": json.dumps(
                    {
                        "SiteId": "SITE-A",
                        "Lines": [
                            {
                                "LineId": "Line1",
                                "Equipment": [
                                    {
                                        "EquipmentId": "Pump01",
                                        "Readings": {"FlowRate": 0, "MotorCurrent": 0},
                                    }
                                ],
                            }
                        ],
                    }
                ),
            }
        ]
    }
    mapping = mapping_loader.load_from_text(
        "source: {records: value}\n"
        "extractions:\n"
        "  - asset: unmapped\n"
        "    timestamp: LastUpdated\n"
        "    from: \"flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'])\"\n"
    )
    asset = "SITE-A-Pump01"
    attach_config_tags(
        mapping,
        {
            "tags": [
                {"name": "SITE-A_Line1_Pump01_Readings_FlowRate", "asset": asset,
                 "datapoint": "flow_rate", "omfHint": FLOAT64},
                {"name": "SITE-A_Line1_Pump01_Readings_MotorCurrent", "asset": asset,
                 "datapoint": "motor_current"},
            ]
        },
    )
    [reading] = emission.group(
        extraction.extract(payload, mapping), hints=omf_hints.build_resolver(mapping)
    )
    assert reading["asset"] == asset
    assert reading["timestamp"] == "2026-11-24 15:40:08.209751+00:00"
    assert reading["readings"]["flow_rate"] == 0
    assert reading["readings"]["motor_current"] == 0
    assert _hint(reading) == {"datapoint": [{"name": "flow_rate", "number": "float64"}]}
