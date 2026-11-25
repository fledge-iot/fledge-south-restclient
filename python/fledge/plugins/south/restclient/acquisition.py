"""HTTP acquisition layer.

Issues GET requests against the configured URL, optionally following
OData v4 ``@odata.nextLink`` pagination, applies authentication, enforces
a response-size cap, and returns the decoded JSON payload. Returns
``None`` on any failure - the plugin
layer converts ``None`` to an empty readings list so Fledge never sees
stale data.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from requests.auth import HTTPBasicAuth


_LOGGER = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_MS = 5000
_DEFAULT_MAX_PAGES = 10
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_CHUNK = 8192

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SECRET_PARAM_RE = re.compile(
    r"(token|key|secret|passw|pwd|sig|auth|credential|code|session)", re.IGNORECASE
)
_URL_USERINFO_RE = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@?#]+@")
_URL_SECRET_QS_RE = re.compile(
    r"(?i)([?&;][^=&\s#'\"]*"
    r"(?:token|key|secret|passw|pwd|sig|auth|credential|code|session)"
    r"[^=&\s#'\"]*=)[^&\s#'\"]*"
)


def redact_url(url: str) -> str:
    """Return ``url`` that is safe to log.

    Drops ``user:password@`` credentials and replaces the value of query
    parameters whose name looks like a secret (token, key, secret, password,
    signature, auth, code, session) with ``***``. Other parameters, such as
    OData ``$filter``, are kept because they help with diagnosis.
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "<unparseable url>"
    netloc = parts.netloc.rsplit("@", 1)[-1]
    query = parts.query
    if query:
        pairs = parse_qsl(query, keep_blank_values=True)
        query = urlencode(
            [(k, "***" if _SECRET_PARAM_RE.search(k) else v) for k, v in pairs],
            safe="$*",
        )
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def _redact_text(text: object) -> str:
    """Redact credentials and secret query values from free text (e.g. an exception)."""
    out = _URL_USERINFO_RE.sub(r"\1", str(text))
    return _URL_SECRET_QS_RE.sub(r"\1***", out)


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """Return ``(scheme, host, port)`` for ``url`` with default ports filled in."""
    try:
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if not scheme or not host:
            return None
        return scheme, host, parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None


def fetch(
    config: dict,
    session: requests.Session | None = None,
    state: dict | None = None,
) -> dict | list | None:
    """Fetch the configured URL, following OData pagination if applicable.

    Returns the decoded JSON payload (a merged dict for paginated OData
    responses) or ``None`` on any error. The plugin layer
    is responsible for converting ``None`` to ``[]``.

    ``state``, when provided, is mutated to track cross-poll counters
    (currently: consecutive-connection-refused backoff log suppression).
    """
    if state is None:
        state = {}
    owns_session = session is None
    if owns_session:
        session = requests.Session()

    try:
        url = (config.get("url") or "").strip()
        if not url:
            _LOGGER.warning("acquisition.fetch: url is empty")
            return None

        timeout_s = _coerce_int(config.get("timeout"), _DEFAULT_TIMEOUT_MS) / 1000.0
        max_pages = max(1, _coerce_int(config.get("maxPages"), _DEFAULT_MAX_PAGES))
        max_bytes = max(1, _coerce_int(config.get("maxResponseBytes"), _DEFAULT_MAX_BYTES))
        verify_tls = _coerce_bool(config.get("verifyTls"), True)
        follow_pagination = _coerce_bool(config.get("followPagination"), True)
        response_format = (config.get("responseFormat") or "auto").lower()

        request_kwargs = _build_request_kwargs(config, verify_tls, timeout_s)

        pages: list[Any] = []
        seen_urls: set[str] = set()
        current_url = url
        for page_idx in range(max_pages):
            if current_url in seen_urls:
                _LOGGER.warning(
                    "pagination loop detected (nextLink %s seen twice); stopping",
                    redact_url(current_url),
                )
                break
            seen_urls.add(current_url)

            payload = _request_one(
                current_url, session, request_kwargs, max_bytes, state
            )
            if payload is None:
                return None

            if _is_odata_error(payload):
                err = payload["error"]
                _LOGGER.warning(
                    "OData error envelope from %s: code=%r message=%r",
                    redact_url(current_url),
                    err.get("code"),
                    err.get("message"),
                )
                return None

            pages.append(payload)

            if not follow_pagination:
                break
            next_url = payload.get("@odata.nextLink") if isinstance(payload, dict) else None
            if not next_url or not isinstance(next_url, str):
                break
            # A relative nextLink is resolved against the page that returned it.
            next_url = urljoin(current_url, next_url)
            if _origin(next_url) != _origin(url):
                # Credentials and headers are sent with every request, so a
                # nextLink that points at another origin is never followed.
                _LOGGER.warning(
                    "ignoring @odata.nextLink to a different origin (%s); "
                    "pagination stopped. Configured origin is %s",
                    redact_url(next_url),
                    redact_url(url),
                )
                break
            current_url = next_url
        else:
            _LOGGER.warning(
                "maxPages=%d reached for %s; pagination truncated", max_pages, redact_url(url)
            )

        if not pages:
            return None
        if _looks_like_odata(pages[0], response_format):
            return _merge_odata_pages(pages)
        return pages[0]
    finally:
        if owns_session:
            session.close()


def _request_one(
    url: str,
    session: requests.Session,
    request_kwargs: dict,
    max_bytes: int,
    state: dict,
) -> Any:
    """Issue one HTTP GET; return parsed JSON, or ``None`` on any error."""
    shown = redact_url(url)
    try:
        with session.get(url, stream=True, **request_kwargs) as resp:
            if resp.status_code >= 400:
                _LOGGER.warning("HTTP %d from %s", resp.status_code, shown)
                return None

            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if chunk:
                    buf.extend(chunk)
                if len(buf) > max_bytes:
                    _LOGGER.warning(
                        "response from %s exceeded maxResponseBytes=%d; aborting",
                        shown,
                        max_bytes,
                    )
                    return None

            _reset_connrefused(state)
            text = buf.decode("utf-8", errors="replace")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                _LOGGER.warning(
                    "invalid JSON from %s: %s; body[:200]=%r", shown, exc, text[:200]
                )
                return None
    except requests.exceptions.ConnectionError as exc:
        _maybe_log_connrefused(state, shown, exc)
        return None
    except requests.exceptions.Timeout as exc:
        _LOGGER.warning("HTTP timeout to %s: %s", shown, _redact_text(exc))
        return None
    except requests.exceptions.RequestException as exc:
        _LOGGER.warning("HTTP error to %s: %s", shown, _redact_text(exc))
        return None


def _build_request_kwargs(config: dict, verify_tls: bool, timeout_s: float) -> dict:
    kwargs: dict[str, Any] = {"timeout": timeout_s, "verify": verify_tls}
    headers = _parse_extra_headers(config.get("extraHeaders"))

    auth_type = (config.get("authType") or "none").lower()
    if auth_type == "basic":
        kwargs["auth"] = HTTPBasicAuth(
            config.get("username", "") or "", config.get("password", "") or ""
        )
    elif auth_type == "bearer":
        token = config.get("bearerToken", "") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "apikey":
        header_name = (config.get("apiKeyHeader") or "X-API-Key").strip() or "X-API-Key"
        header_value = config.get("apiKeyValue", "") or ""
        if header_value:
            headers[header_name] = header_value
    elif auth_type != "none":
        _LOGGER.warning("unknown authType %r; proceeding without auth", auth_type)

    if headers:
        kwargs["headers"] = headers
    return kwargs


def _parse_extra_headers(raw: Any) -> dict[str, str]:
    if raw is None or raw == "" or raw == {}:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOGGER.warning("ignoring invalid extraHeaders JSON: %s", exc)
            return {}
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    _LOGGER.warning("ignoring extraHeaders of unexpected type: %r", type(raw).__name__)
    return {}


def _looks_like_odata(payload: Any, response_format: str) -> bool:
    if response_format == "odata_v4":
        return True
    if response_format != "auto":
        return False
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("value"), list):
        return False
    return any(isinstance(k, str) and k.startswith("@odata.") for k in payload)


def _is_odata_error(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    return isinstance(err, dict) and "code" in err and "message" in err


def _merge_odata_pages(pages: list[Any]) -> dict:
    """Combine paginated OData pages into a single response.

    Concatenates the ``value`` arrays across pages and drops the
    ``@odata.nextLink`` marker; other top-level metadata is taken from
    the first page.
    """
    merged: dict[str, Any] = dict(pages[0]) if isinstance(pages[0], dict) else {"value": []}
    merged.pop("@odata.nextLink", None)
    combined: list[Any] = []
    for page in pages:
        if isinstance(page, dict) and isinstance(page.get("value"), list):
            combined.extend(page["value"])
    merged["value"] = combined
    return merged


# --- Connection-refused exponential backoff log suppression -------------

_BACKOFF_GAPS = [1, 2, 4, 8, 16]


def _maybe_log_connrefused(state: dict, url: str, exc: Exception) -> None:
    """``url`` must already be redacted by the caller."""
    n = state.get("connrefused_count", 0) + 1
    state["connrefused_count"] = n
    next_log = state.get("connrefused_next_log", 1)
    gap_idx = state.get("connrefused_gap_idx", 0)
    if n >= next_log:
        _LOGGER.warning(
            "connection refused to %s (consecutive failure #%d): %s",
            url,
            n,
            _redact_text(exc),
        )
        gap = _BACKOFF_GAPS[min(gap_idx, len(_BACKOFF_GAPS) - 1)]
        state["connrefused_next_log"] = n + gap
        state["connrefused_gap_idx"] = gap_idx + 1


def _reset_connrefused(state: dict) -> None:
    for key in ("connrefused_count", "connrefused_next_log", "connrefused_gap_idx"):
        state.pop(key, None)


# --- Coercion helpers ---------------------------------------------------


def _coerce_int(raw: Any, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _coerce_bool(raw: Any, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off", ""):
            return False
    if raw is None:
        return default
    return bool(raw)
