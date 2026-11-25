"""Unit tests for mapping_loader."""
from __future__ import annotations

from pathlib import Path

import pytest

from python.fledge.plugins.south.restclient import mapping_loader
from python.fledge.plugins.south.restclient.mapping_loader import MappingError


_MINIMAL_VALID = """
extractions:
  - asset: "a"
    timestamp: "ts"
    datapoints:
      "x": { value: "x" }
"""


class TestLoadFromText:
    def test_minimal_valid(self):
        mapping = mapping_loader.load_from_text(_MINIMAL_VALID)
        assert mapping["extractions"][0]["asset"] == "a"

    def test_yaml_and_json_equivalent(self):
        from_yaml = mapping_loader.load_from_text(_MINIMAL_VALID)
        from_json = mapping_loader.load_from_text(
            '{"extractions": [{"asset": "a", "timestamp": "ts", '
            '"datapoints": {"x": {"value": "x"}}}]}'
        )
        assert from_yaml == from_json

    def test_invalid_yaml(self):
        with pytest.raises(MappingError, match="not valid YAML"):
            mapping_loader.load_from_text("extractions: [unclosed")

    def test_empty_document(self):
        with pytest.raises(MappingError, match="empty"):
            mapping_loader.load_from_text("")

    def test_non_object_top_level(self):
        with pytest.raises(MappingError, match="object at the top level"):
            mapping_loader.load_from_text("- 1\n- 2")


class TestValidate:
    def test_missing_extractions(self):
        with pytest.raises(MappingError, match="extractions"):
            mapping_loader.validate({})

    def test_empty_extractions(self):
        with pytest.raises(MappingError, match="non-empty list"):
            mapping_loader.validate({"extractions": []})

    def test_rule_missing_asset(self):
        with pytest.raises(MappingError, match="asset must be a non-empty string"):
            mapping_loader.validate(
                {"extractions": [{"timestamp": "ts", "datapoints": {"x": {"value": "x"}}}]}
            )

    def test_rule_missing_datapoints(self):
        with pytest.raises(MappingError, match="datapoints must be a non-empty mapping"):
            mapping_loader.validate(
                {"extractions": [{"asset": "a", "timestamp": "ts", "datapoints": {}}]}
            )

    def test_datapoint_missing_value(self):
        with pytest.raises(MappingError, match="value must be a non-empty"):
            mapping_loader.validate(
                {"extractions": [{"asset": "a", "timestamp": "ts", "datapoints": {"x": {}}}]}
            )

    def test_unknown_cast_rejected(self):
        with pytest.raises(MappingError, match="cast must be one of"):
            mapping_loader.validate(
                {
                    "extractions": [
                        {
                            "asset": "a",
                            "timestamp": "ts",
                            "datapoints": {"x": {"value": "x", "cast": "frobnicate"}},
                        }
                    ]
                }
            )

    def test_rule_timestamp_optional_when_per_datapoint(self):
        mapping_loader.validate(
            {
                "extractions": [
                    {
                        "asset": "a",
                        "datapoints": {"x": {"value": "x", "timestamp": "ts"}},
                    }
                ]
            }
        )

    def test_no_timestamp_anywhere_is_error(self):
        with pytest.raises(MappingError, match="no timestamp"):
            mapping_loader.validate(
                {"extractions": [{"asset": "a", "datapoints": {"x": {"value": "x"}}}]}
            )

    def test_source_records_must_be_string(self):
        with pytest.raises(MappingError, match="source.records.*string"):
            mapping_loader.validate(
                {
                    "source": {"records": 5},
                    "extractions": [
                        {"asset": "a", "timestamp": "ts", "datapoints": {"x": {"value": "x"}}}
                    ],
                }
            )

    def test_for_each_must_be_string(self):
        with pytest.raises(MappingError, match="for_each must be a JMESPath string"):
            mapping_loader.validate(
                {
                    "extractions": [
                        {
                            "asset": "a",
                            "for_each": 1,
                            "timestamp": "ts",
                            "datapoints": {"x": {"value": "x"}},
                        }
                    ]
                }
            )


class TestLoadDispatch:
    def test_inline_dict(self):
        mapping = mapping_loader.load(
            source="inline",
            inline={
                "extractions": [
                    {"asset": "a", "timestamp": "ts", "datapoints": {"x": {"value": "x"}}}
                ]
            },
        )
        assert mapping["extractions"][0]["asset"] == "a"

    def test_inline_empty(self):
        with pytest.raises(MappingError, match="requires a non-empty"):
            mapping_loader.load(source="inline", inline="")

    def test_file_missing(self, tmp_path: Path):
        with pytest.raises(MappingError, match="not found"):
            mapping_loader.load(source="file", path=str(tmp_path / "missing.yaml"))

    def test_file_loads(self, tmp_path: Path):
        f = tmp_path / "m.yaml"
        f.write_text(_MINIMAL_VALID)
        mapping = mapping_loader.load(source="file", path=str(f))
        assert mapping["extractions"][0]["asset"] == "a"

    def test_unknown_source(self):
        with pytest.raises(MappingError, match="unknown mappingSource"):
            mapping_loader.load(source="redis", inline="anything")

    def test_file_path_missing(self):
        with pytest.raises(MappingError, match="non-empty mappingFile path"):
            mapping_loader.load(source="file", path="")

    def test_inline_unsupported_type(self):
        with pytest.raises(MappingError, match="unsupported inline mapping type"):
            mapping_loader.load(source="inline", inline=[1, 2, 3])

    def test_file_with_invalid_content(self, tmp_path: Path):
        f = tmp_path / "broken.yaml"
        f.write_text("- not\n- a\n- mapping")
        with pytest.raises(MappingError) as exc:
            mapping_loader.load(source="file", path=str(f))
        assert "broken.yaml" in str(exc.value)


class TestValidateEdgeCases:
    def test_non_dict_top_level(self):
        with pytest.raises(MappingError, match="mapping must be a dict"):
            mapping_loader.validate("not a dict")  # type: ignore[arg-type]

    def test_source_not_dict(self):
        with pytest.raises(MappingError, match="`source` must be a dict"):
            mapping_loader.validate(
                {
                    "source": "value",
                    "extractions": [
                        {"asset": "a", "timestamp": "ts", "datapoints": {"x": {"value": "x"}}}
                    ],
                }
            )

    def test_rule_not_dict(self):
        with pytest.raises(MappingError, match=r"extractions\[0\] must be a dict"):
            mapping_loader.validate({"extractions": ["not a dict"]})

    def test_rule_level_timestamp_non_string(self):
        with pytest.raises(MappingError, match="timestamp must be a non-empty string when present"):
            mapping_loader.validate(
                {
                    "extractions": [
                        {
                            "asset": "a",
                            "timestamp": 5,
                            "datapoints": {"x": {"value": "x"}},
                        }
                    ]
                }
            )

    def test_datapoint_spec_not_dict(self):
        with pytest.raises(MappingError, match=r"datapoints\['x'\] must be a dict"):
            mapping_loader.validate(
                {"extractions": [{"asset": "a", "timestamp": "ts", "datapoints": {"x": "x"}}]}
            )

    def test_datapoint_empty_key(self):
        with pytest.raises(MappingError, match="non-string or empty key"):
            mapping_loader.validate(
                {
                    "extractions": [
                        {"asset": "a", "timestamp": "ts", "datapoints": {"": {"value": "x"}}}
                    ]
                }
            )

    def test_datapoint_timestamp_non_string(self):
        with pytest.raises(MappingError, match="timestamp must be a non-empty string"):
            mapping_loader.validate(
                {
                    "extractions": [
                        {
                            "asset": "a",
                            "datapoints": {"x": {"value": "x", "timestamp": 5}},
                        }
                    ]
                }
            )


class TestFromShorthandValidation:
    """`from` / `rename` / `cast` / `exclude` validation."""

    def _mapping(self, **rule_overrides):
        rule = {"asset": "a", "timestamp": "ts", "from": "readings"}
        rule.update(rule_overrides)
        return {"extractions": [rule]}

    def test_from_only_rule_is_valid(self):
        mapping = mapping_loader.validate(self._mapping())
        assert mapping["extractions"][0]["from"] == "readings"

    def test_datapoints_optional_when_from_present(self):
        mapping_loader.load_from_text(
            'extractions:\n  - asset: "a"\n    timestamp: "ts"\n    from: "readings"\n'
        )

    def test_rule_with_neither_from_nor_datapoints(self):
        with pytest.raises(MappingError, match="at least one of `from` or `datapoints`"):
            mapping_loader.validate({"extractions": [{"asset": "a", "timestamp": "ts"}]})

    def test_from_must_be_non_empty_string(self):
        for bad in ("", 5, None, []):
            with pytest.raises(MappingError, match="from must be a non-empty"):
                mapping_loader.validate(self._mapping(**{"from": bad}))

    def test_from_requires_rule_level_timestamp(self):
        with pytest.raises(MappingError, match="requires a non-empty rule-level"):
            mapping_loader.validate({"extractions": [{"asset": "a", "from": "readings"}]})

    def test_datapoints_still_non_empty_when_present(self):
        with pytest.raises(MappingError, match="datapoints must be a non-empty mapping"):
            mapping_loader.validate(self._mapping(datapoints={}))

    def test_rename_must_be_mapping(self):
        with pytest.raises(MappingError, match="rename must be a mapping"):
            mapping_loader.validate(self._mapping(rename=["a", "b"]))

    def test_rename_target_must_be_non_empty_string(self):
        with pytest.raises(MappingError, match=r"rename\['a'\] must be a non-empty"):
            mapping_loader.validate(self._mapping(rename={"a": ""}))

    def test_cast_must_be_mapping(self):
        with pytest.raises(MappingError, match="cast must be a mapping"):
            mapping_loader.validate(self._mapping(cast="float"))

    def test_cast_value_must_be_known_type(self):
        with pytest.raises(MappingError, match=r"cast\['a'\] must be one of"):
            mapping_loader.validate(self._mapping(cast={"a": "decimal"}))

    def test_cast_star_key_permitted(self):
        mapping_loader.validate(self._mapping(cast={"*": "float"}))

    def test_exclude_must_be_list(self):
        with pytest.raises(MappingError, match="exclude must be a list"):
            mapping_loader.validate(self._mapping(exclude="Station"))

    def test_exclude_entries_must_be_non_empty_strings(self):
        with pytest.raises(MappingError, match="exclude entries must be non-empty"):
            mapping_loader.validate(self._mapping(exclude=["ok", 5]))

    @pytest.mark.parametrize("modifier", ["rename", "cast", "exclude"])
    def test_modifier_without_from_is_an_error(self, modifier):
        """A rename with no `from` is almost certainly a mistake - do not ignore it."""
        rule = {
            "asset": "a",
            "timestamp": "ts",
            "datapoints": {"x": {"value": "x"}},
            modifier: {"a": "b"} if modifier != "exclude" else ["a"],
        }
        with pytest.raises(MappingError, match="only meaningful alongside `from`"):
            mapping_loader.validate({"extractions": [rule]})

    def test_full_shorthand_rule_round_trips(self):
        text = """
extractions:
  - asset: "station-{readings.Station}"
    timestamp: "readings.Timestamp"
    from: "readings"
    rename:
      SiteCode: Site
    cast:
      NetWeight: float
      "*": string
    exclude:
      - Station
"""
        rule = mapping_loader.load_from_text(text)["extractions"][0]
        assert rule["rename"] == {"SiteCode": "Site"}
        assert rule["cast"] == {"NetWeight": "float", "*": "string"}
        assert rule["exclude"] == ["Station"]

    def test_rule_tags_require_from(self):
        with pytest.raises(MappingError, match="only meaningful alongside `from`"):
            mapping_loader.validate(
                {
                    "extractions": [
                        {
                            "asset": "a",
                            "timestamp": "ts",
                            "datapoints": {"x": {"value": "x"}},
                            "tags": [{"name": "x"}],
                        }
                    ]
                }
            )

    def test_tags_cannot_combine_with_rename(self):
        with pytest.raises(MappingError, match="cannot be combined with `tags`"):
            mapping_loader.validate(
                self._mapping(
                    tags=[{"name": "a", "asset": "A", "datapoint": "alpha"}],
                    rename={"a": "alpha"},
                )
            )

    def test_duplicate_tag_names_rejected(self):
        with pytest.raises(MappingError, match="duplicate tag name"):
            mapping_loader.validate(
                self._mapping(tags=[{"name": "a"}, {"name": "a", "datapoint": "x"}])
            )

    def test_tags_wrapper_normalized(self):
        mapping = mapping_loader.validate(
            {
                "tags": {"tags": [{"name": "speed", "asset": "motor", "datapoint": "RPM"}]},
                "extractions": [{"asset": "a", "timestamp": "ts", "from": "@"}],
            }
        )
        assert mapping["tags"] == [
            {"name": "speed", "asset": "motor", "datapoint": "RPM"}
        ]
