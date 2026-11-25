from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import app

client = TestClient(app)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures"
PAYLOAD = json.loads(
    (FIXTURES / "payloads" / "batch_runs_single_active.json").read_text(encoding="utf-8")
)
MAPPING = (FIXTURES / "mappings" / "batch_runs_single_active.yaml").read_text(
    encoding="utf-8"
)


def test_evaluate_from_json_on_fixture():
    resp = client.post(
        "/api/evaluate",
        json={
            "json": PAYLOAD,
            "query": "from_json(value[0].Data).M01.Import",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["result"] == 20.14


def test_evaluate_syntax_error_is_visible():
    resp = client.post(
        "/api/evaluate",
        json={"json": {"a": 1}, "query": "foo."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] is None
    assert body["error"]


def test_evaluate_empty_query():
    resp = client.post("/api/evaluate", json={"json": {"a": 1}, "query": "   "})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] is None
    assert body["error"] is None


def test_evaluate_bind_iteration():
    payload = {"key": "M01", "LineId": "1"}
    resp = client.post(
        "/api/evaluate",
        json={
            "json": payload,
            "query": "$item.key",
            "bind_iteration": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"] == "M01"

    parent = client.post(
        "/api/evaluate",
        json={
            "json": payload,
            "query": "$parent.LineId",
            "bind_iteration": True,
        },
    )
    assert parent.json()["result"] == "1"


def test_extract_fixture_mapping():
    resp = client.post(
        "/api/extract",
        json={"json": PAYLOAD, "mapping": MAPPING},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    names = {(row["asset"], row["datapoint"]) for row in body["tuples"]}
    assert ("EnergyMeter-1", "energy.m01.import") in names
    imported = next(
        row for row in body["tuples"] if row["datapoint"] == "energy.m01.import"
    )
    assert imported["value"] == 20.14


def test_extract_invalid_mapping():
    resp = client.post(
        "/api/extract",
        json={"json": PAYLOAD, "mapping": "extractions: []"},
    )
    assert resp.status_code == 200
    assert resp.json()["tuples"] == []
    assert "extractions" in resp.json()["error"]


def test_complete_from_json_inner_keys():
    inner = json.loads(PAYLOAD["value"][0]["Data"])
    resp = client.post(
        "/api/complete",
        json={
            "json": PAYLOAD,
            "query": "from_json(value[0].Data).",
            "cursor": len("from_json(value[0].Data)."),
        },
    )
    assert resp.status_code == 200
    labels = {item["label"] for item in resp.json()["suggestions"]}
    assert labels == {f".{key}" for key in inner}


def test_examples_include_fixture():
    resp = client.get("/api/examples")
    assert resp.status_code == 200
    names = {item["name"] for item in resp.json()["examples"]}
    assert "batch_runs_single_active" in names
    match = next(
        item
        for item in resp.json()["examples"]
        if item["name"] == "batch_runs_single_active"
    )
    assert match["payload"]["value"][0]["LineId"] == "LINE-A"
    assert "from_json" in match["mapping"]


def test_index_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "JMESPath Playground" in resp.text
    assert "User functions" in resp.text
    assert "flatten" in resp.text


def test_evaluate_flatten_double_quoted_keys_on_site():
    payload = json.loads(
        (FIXTURES / "payloads" / "site_snapshot.json").read_text(encoding="utf-8")
    )
    resp = client.post(
        "/api/evaluate",
        json={
            "json": payload,
            "query": 'flatten(from_json(value[0].Data),["SiteId"])',
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert "SiteId" not in body["result"]
    assert body["result"]["SITE-A_Lines_0_Equipment_0_Readings_Level"] == 76.412


def test_evaluate_flatten_hierarchy():
    payload = json.loads(
        (FIXTURES / "payloads" / "flatten_hierarchy.json").read_text(encoding="utf-8")
    )
    resp = client.post(
        "/api/evaluate",
        json={
            "json": payload,
            "query": (
                "flatten(from_json(value[0].Data), "
                "['SiteId', 'LineId', 'EquipmentId'])"
            ),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["result"]["SITE-A_Line1_Tank01_Readings_Level"] == 62.5
    assert body["result"]["SITE-A_Line2_Pump02_Readings_FlowRate"] == 11.2


def test_evaluate_user_function():
    resp = client.post(
        "/api/evaluate",
        json={
            "json": {"name": "LINE A"},
            "query": "slug(name)",
            "functions": (
                "def slug(value):\n"
                "    return re.sub(r'[^A-Za-z0-9]+', '-', str(value))"
                ".strip('-').lower()\n"
            ),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["result"] == "line-a"
    assert body["functions"] == ["slug"]


def test_evaluate_bad_functions_is_visible():
    resp = client.post(
        "/api/evaluate",
        json={
            "json": {"name": "x"},
            "query": "slug(name)",
            "functions": "def broken(\n",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] is None
    assert body["error"].startswith("functions:")


def test_evaluate_comment_only_functions_is_noop():
    resp = client.post(
        "/api/evaluate",
        json={
            "json": PAYLOAD,
            "query": "from_json(value[0].Data).M01.Import",
            "functions": "# just a comment\n",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["result"] == 20.14
    assert body["functions"] == []


def test_extract_uses_functions_box_when_mapping_has_none():
    resp = client.post(
        "/api/extract",
        json={
            "json": {"name": "LINE A", "x": 1, "ts": "2026-05-15T14:30:00.000Z"},
            "mapping": (
                "extractions:\n"
                "  - asset: '{slug(name)}'\n"
                "    timestamp: ts\n"
                "    datapoints:\n"
                "      x: { value: x }\n"
            ),
            "functions": (
                "def slug(value):\n"
                "    return str(value).lower().replace(' ', '-')\n"
            ),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["tuples"][0]["asset"] == "line-a"
    assert body["functions"] == ["slug"]


def test_complete_includes_user_function():
    resp = client.post(
        "/api/complete",
        json={
            "json": {"name": "LINE A"},
            "query": "slu",
            "cursor": 3,
            "functions": "def slug(value):\n    return value\n",
        },
    )
    assert resp.status_code == 200
    labels = {item["label"] for item in resp.json()["suggestions"]}
    assert "slug" in labels


def test_examples_include_flatten_query_and_user_functions():
    resp = client.get("/api/examples")
    assert resp.status_code == 200
    by_name = {item["name"]: item for item in resp.json()["examples"]}
    assert "flatten" in by_name["flatten_hierarchy"]["query"]
    assert "def slug" in (by_name["user_functions"]["functions"] or "")
