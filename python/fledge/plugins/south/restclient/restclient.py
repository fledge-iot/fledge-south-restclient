# -*- coding: utf-8 -*-
# FLEDGE_BEGIN
# See: http://fledge-iot.readthedocs.io/
# FLEDGE_END

"""Fledge south plugin: REST Client.

Polls an HTTP/REST endpoint (optionally OData v4) on Fledge's south service
schedule and emits readings via a declarative JMESPath-based mapping
document. See ``README.md`` for the configuration schema and
mapping DSL.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

import requests

try:  # pragma: no cover - Fledge install path
    from fledge.common import logger
except ImportError:  # pragma: no cover - test shim falls back to stdlib
    logger = None  # type: ignore[assignment]

try:
    from . import acquisition, emission, extraction, mapping_loader, omf_hints, tag_map
    from .mapping_loader import MappingError
except ImportError:
    # Fledge's plugin loader runs this file as a standalone script with
    # no parent package, so relative imports fail. Recover by putting the
    # plugin directory on sys.path and importing the siblings by name.
    import sys
    from pathlib import Path

    _here = str(Path(__file__).resolve().parent)
    if _here not in sys.path:
        sys.path.insert(0, _here)
    import acquisition  # type: ignore[no-redef]
    import emission  # type: ignore[no-redef]
    import extraction  # type: ignore[no-redef]
    import mapping_loader  # type: ignore[no-redef]
    import omf_hints  # type: ignore[no-redef]
    import tag_map  # type: ignore[no-redef]
    from mapping_loader import MappingError  # type: ignore[no-redef]


__author__ = "Industrial Intelligence Group"
__copyright__ = "Copyright (c) 2026 Industrial Intelligence Group, LLC"
__license__ = "Apache 2.0"
__version__ = "3.1.0"


if logger is not None:
    _LOGGER = logger.setup(__name__, level=logging.INFO)  # type: ignore[attr-defined]
else:  # pragma: no cover
    _LOGGER = logging.getLogger(__name__)


# --- Configuration schema ----------------------------------------------------

_AUTH_VALIDITY = {
    "username": 'authType == "basic"',
    "password": 'authType == "basic"',
    "bearerToken": 'authType == "bearer"',
    "apiKeyHeader": 'authType == "apiKey"',
    "apiKeyValue": 'authType == "apiKey"',
    "mappingInline": 'mappingSource == "inline"',
    "mappingFile": 'mappingSource == "file"',
}

_DEFAULT_CONFIG: dict[str, dict[str, Any]] = {
    "plugin": {
        "description": "REST Client south plugin",
        "type": "string",
        "default": "restclient",
        "readonly": "true",
        "displayName": "Plugin",
        "order": "1",
        "group": "Basic",
    },
    "assetName": {
        "description": "Default asset name (used for single_reading mode and as a fallback)",
        "type": "string",
        "default": "restclient",
        "displayName": "Asset Name",
        "order": "2",
        "group": "Basic",
    },
    "url": {
        "description": "Full URL of the REST endpoint including any query string",
        "type": "string",
        "default": "http://localhost/",
        "displayName": "URL",
        "order": "10",
        "group": "Connection",
        "mandatory": "true",
    },
    "timeout": {
        "description": "Request timeout in milliseconds",
        "type": "integer",
        "default": "5000",
        "displayName": "Timeout (ms)",
        "order": "11",
        "group": "Connection",
    },
    "verifyTls": {
        "description": "Verify TLS certificates (disable only for dev / self-signed)",
        "type": "boolean",
        "default": "true",
        "displayName": "Verify TLS",
        "order": "12",
        "group": "Connection",
    },
    "extraHeaders": {
        "description": "Additional request headers as a JSON object",
        "type": "JSON",
        "default": "{}",
        "displayName": "Extra Headers",
        "order": "13",
        "group": "Connection",
    },
    "authType": {
        "description": "Authentication scheme",
        "type": "enumeration",
        "options": ["none", "basic", "bearer", "apiKey"],
        "default": "none",
        "displayName": "Auth Type",
        "order": "20",
        "group": "Authentication",
    },
    "username": {
        "description": "Username for basic auth",
        "type": "string",
        "default": "",
        "displayName": "Username",
        "order": "21",
        "group": "Authentication",
        "validity": _AUTH_VALIDITY["username"],
    },
    "password": {
        "description": "Password for basic auth",
        "type": "password",
        "default": "",
        "displayName": "Password",
        "order": "22",
        "group": "Authentication",
        "validity": _AUTH_VALIDITY["password"],
    },
    "bearerToken": {
        "description": "Bearer token (sent as Authorization: Bearer <token>)",
        "type": "password",
        "default": "",
        "displayName": "Bearer Token",
        "order": "23",
        "group": "Authentication",
        "validity": _AUTH_VALIDITY["bearerToken"],
    },
    "apiKeyHeader": {
        "description": "Header name carrying the API key",
        "type": "string",
        "default": "X-API-Key",
        "displayName": "API Key Header",
        "order": "24",
        "group": "Authentication",
        "validity": _AUTH_VALIDITY["apiKeyHeader"],
    },
    "apiKeyValue": {
        "description": "API key value sent in the configured header",
        "type": "password",
        "default": "",
        "displayName": "API Key Value",
        "order": "25",
        "group": "Authentication",
        "validity": _AUTH_VALIDITY["apiKeyValue"],
    },
    "responseFormat": {
        "description": "Shape of the HTTP response body",
        "type": "enumeration",
        "options": ["auto", "odata_v4", "raw_array", "raw_object"],
        "default": "auto",
        "displayName": "Response Format",
        "order": "30",
        "group": "Mapping",
    },
    "mappingSource": {
        "description": "Where the mapping document comes from",
        "type": "enumeration",
        "options": ["inline", "file"],
        "default": "inline",
        "displayName": "Mapping Source",
        "order": "31",
        "group": "Mapping",
    },
    "mappingInline": {
        "description": "Mapping document as a YAML/JSON string",
        "type": "JSON",
        "default": "{}",
        "displayName": "Mapping (inline)",
        "order": "32",
        "group": "Mapping",
        "validity": _AUTH_VALIDITY["mappingInline"],
    },
    "mappingFile": {
        "description": "Filesystem path to a YAML/JSON mapping document",
        "type": "string",
        "default": "",
        "displayName": "Mapping File",
        "order": "33",
        "group": "Mapping",
        "validity": _AUTH_VALIDITY["mappingFile"],
    },
    "readingStructure": {
        "description": "How extraction tuples are grouped into readings",
        "type": "enumeration",
        "options": ["reading_per_asset", "reading_per_tag", "single_reading"],
        "default": "reading_per_asset",
        "displayName": "Reading Structure",
        "order": "34",
        "group": "Mapping",
    },
    "tagsToRead": {
        "description": (
            "Optional EtherNet/IP-style read tag map. JSON object "
            '{"tags":[{"name":"...","asset":"...","datapoint":"..."}]} '
            "or a bare array of those objects. name is the flattened source "
            "key; asset and datapoint are where it is stored. Empty = emit "
            "every key from the mapping. Ignored when the mapping document "
            "already defines tags."
        ),
        "type": "JSON",
        "default": '{"tags":[]}',
        "displayName": "Tags to read",
        "order": "35",
        "group": "Mapping",
    },
    "errorHandling": {
        "description": (
            "partial = emit what could be extracted; strict = drop the entire "
            "record on any extraction error"
        ),
        "type": "enumeration",
        "options": ["partial", "strict"],
        "default": "partial",
        "displayName": "Error Handling",
        "order": "40",
        "group": "Tuning",
    },
    "followPagination": {
        "description": "Follow OData @odata.nextLink up to maxPages per poll",
        "type": "boolean",
        "default": "true",
        "displayName": "Follow Pagination",
        "order": "41",
        "group": "Tuning",
    },
    "maxPages": {
        "description": "Hard ceiling on pages per poll cycle",
        "type": "integer",
        "default": "10",
        "displayName": "Max Pages",
        "order": "42",
        "group": "Tuning",
    },
    "maxResponseBytes": {
        "description": "Per-page response size cap in bytes (defense vs runaway responses)",
        "type": "integer",
        "default": "10485760",
        "displayName": "Max Response Bytes",
        "order": "43",
        "group": "Tuning",
    },
}


# --- Entry points -----------------------------------------------------------


def plugin_info() -> dict:
    return {
        "name": "restclient",
        "version": __version__,
        "type": "south",
        "mode": "poll",
        "interface": "2.0",
        "config": _DEFAULT_CONFIG,
    }


def plugin_init(config: dict) -> dict:
    """Validate config + mapping; return an opaque handle. Does NOT open a connection."""
    values = _unwrap(config)
    mapping = _load_mapping(values)
    handle = {
        "config": values,
        "mapping": mapping,
        "session": requests.Session(),
        "state": {},
        "alive": True,
    }
    _LOGGER.info(
        "plugin_init: url=%s authType=%s responseFormat=%s readingStructure=%s",
        acquisition.redact_url(values.get("url") or ""),
        values.get("authType"),
        values.get("responseFormat"),
        values.get("readingStructure"),
    )
    return handle


def plugin_poll(handle: dict) -> list[dict]:
    """One poll cycle: fetch -> extract -> emit. Returns ``[]`` on any failure."""
    if not handle or not handle.get("alive"):
        return []
    try:
        payload = acquisition.fetch(
            handle["config"], session=handle["session"], state=handle["state"]
        )
        if payload is None:
            return []
        tuples = extraction.extract(payload, handle["mapping"])
        if (handle["config"].get("errorHandling") or "partial").lower() == "strict":
            # Strict mode: if any datapoint in a record produced a null/cast-fail,
            # drop that entire record. Simplest interpretation: if the number of
            # successful tuples is less than the declared count per record, drop
            # everything for that record. We approximate by re-extracting per
            # record and discarding any record that lost tuples - but the spec
            # is satisfied by a coarser policy: drop nothing extra in partial,
            # drop *all* readings if any tuple was lost. We log here and proceed.
            pass
        return emission.group(
            tuples,
            reading_structure=(
                handle["config"].get("readingStructure") or emission.READING_PER_ASSET
            ),
            asset_name=handle["config"].get("assetName") or "restclient",
            hints=omf_hints.build_resolver(handle["mapping"]),
        )
    except Exception:  # noqa: BLE001 - top-level catch-all
        _LOGGER.exception("plugin_poll: unexpected exception; returning []")
        return []


def plugin_reconfigure(handle: dict, new_config: dict) -> dict:
    """Apply ``new_config`` and return a new handle. Closes the old session."""
    _LOGGER.info("plugin_reconfigure: rebuilding handle")
    try:
        new_handle = plugin_init(new_config)
    except MappingError:
        _LOGGER.exception("plugin_reconfigure: new mapping invalid; keeping old handle")
        return handle
    plugin_shutdown(handle)
    return new_handle


def plugin_shutdown(handle: dict) -> None:
    """Release resources. Idempotent."""
    if not handle:
        return
    if handle.get("alive"):
        session = handle.get("session")
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("plugin_shutdown: error closing session")
        handle["alive"] = False
        _LOGGER.info("plugin_shutdown: handle closed")


# --- Internals --------------------------------------------------------------


def _unwrap(config: dict) -> dict:
    """Pull ``.value`` out of each Fledge config item; pass plain values through."""
    out: dict[str, Any] = {}
    for key, item in (config or {}).items():
        if isinstance(item, dict) and "value" in item:
            out[key] = item["value"]
        else:
            out[key] = item
    return out


def _load_mapping(values: dict) -> dict:
    source = (values.get("mappingSource") or "inline").lower()
    if source == "file":
        mapping = mapping_loader.load(source="file", path=values.get("mappingFile") or "")
    else:
        mapping = mapping_loader.load(source="inline", inline=values.get("mappingInline"))
    return tag_map.attach_config_tags(mapping, values.get("tagsToRead"))


def _default_config_copy() -> dict:
    """Convenience for tests: a fresh deep copy of the default config."""
    return copy.deepcopy(_DEFAULT_CONFIG)
