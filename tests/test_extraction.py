"""Unit tests for the extraction layer."""
from __future__ import annotations

import json
import re
from datetime import datetime

import jmespath
import pytest

from python.fledge.plugins.south.restclient import extraction


def _mapping(extractions, source=None):
    m = {"extractions": extractions}
    if source is not None:
        m["source"] = source
    return m


class TestCustomJmesPathFunctions:
    def test_from_json_parses_string(self):
        payload = {"raw": '{"a": 1, "b": 2}'}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {
                        "a": {"value": "from_json(raw).a"},
                        "b": {"value": "from_json(raw).b"},
                    },
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert sorted(t[1:3] for t in tuples) == [("a", 1), ("b", 2)]

    def test_from_json_null_returns_null(self):
        payload = {"raw": None}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"a": {"value": "from_json(raw).a"}},
                }
            ]
        )
        # null result is dropped
        assert extraction.extract(payload, mapping) == []

    def test_from_json_empty_string_returns_null(self):
        payload = {"raw": ""}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"a": {"value": "from_json(raw).a"}},
                }
            ]
        )
        assert extraction.extract(payload, mapping) == []

    def test_from_json_invalid_json_returns_null(self):
        payload = {"raw": "{not json"}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"a": {"value": "from_json(raw).a"}},
                }
            ]
        )
        assert extraction.extract(payload, mapping) == []

    def test_to_array_converts_object(self):
        payload = {"m": {"k1": "v1", "k2": "v2"}}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "for_each": "to_array(m)",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"key": {"value": "$item.key"}},
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert sorted(t[2] for t in tuples) == ["v1", "v2"] or sorted(t[2] for t in tuples) == [
            "k1",
            "k2",
        ]

    def test_flatten_via_from_emits_every_leaf(self):
        payload = {
            "LastUpdated": "2026-08-04T13:05:45.7308710Z",
            "Data": json.dumps(
                {
                    "SiteId": "SITE-A",
                    "Lines": [
                        {
                            "LineId": "Line1",
                            "Equipment": [
                                {
                                    "EquipmentId": "Tank01",
                                    "Readings": {"Level": 62.5},
                                    "Status": {"HighLevelAlarm": 0},
                                }
                            ],
                        }
                    ],
                }
            ),
        }
        mapping = _mapping(
            [
                {
                    "asset": "site",
                    "timestamp": "LastUpdated",
                    "from": "flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'])",
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        points = {t[1]: t[2] for t in tuples}
        assert points == {
            "SITE-A_Line1_Tank01_Readings_Level": 62.5,
            "SITE-A_Line1_Tank01_Status_HighLevelAlarm": 0,
        }

    def test_tag_map_routes_flattened_keys_to_asset_and_datapoint(self):
        payload = {
            "LastUpdated": "2026-08-04T13:05:45.7308710Z",
            "Data": json.dumps(
                {
                    "SiteId": "SITE-A",
                    "Lines": [
                        {
                            "LineId": "Line1",
                            "Equipment": [
                                {
                                    "EquipmentId": "Tank01",
                                    "Readings": {"Level": 62.5},
                                    "Status": {"HighLevelAlarm": 0},
                                }
                            ],
                        }
                    ],
                }
            ),
        }
        mapping = _mapping(
            [
                {
                    "asset": "unmapped",
                    "timestamp": "LastUpdated",
                    "from": "flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'])",
                    "tags": [
                        {
                            "name": "SITE-A_Line1_Tank01_Readings_Level",
                            "asset": "tank-01",
                            "datapoint": "level",
                        },
                        {
                            "name": "SITE-A_Line1_Tank01_Status_HighLevelAlarm",
                            "asset": "tank-01",
                            "datapoint": "high_alarm",
                        },
                        {
                            "name": "SITE-A_Line1_Tank01_Readings_DoesNotExist",
                            "asset": "tank-01",
                            "datapoint": "missing",
                        },
                    ],
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert [(t[0], t[1], t[2]) for t in tuples] == [
            ("tank-01", "level", 62.5),
            ("tank-01", "high_alarm", 0),
        ]

    def test_document_level_tags_apply_to_from_rules(self):
        mapping = {
            "tags": [
                {"name": "a", "asset": "A", "datapoint": "alpha"},
                {"name": "b", "datapoint": "beta"},
            ],
            "extractions": [
                {
                    "asset": "fallback",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "from": "@",
                }
            ],
        }
        tuples = extraction.extract({"a": 1, "b": 2, "c": 3}, mapping)
        assert [(t[0], t[1], t[2]) for t in tuples] == [
            ("A", "alpha", 1),
            ("fallback", "beta", 2),
        ]


class TestEvaluate:
    def test_from_json_and_path(self):
        payload = {"raw": '{"a": 1}'}
        assert extraction.evaluate("from_json(raw).a", payload) == 1

    def test_to_array(self):
        payload = {"m": {"k1": "v1"}}
        assert extraction.evaluate("to_array(m)", payload) == [{"key": "k1", "value": "v1"}]

    def test_flatten_structural_uses_indexes(self):
        payload = {
            "SiteId": "SITE-A",
            "Lines": [
                {
                    "LineId": "Line1",
                    "Equipment": [
                        {"EquipmentId": "Tank01", "Readings": {"Level": 62.5}},
                    ],
                }
            ],
        }
        assert extraction.evaluate("flatten(@)", payload) == {
            "SiteId": "SITE-A",
            "Lines_0_LineId": "Line1",
            "Lines_0_Equipment_0_EquipmentId": "Tank01",
            "Lines_0_Equipment_0_Readings_Level": 62.5,
        }

    def test_flatten_identity_keys_drop_container_names(self):
        payload = {
            "SiteId": "SITE-A",
            "Lines": [
                {
                    "LineId": "Line1",
                    "Equipment": [
                        {
                            "EquipmentId": "Pump01",
                            "Readings": {"FlowRate": 13.4},
                            "Status": {"Running": 1},
                        }
                    ],
                }
            ],
        }
        got = extraction.evaluate(
            "flatten(@, ['SiteId', 'LineId', 'EquipmentId'])",
            payload,
        )
        assert got == {
            "SITE-A_Line1_Pump01_Readings_FlowRate": 13.4,
            "SITE-A_Line1_Pump01_Status_Running": 1,
        }

    def test_flatten_null_is_empty_object(self):
        assert extraction.evaluate("flatten(missing)", {}) == {}

    def test_flatten_rejects_wrong_arity(self):
        with pytest.raises(jmespath.exceptions.JMESPathError, match="1, 2, or 3 arguments"):
            extraction.evaluate("flatten(@, ['a'], '_', '_')", {})

    def test_flatten_map_object_uses_keys(self):
        payload = {"M01": {"Import": 20.14, "Net": 20.02}, "M03": {"Import": 19.98}}
        assert extraction.evaluate("flatten(@)", payload) == {
            "M01_Import": 20.14,
            "M01_Net": 20.02,
            "M03_Import": 19.98,
        }

    def test_flatten_json_style_double_quoted_keys(self):
        """Users type ["SiteId"] like JSON; JMESPath would treat that as a field lookup."""
        from pathlib import Path

        payload = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "payloads" / "site_snapshot.json").read_text()
        )
        only_site = extraction.evaluate(
            'flatten(from_json(value[0].Data),["SiteId"])',
            payload,
        )
        assert "SiteId" not in only_site
        assert only_site["SITE-A_Lines_0_Equipment_0_Readings_Level"] == 76.412

        pretty = extraction.evaluate(
            'flatten(from_json(value[0].Data),["SiteId","LineId","EquipmentId"])',
            payload,
        )
        assert pretty["SITE-A_Line1_Tank01_Readings_Level"] == 76.412
        assert pretty["SITE-A_Line1_Pump01_Readings_FlowRate"] == 13.402

    def test_flatten_accepts_single_string_identity_key(self):
        payload = {"SiteId": "SITE-A", "Readings": {"Level": 62.5}}
        assert extraction.evaluate("flatten(@, 'SiteId')", payload) == {
            "SITE-A_Readings_Level": 62.5
        }

    def test_flatten_custom_separator(self):
        payload = {"SiteId": "SITE-A", "Readings": {"Level": 62.5}}
        assert extraction.evaluate("flatten(@, 'SiteId', '.')", payload) == {
            "SITE-A.Readings.Level": 62.5
        }
        assert extraction.evaluate("flatten(@, null, '-')", {"a": {"b": 1}}) == {"a-b": 1}

    def test_flatten_empty_separator_rejected(self):
        with pytest.raises(jmespath.exceptions.JMESPathError, match="non-empty string"):
            extraction.evaluate("flatten(@, null, '')", {"a": 1})

    def test_flatten_null_identity_lookup_explains_quote_mistake(self):
        payload = {"value": [{"Data": "{}"}]}
        with pytest.raises(jmespath.exceptions.JMESPathError, match="looks up a field"):
            # Bypass the JSON-style rewrite by using an identifier multi-select
            # whose field is missing on the root.
            extraction.evaluate("flatten(`{}`, [DoesNotExist])", payload)

    def test_flatten_site_fixture_examples(self):
        from pathlib import Path

        payload = json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "payloads" / "site_snapshot.json").read_text()
        )
        got = extraction.evaluate(
            "flatten(from_json(value[0].Data), ['SiteId', 'LineId', 'EquipmentId'])",
            payload,
        )
        assert got["SITE-A_Line1_Tank01_Readings_Level"] == 76.412
        assert got["SITE-A_Line2_Pump02_Status_Running"] in (0, 1)
        assert "SITE-A_Line2_Valve02_Readings_Position" not in got

    def test_item_and_parent_rewrite(self):
        ctx = {"_item": {"key": "M01"}, "_parent": {"LineId": "1"}}
        assert extraction.evaluate("$item.key", ctx) == "M01"
        assert extraction.evaluate("$parent.LineId", ctx) == "1"

    def test_raises_on_syntax_error(self):
        with pytest.raises(jmespath.exceptions.JMESPathError):
            extraction.evaluate("foo.", {})

    def test_eval_still_swallows_syntax_error(self):
        assert extraction._eval("foo.", {}) is None


class TestTemplateSubstitution:
    def test_asset_template(self):
        payload = {"items": [{"id": "A", "v": 1}, {"id": "B", "v": 2}]}
        mapping = _mapping(
            [
                {
                    "asset": "thing-{id}",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"v": {"value": "v"}},
                }
            ],
            source={"records": "items"},
        )
        tuples = extraction.extract(payload, mapping)
        assets = sorted(t[0] for t in tuples)
        assert assets == ["thing-A", "thing-B"]

    def test_datapoint_template(self):
        payload = {"items": [{"id": "A", "v": 1}]}
        mapping = _mapping(
            [
                {
                    "asset": "thing",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"{id}.value": {"value": "v"}},
                }
            ],
            source={"records": "items"},
        )
        tuples = extraction.extract(payload, mapping)
        assert tuples[0][1] == "A.value"

    def test_template_null_segment_drops(self):
        payload = {"items": [{"id": None, "v": 1}]}
        mapping = _mapping(
            [
                {
                    "asset": "thing-{id}",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"v": {"value": "v"}},
                }
            ],
            source={"records": "items"},
        )
        # asset template fails -> whole rule dropped for that record
        assert extraction.extract(payload, mapping) == []

    def test_datapoint_name_template_null_drops_only_that_datapoint(self):
        payload = {"items": [{"id": "A", "alt": None, "v": 1, "w": 2}]}
        mapping = _mapping(
            [
                {
                    "asset": "thing-{id}",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {
                        "{alt}.value": {"value": "v"},  # alt is null -> drop this datapoint
                        "kept": {"value": "w"},          # this one survives
                    },
                }
            ],
            source={"records": "items"},
        )
        tuples = extraction.extract(payload, mapping)
        assert [t[1] for t in tuples] == ["kept"]


class TestForEachIteration:
    def test_for_each_with_item_and_parent(self):
        payload = {
            "records": [
                {"name": "r1", "data": {"a": 1, "b": 2}},
            ]
        }
        mapping = _mapping(
            [
                {
                    "for_each": "to_array(data)",
                    "asset": "{$parent.name}-{$item.key}",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"v": {"value": "$item.value"}},
                }
            ],
            source={"records": "records"},
        )
        tuples = sorted(extraction.extract(payload, mapping))
        assert tuples[0][0:3] == ("r1-a", "v", 1)
        assert tuples[1][0:3] == ("r1-b", "v", 2)

    def test_for_each_non_list_yields_nothing(self):
        payload = {"records": [{"data": None}]}
        mapping = _mapping(
            [
                {
                    "for_each": "to_array(data)",
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"v": {"value": "$item.key"}},
                }
            ],
            source={"records": "records"},
        )
        # to_array(null) -> [] -> no iteration
        assert extraction.extract(payload, mapping) == []


class TestCast:
    @pytest.mark.parametrize(
        "cast,raw,expected",
        [
            ("int", "5", 5),
            ("int", 5.7, 5),
            ("float", "1.5", 1.5),
            ("string", 42, "42"),
            ("bool", 1, True),
            ("bool", 0, False),
            ("bool", "", False),
        ],
    )
    def test_cast_success(self, cast, raw, expected):
        payload = {"v": raw}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"out": {"value": "v", "cast": cast}},
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert tuples[0][2] == expected
        assert type(tuples[0][2]) is type(expected)

    def test_cast_failure_drops(self):
        payload = {"v": "not-a-number"}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"out": {"value": "v", "cast": "float"}},
                }
            ]
        )
        assert extraction.extract(payload, mapping) == []


class TestTimestampHandling:
    def test_z_becomes_plus_offset(self):
        payload = {"ts": "2026-04-16T10:31:33.451Z", "v": 1}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "ts",
                    "datapoints": {"v": {"value": "v"}},
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert tuples[0][3] == "2026-04-16 10:31:33.451000+00:00"

    def test_naive_timestamp_warns_and_assumes_utc(self, caplog):
        import logging

        caplog.set_level(logging.WARNING, logger="python.fledge.plugins.south.restclient.extraction")
        payload = {"ts": "2026-04-16T10:31:33", "v": 1}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "ts",
                    "datapoints": {"v": {"value": "v"}},
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert tuples[0][3] == "2026-04-16 10:31:33.000000+00:00"
        assert any("naive timestamp" in rec.message for rec in caplog.records)

    def test_missing_timestamp_falls_back_to_now(self):
        payload = {"v": 1}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "ts",  # JMESPath returns null
                    "datapoints": {"v": {"value": "v"}},
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert tuples[0][3].endswith("+00:00")
        datetime.strptime(tuples[0][3], "%Y-%m-%d %H:%M:%S.%f+00:00")

    def test_existing_offset_converted_to_utc(self):
        payload = {"ts": "2026-04-16T10:31:33.451+05:00", "v": 1}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "ts",
                    "datapoints": {"v": {"value": "v"}},
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert tuples[0][3] == "2026-04-16 05:31:33.451000+00:00"

    def test_per_datapoint_timestamp_overrides_rule(self):
        payload = {"rule_ts": "2026-01-01T00:00:00Z", "dp_ts": "2027-02-02T00:00:00Z", "v": 1}
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "rule_ts",
                    "datapoints": {"v": {"value": "v", "timestamp": "dp_ts"}},
                }
            ]
        )
        tuples = extraction.extract(payload, mapping)
        assert tuples[0][3].startswith("2027-02-02")


class TestNullPayload:
    def test_none_payload_yields_empty(self):
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"v": {"value": "v"}},
                }
            ]
        )
        assert extraction.extract(None, mapping) == []

    def test_records_null_yields_empty(self):
        payload = {}  # records expr `value` returns null
        mapping = _mapping(
            [
                {
                    "asset": "x",
                    "timestamp": "'2026-01-01T00:00:00Z'",
                    "datapoints": {"v": {"value": "v"}},
                }
            ],
            source={"records": "value"},
        )
        assert extraction.extract(payload, mapping) == []


class TestFromShorthand:
    """`from` pass-through expansion."""

    PAYLOAD = {
        "readings": {
            "SampleId": "S-0001234",
            "SiteCode": "SITE-A",
            "NetWeight": "12.5",
            "Station": "QC1",
            "Timestamp": "2026-04-16T10:31:33.451Z",
        }
    }

    def _rule(self, **overrides):
        rule = {
            "asset": "station-{readings.Station}",
            "timestamp": "readings.Timestamp",
            "from": "readings",
        }
        rule.update(overrides)
        return rule

    def test_expands_one_datapoint_per_key(self):
        tuples = extraction.extract(self.PAYLOAD, _mapping([self._rule()]))
        assert [t[1] for t in tuples] == [
            "SampleId",
            "SiteCode",
            "NetWeight",
            "Station",
            "Timestamp",
        ]
        assert all(t[0] == "station-QC1" for t in tuples)

    def test_preserves_source_object_order(self):
        payload = {"r": {"z": 1, "a": 2, "m": 3}}
        rule = {"asset": "x", "timestamp": "'2026-01-01T00:00:00Z'", "from": "r"}
        tuples = extraction.extract(payload, _mapping([rule]))
        assert [t[1] for t in tuples] == ["z", "a", "m"]

    def test_rename_changes_emitted_name_only(self):
        tuples = extraction.extract(
            self.PAYLOAD, _mapping([self._rule(rename={"SiteCode": "Site"})])
        )
        emitted = {t[1]: t[2] for t in tuples}
        assert emitted["Site"] == "SITE-A"
        assert "SiteCode" not in emitted

    def test_cast_keys_on_source_name_not_renamed_name(self):
        """`cast` keys on the ORIGINAL source key even when `rename` applies."""
        tuples = extraction.extract(
            self.PAYLOAD,
            _mapping([self._rule(rename={"NetWeight": "Weight"}, cast={"NetWeight": "float"})]),
        )
        emitted = {t[1]: t[2] for t in tuples}
        assert emitted["Weight"] == 12.5

    def test_star_cast_applies_to_unlisted_keys(self):
        payload = {"r": {"a": "1", "b": "2", "c": "3"}}
        rule = {
            "asset": "x",
            "timestamp": "'2026-01-01T00:00:00Z'",
            "from": "r",
            "cast": {"*": "float", "c": "string"},
        }
        tuples = extraction.extract(payload, _mapping([rule]))
        assert {t[1]: t[2] for t in tuples} == {"a": 1.0, "b": 2.0, "c": "3"}

    def test_exclude_drops_source_keys(self):
        tuples = extraction.extract(
            self.PAYLOAD, _mapping([self._rule(exclude=["Station", "Timestamp"])])
        )
        assert [t[1] for t in tuples] == ["SampleId", "SiteCode", "NetWeight"]

    def test_null_values_dropped(self):
        payload = {"r": {"a": 1, "b": None, "c": 3}}
        rule = {"asset": "x", "timestamp": "'2026-01-01T00:00:00Z'", "from": "r"}
        tuples = extraction.extract(payload, _mapping([rule]))
        assert [t[1] for t in tuples] == ["a", "c"]

    def test_failed_cast_drops_only_that_datapoint(self):
        payload = {"r": {"good": "1.5", "bad": "not-a-number", "also_good": "2.5"}}
        rule = {
            "asset": "x",
            "timestamp": "'2026-01-01T00:00:00Z'",
            "from": "r",
            "cast": {"*": "float"},
        }
        tuples = extraction.extract(payload, _mapping([rule]))
        assert [t[1] for t in tuples] == ["good", "also_good"]

    def test_from_yielding_non_object_emits_nothing(self):
        payload = {"r": [1, 2, 3]}
        rule = {"asset": "x", "timestamp": "'2026-01-01T00:00:00Z'", "from": "r"}
        assert extraction.extract(payload, _mapping([rule])) == []

    def test_from_yielding_null_still_processes_explicit_datapoints(self):
        """A dead `from` must not suppress explicit datapoints on the same rule."""
        payload = {"missing": None, "v": 42}
        rule = {
            "asset": "x",
            "timestamp": "'2026-01-01T00:00:00Z'",
            "from": "missing",
            "datapoints": {"v": {"value": "v"}},
        }
        tuples = extraction.extract(payload, _mapping([rule]))
        assert [(t[1], t[2]) for t in tuples] == [("v", 42)]

    def test_absent_field_produces_no_datapoint(self):
        """Keys are discovered from data - a sparse record is not an error."""
        rule = {"asset": "x", "timestamp": "'2026-01-01T00:00:00Z'", "from": "r"}
        sparse = extraction.extract({"r": {"a": 1}}, _mapping([rule]))
        full = extraction.extract({"r": {"a": 1, "b": 2}}, _mapping([rule]))
        assert [t[1] for t in sparse] == ["a"]
        assert [t[1] for t in full] == ["a", "b"]

    def test_explicit_datapoints_emitted_after_from(self):
        """Explicit datapoints win on collision by being emitted last."""
        payload = {"r": {"a": "from-value"}, "other": "explicit-value"}
        rule = {
            "asset": "x",
            "timestamp": "'2026-01-01T00:00:00Z'",
            "from": "r",
            "datapoints": {"a": {"value": "other"}},
        }
        tuples = extraction.extract(payload, _mapping([rule]))
        assert [(t[1], t[2]) for t in tuples] == [("a", "from-value"), ("a", "explicit-value")]
        # emission is last-write-wins on datapoint name within an (asset, ts) group
        from python.fledge.plugins.south.restclient import emission

        readings = emission.group(tuples, "reading_per_asset")
        assert readings[0]["readings"]["a"] == "explicit-value"

    def test_works_inside_for_each_with_item_and_parent(self):
        payload = {
            "LastUpdated": "2026-04-16T10:31:33.451Z",
            "lines": [
                {"n": 1, "vals": {"speed": 10, "temp": 20}},
                {"n": 2, "vals": {"speed": 30}},
            ],
        }
        rule = {
            "for_each": "lines",
            "asset": "line-{$item.n}",
            "timestamp": "$parent.LastUpdated",
            "from": "$item.vals",
        }
        tuples = extraction.extract(payload, _mapping([rule]))
        assert [(t[0], t[1], t[2]) for t in tuples] == [
            ("line-1", "speed", 10),
            ("line-1", "temp", 20),
            ("line-2", "speed", 30),
        ]
        assert all(t[3] == "2026-04-16 10:31:33.451000+00:00" for t in tuples)

    def test_rule_timestamp_used_for_all_from_datapoints(self):
        tuples = extraction.extract(self.PAYLOAD, _mapping([self._rule()]))
        assert {t[3] for t in tuples} == {"2026-04-16 10:31:33.451000+00:00"}

    def test_null_rule_timestamp_falls_back_to_now(self):
        payload = {"r": {"a": 1}, "ts": None}
        rule = {"asset": "x", "timestamp": "ts", "from": "r"}
        tuples = extraction.extract(payload, _mapping([rule]))
        assert len(tuples) == 1
        datetime.strptime(tuples[0][3], "%Y-%m-%d %H:%M:%S.%f+00:00")
        assert tuples[0][3].endswith("+00:00")


class TestFledgeTimestampFormat:
    """Timestamps must match what Fledge's C++ Reading::stringToTimestamp parses.

    It uses strptime("%Y-%m-%d %H:%M:%S"), so a "T" separator loses the time of
    day (every reading lands at 00:00:00 UTC), and it reads the fraction as a
    raw microsecond count, so more than 6 digits overflows tv_usec.
    """

    from datetime import datetime as _dt

    @pytest.mark.parametrize(
        "raw, expected",
        [
            # 7 fractional digits and a Z suffix, as some OData sources emit.
            ("2026-11-24T15:40:08.2097515Z", "2026-11-24 15:40:08.209751+00:00"),
            ("2026-11-24T15:40:08Z", "2026-11-24 15:40:08.000000+00:00"),
            ("2026-11-24T15:40:08.5z", "2026-11-24 15:40:08.500000+00:00"),
            # Offsets are converted to UTC, across a date boundary too.
            ("2026-11-24T08:40:08.1-07:00", "2026-11-24 15:40:08.100000+00:00"),
            ("2026-11-24T20:40:08+0500", "2026-11-24 15:40:08.000000+00:00"),
            ("2026-11-24T22:00:00-07", "2026-11-25 05:00:00.000000+00:00"),
            # Already in Fledge's own form: unchanged.
            ("2026-11-24 15:40:08.209751+00:00", "2026-11-24 15:40:08.209751+00:00"),
        ],
    )
    def test_normalizes_to_fledge_form(self, raw, expected):
        out = extraction._format_timestamp(raw)
        assert out == expected
        self._dt.strptime(out[:19], "%Y-%m-%d %H:%M:%S")
        assert len(out.split(".")[1].split("+")[0]) == 6

    @pytest.mark.parametrize("raw", ["not a time", "1764000000", "2026-13-40T99:00:00Z"])
    def test_unparseable_returns_none(self, raw, caplog):
        assert extraction._format_timestamp(raw) is None

    def test_now_is_fledge_form(self):
        out = extraction._now_iso()
        self._dt.strptime(out, "%Y-%m-%d %H:%M:%S.%f+00:00")
