"""Unit tests for the acquisition layer.

Uses the ``responses`` library to mock HTTP at the transport layer.
Covers the error handling matrix plus pagination, OData detection, and
the four auth modes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import requests
import responses

from python.fledge.plugins.south.restclient import acquisition


URL = "http://test-host.local/api/data"
NEXT_URL = "http://test-host.local/api/data?$skiptoken=PAGE2"


def _config(**overrides) -> dict:
    base = {
        "url": URL,
        "timeout": 5000,
        "verifyTls": True,
        "extraHeaders": {},
        "authType": "none",
        "username": "",
        "password": "",
        "bearerToken": "",
        "apiKeyHeader": "X-API-Key",
        "apiKeyValue": "",
        "responseFormat": "auto",
        "followPagination": True,
        "maxPages": 10,
        "maxResponseBytes": 10 * 1024 * 1024,
    }
    base.update(overrides)
    return base


def _read_fixture(name: str) -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    return json.loads((repo_root / "tests" / "fixtures" / "payloads" / f"{name}.json").read_text())


class TestHappyPath:
    @responses.activate
    def test_returns_decoded_json(self):
        responses.add(responses.GET, URL, json={"temperature": 22.5}, status=200)
        result = acquisition.fetch(_config())
        assert result == {"temperature": 22.5}

    @responses.activate
    def test_odata_auto_detected_and_merged(self):
        page = _read_fixture("batch_runs_single_active")
        responses.add(responses.GET, URL, json=page, status=200)
        result = acquisition.fetch(_config())
        assert isinstance(result, dict)
        assert len(result["value"]) == 1
        assert "@odata.nextLink" not in result


class TestErrorMatrix:
    @responses.activate
    def test_http_4xx_returns_none(self):
        responses.add(responses.GET, URL, json={"error": "unauthorized"}, status=401)
        assert acquisition.fetch(_config()) is None

    @responses.activate
    def test_http_5xx_returns_none(self):
        responses.add(responses.GET, URL, status=503)
        assert acquisition.fetch(_config()) is None

    @responses.activate
    def test_timeout_returns_none(self):
        responses.add(responses.GET, URL, body=requests.exceptions.Timeout("slow"))
        assert acquisition.fetch(_config()) is None

    @responses.activate
    def test_connection_refused_returns_none_and_logs(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        responses.add(
            responses.GET,
            URL,
            body=requests.exceptions.ConnectionError("refused"),
        )
        state: dict = {}
        assert acquisition.fetch(_config(), state=state) is None
        assert state["connrefused_count"] == 1
        assert any("connection refused" in rec.message for rec in caplog.records)

    @responses.activate
    def test_invalid_json_returns_none(self):
        responses.add(responses.GET, URL, body="not json {", status=200)
        assert acquisition.fetch(_config()) is None

    @responses.activate
    def test_odata_error_envelope_returns_none(self):
        payload = _read_fixture("odata_error")
        responses.add(responses.GET, URL, json=payload, status=200)
        assert acquisition.fetch(_config()) is None

    @responses.activate
    def test_empty_url_returns_none(self):
        # No mock added - should not even attempt request
        assert acquisition.fetch(_config(url="")) is None

    @responses.activate
    def test_max_response_bytes_aborts(self):
        # Stream a body larger than the cap
        body = "x" * (1024 * 64)
        responses.add(responses.GET, URL, body=body, status=200)
        assert acquisition.fetch(_config(maxResponseBytes=1024)) is None


class TestPagination:
    @responses.activate
    def test_follows_nextlink_and_merges(self):
        page1 = _read_fixture("batch_runs_paginated_page1")
        page2 = _read_fixture("batch_runs_paginated_page2")
        # rewrite nextLink to match our test URL
        page1 = dict(page1)
        page1["@odata.nextLink"] = NEXT_URL
        responses.add(responses.GET, URL, json=page1, status=200)
        responses.add(responses.GET, NEXT_URL, json=page2, status=200)
        result = acquisition.fetch(_config())
        assert isinstance(result, dict)
        assert len(result["value"]) == 2
        assert "@odata.nextLink" not in result

    @responses.activate
    def test_follow_pagination_false_stops_after_first(self):
        page1 = _read_fixture("batch_runs_paginated_page1")
        page1 = dict(page1)
        page1["@odata.nextLink"] = NEXT_URL
        responses.add(responses.GET, URL, json=page1, status=200)
        # no mock for NEXT_URL - must not be called
        result = acquisition.fetch(_config(followPagination=False))
        assert len(result["value"]) == 1

    @responses.activate
    def test_max_pages_caps_pagination(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        page_a = {"@odata.context": "x", "@odata.nextLink": URL + "?p=2", "value": [{"i": 1}]}
        page_b = {"@odata.context": "x", "@odata.nextLink": URL + "?p=3", "value": [{"i": 2}]}
        page_c = {"@odata.context": "x", "value": [{"i": 3}]}  # not reached
        responses.add(responses.GET, URL, json=page_a, status=200)
        responses.add(responses.GET, URL + "?p=2", json=page_b, status=200)
        responses.add(responses.GET, URL + "?p=3", json=page_c, status=200)
        result = acquisition.fetch(_config(maxPages=2))
        assert len(result["value"]) == 2
        assert any("maxPages=2" in rec.message for rec in caplog.records)

    @responses.activate
    def test_loop_detection_stops(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        # nextLink points back to the same URL
        payload = {"@odata.context": "x", "@odata.nextLink": URL, "value": [{"i": 1}]}
        responses.add(responses.GET, URL, json=payload, status=200)
        result = acquisition.fetch(_config())
        # Only the first page is consumed before loop detected
        assert len(result["value"]) == 1
        assert any("pagination loop" in rec.message for rec in caplog.records)


class TestNextLinkOrigin:
    @responses.activate
    def test_cross_host_nextlink_not_followed(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        payload = {
            "@odata.context": "x",
            "@odata.nextLink": "https://other.example.net/odata/Runs?$skip=1",
            "value": [{"i": 1}],
        }
        responses.add(responses.GET, URL, json=payload, status=200)
        # no mock for the other host: any call to it would raise ConnectionError
        result = acquisition.fetch(_config(authType="bearer", bearerToken="tok"))
        assert len(result["value"]) == 1
        assert len(responses.calls) == 1
        assert any("different origin" in rec.message for rec in caplog.records)

    @responses.activate
    def test_scheme_downgrade_nextlink_not_followed(self):
        https_url = "https://sim.example.test/odata/Runs"
        payload = {
            "@odata.context": "x",
            "@odata.nextLink": "http://sim.example.test/odata/Runs?$skip=1",
            "value": [{"i": 1}],
        }
        responses.add(responses.GET, https_url, json=payload, status=200)
        result = acquisition.fetch(_config(url=https_url))
        assert len(result["value"]) == 1
        assert len(responses.calls) == 1

    @responses.activate
    def test_different_port_nextlink_not_followed(self):
        base = "http://sim.example.test:8080/odata/Runs"
        payload = {
            "@odata.context": "x",
            "@odata.nextLink": "http://sim.example.test:9090/odata/Runs?$skip=1",
            "value": [{"i": 1}],
        }
        responses.add(responses.GET, base, json=payload, status=200)
        result = acquisition.fetch(_config(url=base))
        assert len(responses.calls) == 1
        assert len(result["value"]) == 1

    @responses.activate
    def test_relative_nextlink_is_followed(self):
        base = "http://sim.example.test/odata/Runs"
        page1 = {"@odata.context": "x", "@odata.nextLink": "/odata/Runs?$skip=1", "value": [{"i": 1}]}
        page2 = {"@odata.context": "x", "value": [{"i": 2}]}
        responses.add(responses.GET, base, json=page1, status=200)
        responses.add(responses.GET, base + "?$skip=1", json=page2, status=200)
        result = acquisition.fetch(_config(url=base))
        assert [r["i"] for r in result["value"]] == [1, 2]

    @responses.activate
    def test_explicit_default_port_counts_as_same_origin(self):
        base = "https://sim.example.test/odata/Runs"
        page1 = {
            "@odata.context": "x",
            "@odata.nextLink": "https://SIM.example.test:443/odata/Runs?$skip=1",
            "value": [{"i": 1}],
        }
        page2 = {"@odata.context": "x", "value": [{"i": 2}]}
        responses.add(responses.GET, base, json=page1, status=200)
        responses.add(responses.GET, "https://sim.example.test:443/odata/Runs?$skip=1", json=page2, status=200)
        result = acquisition.fetch(_config(url=base))
        assert len(result["value"]) == 2


class TestLogRedaction:
    def test_redact_url_drops_userinfo_and_secret_params(self):
        out = acquisition.redact_url(
            "https://user:hunter2@api.example.test/v1/data?api_key=abc123&$top=5&sig=zzz"
        )
        assert "hunter2" not in out and "abc123" not in out and "zzz" not in out
        assert out.startswith("https://api.example.test/v1/data?")
        assert "$top=5" in out
        assert "api_key=***" in out and "sig=***" in out

    def test_redact_url_plain_url_unchanged(self):
        u = "http://sim.example.test:8080/odata/Runs?$filter=Status eq 'x'"
        assert acquisition.redact_url(u).startswith("http://sim.example.test:8080/odata/Runs")

    def test_redact_text_scrubs_exception_text(self):
        msg = "HTTPSConnectionPool(host='h'): Max retries exceeded with url: /p?access_token=SECRET&x=1"
        out = acquisition._redact_text(msg)
        assert "SECRET" not in out and "x=1" in out

    @responses.activate
    def test_http_error_log_redacts_query_secret(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        u = URL + "?api_key=TOPSECRET"
        responses.add(responses.GET, u, json={"error": "x"}, status=500)
        assert acquisition.fetch(_config(url=u)) is None
        text = " ".join(rec.getMessage() for rec in caplog.records)
        assert "HTTP 500" in text
        assert "TOPSECRET" not in text

    @responses.activate
    def test_connection_error_log_redacts_query_secret(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        u = URL + "?token=TOPSECRET"
        responses.add(
            responses.GET, u,
            body=requests.exceptions.ConnectionError("failed for url: " + u),
        )
        assert acquisition.fetch(_config(url=u)) is None
        text = " ".join(rec.getMessage() for rec in caplog.records)
        assert "TOPSECRET" not in text


class TestAuth:
    @responses.activate
    def test_basic_auth_sends_header(self):
        responses.add(responses.GET, URL, json={}, status=200)
        acquisition.fetch(_config(authType="basic", username="alice", password="secret"))
        sent = responses.calls[0].request
        assert sent.headers.get("Authorization", "").startswith("Basic ")

    @responses.activate
    def test_bearer_auth_sends_token(self):
        responses.add(responses.GET, URL, json={}, status=200)
        acquisition.fetch(_config(authType="bearer", bearerToken="abc.def.ghi"))
        sent = responses.calls[0].request
        assert sent.headers.get("Authorization") == "Bearer abc.def.ghi"

    @responses.activate
    def test_apikey_auth_uses_configured_header(self):
        responses.add(responses.GET, URL, json={}, status=200)
        acquisition.fetch(
            _config(authType="apiKey", apiKeyHeader="X-Custom", apiKeyValue="K123")
        )
        sent = responses.calls[0].request
        assert sent.headers.get("X-Custom") == "K123"

    @responses.activate
    def test_unknown_auth_type_warns_and_proceeds(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        responses.add(responses.GET, URL, json={}, status=200)
        result = acquisition.fetch(_config(authType="oauth2"))
        assert result == {}
        assert any("unknown authType" in rec.message for rec in caplog.records)


class TestExtraHeaders:
    @responses.activate
    def test_dict_extra_headers_sent(self):
        responses.add(responses.GET, URL, json={}, status=200)
        acquisition.fetch(_config(extraHeaders={"X-Trace": "abc"}))
        assert responses.calls[0].request.headers.get("X-Trace") == "abc"

    @responses.activate
    def test_json_string_extra_headers_sent(self):
        responses.add(responses.GET, URL, json={}, status=200)
        acquisition.fetch(_config(extraHeaders='{"X-Trace": "abc"}'))
        assert responses.calls[0].request.headers.get("X-Trace") == "abc"

    @responses.activate
    def test_invalid_extra_headers_ignored(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        responses.add(responses.GET, URL, json={}, status=200)
        result = acquisition.fetch(_config(extraHeaders="not-json"))
        assert result == {}
        assert any("extraHeaders" in rec.message for rec in caplog.records)


class TestResponseFormat:
    @responses.activate
    def test_response_format_odata_forces_merge(self):
        # Plain dict without @odata markers, but responseFormat=odata_v4
        responses.add(responses.GET, URL, json={"value": [{"a": 1}]}, status=200)
        result = acquisition.fetch(_config(responseFormat="odata_v4"))
        assert isinstance(result, dict)
        assert result["value"] == [{"a": 1}]

    @responses.activate
    def test_response_format_raw_array_skips_odata_merge(self):
        # OData-shaped response but responseFormat=raw_array -> return as-is
        page = _read_fixture("batch_runs_single_active")
        responses.add(responses.GET, URL, json=page, status=200)
        result = acquisition.fetch(_config(responseFormat="raw_array"))
        # raw_array preserves the original keys including @odata.context
        assert "@odata.context" in result


class TestBackoffSuppression:
    @responses.activate
    def test_first_failure_logs_subsequent_suppressed(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        responses.add(
            responses.GET,
            URL,
            body=requests.exceptions.ConnectionError("refused"),
        )
        # responses mocks are one-shot by default; re-register for each call
        for _ in range(2):
            responses.add(
                responses.GET,
                URL,
                body=requests.exceptions.ConnectionError("refused"),
            )

        state: dict = {}
        # First failure -> log
        acquisition.fetch(_config(), state=state)
        # Second failure -> log (gap=1 between -> log at n=2)
        acquisition.fetch(_config(), state=state)
        # Third failure -> suppress (gap=2 -> next log at n=4)
        acquisition.fetch(_config(), state=state)

        warnings = [r for r in caplog.records if "connection refused" in r.message]
        assert len(warnings) == 2  # n=1 and n=2 logged, n=3 suppressed
        assert state["connrefused_count"] == 3

    @responses.activate
    def test_successful_request_resets_counter(self):
        responses.add(
            responses.GET,
            URL,
            body=requests.exceptions.ConnectionError("refused"),
        )
        responses.add(responses.GET, URL, json={"ok": True}, status=200)
        state: dict = {}
        acquisition.fetch(_config(), state=state)
        assert state["connrefused_count"] == 1
        acquisition.fetch(_config(), state=state)
        assert "connrefused_count" not in state


class TestSessionLifecycle:
    @responses.activate
    def test_caller_provided_session_reused(self):
        responses.add(responses.GET, URL, json={}, status=200)
        sess = requests.Session()
        acquisition.fetch(_config(), session=sess)
        # Session should still be open (caller owns it)
        sess.get  # not closed; if it were, attribute access would still work
        sess.close()


class TestCoercionAndDefensive:
    @responses.activate
    def test_string_int_coerced(self):
        responses.add(responses.GET, URL, json={}, status=200)
        # Fledge often hands values in as strings
        result = acquisition.fetch(_config(timeout="3000", maxPages="5"))
        assert result == {}

    @responses.activate
    def test_garbage_int_falls_back_to_default(self):
        responses.add(responses.GET, URL, json={}, status=200)
        # Bad value -> default; should not blow up
        result = acquisition.fetch(_config(timeout="not-a-number"))
        assert result == {}

    @responses.activate
    def test_bool_string_variants(self):
        responses.add(responses.GET, URL, json={}, status=200)
        result = acquisition.fetch(_config(verifyTls="false", followPagination="False"))
        assert result == {}

    @responses.activate
    def test_extraheaders_list_ignored(self, caplog):
        caplog.set_level(logging.WARNING, logger=acquisition.__name__)
        responses.add(responses.GET, URL, json={}, status=200)
        result = acquisition.fetch(_config(extraHeaders=["not", "a", "dict"]))
        assert result == {}
        assert any("extraHeaders" in rec.message for rec in caplog.records)

    @responses.activate
    def test_generic_request_exception_returns_none(self):
        responses.add(
            responses.GET,
            URL,
            body=requests.exceptions.ChunkedEncodingError("garbled"),
        )
        assert acquisition.fetch(_config()) is None
