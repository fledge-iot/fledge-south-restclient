from __future__ import annotations

import json
from pathlib import Path

from complete import complete

REPO = Path(__file__).resolve().parents[2]
PAYLOAD = json.loads(
    (REPO / "tests" / "fixtures" / "payloads" / "batch_runs_single_active.json").read_text(
        encoding="utf-8"
    )
)


def _labels(query: str, cursor: int | None = None, context=None):
    ctx = PAYLOAD if context is None else context
    pos = len(query) if cursor is None else cursor
    return [item["label"] for item in complete(query, pos, ctx)["suggestions"]]


def test_from_json_dot_offers_inner_keys():
    query = "from_json(value[0].Data)."
    labels = _labels(query)
    assert ".M01" in labels
    assert ".M03" in labels


def test_partial_key_filters():
    query = "from_json(value[0].Data).M0"
    labels = _labels(query)
    assert labels == [".M01", ".M03"]


def test_function_prefix():
    labels = _labels("from_")
    assert "from_json" in labels
    assert "to_array" not in labels
    assert "flatten" in _labels("flat")


def test_dollar_vars():
    labels = _labels("$")
    assert labels == ["$item", "$parent"]
    assert _labels("$ite") == ["$item"]


def test_array_suffixes_on_value():
    labels = _labels("value")
    assert "[0]" in labels
    assert "[]" in labels


def test_quoted_odata_key():
    query = ""
    labels = _labels(query)
    assert any(label.startswith('["@odata') for label in labels)


def test_incomplete_prefix_does_not_crash():
    result = complete("from_json(value[0].Dat", len("from_json(value[0].Dat"), PAYLOAD)
    assert "suggestions" in result
    assert isinstance(result["suggestions"], list)


def test_user_function_prefix():
    extra = [("slug", "user - playground functions box")]
    labels = [
        item["label"]
        for item in complete("slu", 3, PAYLOAD, extra_functions=extra)["suggestions"]
    ]
    assert "slug" in labels


def test_flatten_dot_offers_leaf_keys():
    site = {
        "Data": '{"SiteId":"SITE-A","Lines":[{"LineId":"Line1","Equipment":[{"EquipmentId":"Tank01","Readings":{"Level":62.5}}]}]}'
    }
    query = "flatten(from_json(Data), ['SiteId', 'LineId', 'EquipmentId'])."
    labels = [
        item["label"]
        for item in complete(query, len(query), site)["suggestions"]
    ]
    assert any("Readings_Level" in label for label in labels)


def test_item_keys_when_bound():
    scope = {"_item": {"key": "M01", "value": {"Import": 1}}, "_parent": PAYLOAD}
    query = "$item."
    labels = [
        item["label"]
        for item in complete(query, len(query), scope)["suggestions"]
    ]
    assert ".key" in labels
    assert ".value" in labels
