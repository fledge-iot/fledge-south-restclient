"""User-defined JMESPath functions from the mapping document."""
from __future__ import annotations

from pathlib import Path

import jmespath
import pytest

from python.fledge.plugins.south.restclient import extraction, mapping_loader
from python.fledge.plugins.south.restclient.mapping_loader import MappingError
from python.fledge.plugins.south.restclient.user_functions import UserFunctionError, load_callables


_SLUG_SRC = """
def slug(value):
    if value is None:
        return None
    return re.sub(r"[^A-Za-z0-9]+", "-", str(value)).strip("-").lower()
"""

_MINIMAL_RULE = {
    "asset": "a",
    "timestamp": "ts",
    "datapoints": {"x": {"value": "x"}},
}


def test_slug_and_unit_conversion_via_from():
    mapping = {
        "functions": _SLUG_SRC
        + """
def f_to_c(f):
    return (float(f) - 32.0) * 5.0 / 9.0
""",
        "extractions": [
            {
                "asset": "sensor-{slug(name)}",
                "timestamp": "ts",
                "datapoints": {"temp_c": {"value": "f_to_c(temp_f)", "cast": "float"}},
            }
        ],
    }
    payload = {"name": "LINE A", "temp_f": 86, "ts": "2026-05-15T14:30:00.000Z"}
    tuples = extraction.extract(payload, mapping)
    assert len(tuples) == 1
    asset, name, value, _ts = tuples[0]
    assert asset == "sensor-line-a"
    assert name == "temp_c"
    assert value == 30.0


def test_user_function_composes_with_from_json():
    mapping = {
        "functions": "def upper(value):\n    return str(value).upper()\n",
        "extractions": [
            {
                "asset": "x",
                "timestamp": "'2026-01-01T00:00:00Z'",
                "datapoints": {"site": {"value": "upper(from_json(raw).SiteId)"}},
            }
        ],
    }
    tuples = extraction.extract({"raw": '{"SiteId": "site-a"}'}, mapping)
    assert tuples[0][1:3] == ("site", "SITE-A")


def test_runtime_error_drops_datapoint():
    mapping = {
        "functions": "def boom(value):\n    raise ValueError('nope')\n",
        "extractions": [
            {
                "asset": "x",
                "timestamp": "'2026-01-01T00:00:00Z'",
                "datapoints": {"a": {"value": "boom(x)"}},
            }
        ],
    }
    assert extraction.extract({"x": 1}, mapping) == []


def test_evaluate_with_compiled_options():
    mapping = {"functions": _SLUG_SRC, "extractions": [_MINIMAL_RULE]}
    options = extraction.options_for_mapping(mapping)
    assert extraction.evaluate("slug(name)", {"name": "Line 1"}, options=options) == "line-1"


def test_private_helpers_are_not_registered():
    src = """
def _norm(value):
    return str(value).strip().lower()

def slug(value):
    return _norm(value).replace(" ", "-")
"""
    mapping = {"functions": src, "extractions": [_MINIMAL_RULE]}
    options = extraction.options_for_mapping(mapping)
    assert extraction.evaluate("slug('LINE A')", {}, options=options) == "line-a"
    with pytest.raises(jmespath.exceptions.UnknownFunctionError):
        extraction.evaluate("_norm('LINE A')", {}, options=options)


def test_reserved_name_rejected_at_load():
    with pytest.raises(MappingError, match="collides"):
        mapping_loader.validate(
            {"functions": "def flatten(value):\n    return value\n", "extractions": [_MINIMAL_RULE]}
        )


def test_syntax_error_rejected_at_load():
    with pytest.raises(MappingError, match="syntax error"):
        mapping_loader.validate(
            {"functions": "def broken(\n", "extractions": [_MINIMAL_RULE]}
        )


def test_os_import_rejected():
    with pytest.raises(UserFunctionError, match="not allowed"):
        load_callables("import os\n\ndef ping(value):\n    return os.name\n")


def test_star_args_rejected():
    with pytest.raises(UserFunctionError, match=r"\*args"):
        load_callables("def add(*args):\n    return args\n")


def test_no_public_function_rejected():
    with pytest.raises(UserFunctionError, match="no public functions"):
        load_callables("def _helper(value):\n    return value\n")


def test_functions_file_is_loaded(tmp_path: Path):
    path = tmp_path / "funcs.py"
    path.write_text(_SLUG_SRC, encoding="utf-8")
    mapping = mapping_loader.validate(
        {"functionsFile": str(path), "extractions": [_MINIMAL_RULE]}
    )
    options = extraction.options_for_mapping(mapping)
    assert extraction.evaluate("slug('LINE A')", {}, options=options) == "line-a"


def test_relative_functions_file_resolved_from_mapping(tmp_path: Path):
    (tmp_path / "funcs.py").write_text(_SLUG_SRC, encoding="utf-8")
    mapping_path = tmp_path / "map.yaml"
    mapping_path.write_text(
        "functionsFile: funcs.py\n"
        "extractions:\n"
        "  - asset: '{slug(name)}'\n"
        "    timestamp: ts\n"
        "    datapoints:\n"
        "      x: { value: x }\n",
        encoding="utf-8",
    )
    mapping = mapping_loader.load(source="file", path=str(mapping_path))
    assert Path(mapping["functionsFile"]).is_file()
    tuples = extraction.extract(
        {"name": "LINE A", "x": 1, "ts": "2026-05-15T14:30:00.000Z"}, mapping
    )
    assert tuples[0][0] == "line-a"


def test_functions_must_be_string():
    with pytest.raises(MappingError, match="Python source string"):
        mapping_loader.validate({"functions": {"slug": "x"}, "extractions": [_MINIMAL_RULE]})
