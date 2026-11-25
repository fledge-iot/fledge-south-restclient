"""JMESPath-based extraction layer.

Applies a validated mapping document to a JSON payload, producing a list of
``(asset, datapoint, value, timestamp)`` tuples.

Supports:
  * Custom JMESPath functions ``from_json(@)``, ``to_array(@)``,
    ``flatten(@[, identity_keys[, separator]])`` (default separator ``_``),
    plus optional user ``def`` s from the
    mapping document's ``functions`` / ``functionsFile``.
  * ``{...}`` template substitution in asset and datapoint names (the
    contents are a JMESPath expression evaluated against the current
    context).
  * ``for_each`` iteration with ``$item`` and ``$parent`` context variables.
  * EtherNet/IP-style ``tags`` allowlist that maps flattened keys to
    ``(asset, datapoint)``.
  * ``from`` pass-through shorthand - expand an object into one datapoint per
    key, with ``rename`` / ``cast`` / ``exclude`` modifiers keyed on the
    original source key.
  * Per-datapoint ``cast`` to ``int`` / ``float`` / ``string`` / ``bool``.
  * Per-datapoint or rule-level ``timestamp`` expression with UTC fallback,
    normalized to Fledge's ``YYYY-MM-DD HH:MM:SS.ffffff+00:00``.

Null / failed-cast / missing-path datapoints are silently dropped per
partial mode; the caller may filter further for strict mode.
"""
from __future__ import annotations

import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import jmespath
from jmespath import functions

try:
    from . import user_functions
except ImportError:  # pragma: no cover - Fledge standalone script load
    import user_functions  # type: ignore[no-redef]


_LOGGER = logging.getLogger(__name__)


def _identity_key_names(id_keys: Any) -> list[str]:
    """Normalize the optional ``flatten`` identity-key list to strings."""
    if not id_keys:
        return []
    if isinstance(id_keys, str):
        return [id_keys] if id_keys else []
    names: list[str] = []
    for key in id_keys:
        if isinstance(key, str) and key:
            names.append(key)
    return names


def _path_segment(value: Any) -> str:
    """Render a leaf identity value as a single dotted-path segment."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


_DEFAULT_FLATTEN_SEP = "_"


def _path_join(sep: str, *parts: str) -> str:
    return sep.join(p for p in parts if p)


def _identity_segments(obj: dict, id_keys: list[str]) -> list[str]:
    """Path segments taken from ``obj`` for each configured identity key."""
    segs: list[str] = []
    for key in id_keys:
        if key not in obj:
            continue
        value = obj[key]
        if value is None or value == "" or isinstance(value, (dict, list)):
            continue
        segs.append(_path_segment(value))
    return segs


def _flatten_walk(
    value: Any,
    prefix: str,
    id_keys: list[str],
    out: dict[str, Any],
    sep: str,
) -> None:
    """Depth-first walk that records every JSON leaf under a joined path.

    Identity keys (``SiteId``, ``LineId``, ``EquipmentId``, ...) are
    promoted into the path and not emitted as leaves. Arrays of objects that
    carry those keys drop the container name (``Lines``, ``Equipment``)
    so a site tree flattens to ``SITE-A_Line1_Tank01_Readings_Level`` rather
    than ``Lines_0_Equipment_0_Readings_Level``. Objects and arrays
    without identity keys keep their key names and numeric indexes.
    """
    if value is None:
        return

    if isinstance(value, dict):
        obj_prefix = _path_join(sep, prefix, *_identity_segments(value, id_keys))
        id_key_set = set(id_keys)
        for key, child in value.items():
            if key in id_key_set:
                continue
            if isinstance(child, list):
                for index, item in enumerate(child):
                    if isinstance(item, dict) and _identity_segments(item, id_keys):
                        _flatten_walk(item, obj_prefix, id_keys, out, sep)
                    else:
                        _flatten_walk(
                            item,
                            _path_join(sep, obj_prefix, _path_segment(key), str(index)),
                            id_keys,
                            out,
                            sep,
                        )
            elif isinstance(child, dict) and _identity_segments(child, id_keys):
                _flatten_walk(child, obj_prefix, id_keys, out, sep)
            else:
                _flatten_walk(
                    child,
                    _path_join(sep, obj_prefix, _path_segment(key)),
                    id_keys,
                    out,
                    sep,
                )
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict) and _identity_segments(item, id_keys):
                _flatten_walk(item, prefix, id_keys, out, sep)
            else:
                _flatten_walk(item, _path_join(sep, prefix, str(index)), id_keys, out, sep)
        return

    if prefix:
        out[prefix] = value


class _RestClientFunctions(functions.Functions):
    @functions.signature({"types": ["string", "null"]})
    def _func_from_json(self, value):
        if value is None or value == "":
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    @functions.signature({"types": ["object", "null"]})
    def _func_to_array(self, obj):
        if obj is None:
            return []
        return [{"key": k, "value": v} for k, v in obj.items()]

    # Registered with a 1-arg signature so the function table knows the name.
    # ``call_function`` below accepts optional identity-keys and separator
    # arguments because jmespath.py cannot express optional arity via
    # ``@signature``.
    @functions.signature({"types": ["object", "array", "null"]})
    def _func_flatten(self, value, id_keys=None, separator=None):
        if value is None:
            return {}
        keys = _identity_key_names(id_keys)
        sep = _DEFAULT_FLATTEN_SEP if separator is None else separator
        out: dict[str, Any] = {}
        _flatten_walk(value, "", keys, out, sep)
        return out

    def call_function(self, function_name, resolved_args):
        if function_name == "flatten":
            return self._call_flatten(resolved_args)
        return super().call_function(function_name, resolved_args)

    def _call_flatten(self, args):
        if len(args) not in (1, 2, 3):
            raise jmespath.exceptions.JMESPathError(
                f"flatten() takes 1, 2, or 3 arguments, received {len(args)}"
            )
        self._type_check(args[:1], [{"types": ["object", "array", "null"]}], "flatten")
        id_keys = None
        separator = None
        if len(args) >= 2:
            self._type_check(args[1:2], [{"types": ["array", "string", "null"]}], "flatten")
            id_keys = args[1]
            keys = _identity_key_names(id_keys)
            if isinstance(id_keys, list) and id_keys and not keys:
                raise jmespath.exceptions.JMESPathError(
                    "flatten() identity keys resolved to null. In JMESPath, "
                    '["SiteId"] looks up a field on the current document '
                    "(usually the OData root, which has no SiteId). Use "
                    "['SiteId', 'LineId', 'EquipmentId'] or the JSON "
                    'literal `["SiteId", "LineId", "EquipmentId"]`.'
                )
        if len(args) == 3:
            self._type_check(args[2:3], [{"types": ["string"]}], "flatten")
            separator = args[2]
            if separator == "":
                raise jmespath.exceptions.JMESPathError(
                    "flatten() separator must be a non-empty string"
                )
        return self._func_flatten(args[0], id_keys, separator)


_JMESPATH_OPTIONS = jmespath.Options(custom_functions=_RestClientFunctions())
_ACTIVE_OPTIONS: ContextVar[jmespath.Options | None] = ContextVar(
    "restclient_jmespath_options", default=None
)
_TEMPLATE_RE = re.compile(r"\{([^{}]+)\}")
_DOLLAR_VAR_RE = re.compile(r"\$(item|parent)\b")
# ISO-8601 date-time with either separator, any number of fractional digits,
# and an optional Z or +hh:mm / +hhmm / +hh offset.
_ISO_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})"
    r"(?:[.,](\d+))?"
    r"(Z|[+-]\d{2}(?::?\d{2})?)?$",
    re.IGNORECASE,
)
_JSON_STYLE_STRING_ARRAY_RE = re.compile(
    r'^\s*\[\s*(?:"[^"]*"\s*(?:,\s*"[^"]*"\s*)*)\]\s*$'
)


Tuple4 = tuple  # (asset: str, datapoint: str, value: Any, timestamp: str)


def extract(payload: Any, mapping: dict) -> list[Tuple4]:
    """Apply ``mapping`` to ``payload`` and return a list of extraction tuples.

    Tuples are produced in mapping-rule, datapoint-declaration, and record
    order (stable, for deterministic emission grouping).
    """
    if payload is None:
        return []

    token = _ACTIVE_OPTIONS.set(options_for_mapping(mapping))
    try:
        return _extract_with_active_options(payload, mapping)
    finally:
        _ACTIVE_OPTIONS.reset(token)


def _extract_with_active_options(payload: Any, mapping: dict) -> list[Tuple4]:
    source = mapping.get("source", {}) or {}
    records_expr = source.get("records", "@")
    records = _eval(records_expr, payload)
    if records is None:
        return []
    if not isinstance(records, list):
        records = [records]

    default_tags = mapping.get("tags") or None
    tuples: list[Tuple4] = []
    for record in records:
        for rule in mapping.get("extractions", []):
            tuples.extend(_apply_rule(rule, record, default_tags=default_tags))
    return tuples


def _apply_rule(rule: dict, record: Any, default_tags: list[dict] | None = None) -> Iterable[Tuple4]:
    for_each_expr = rule.get("for_each")
    if not for_each_expr:
        yield from _apply_rule_body(
            rule, context=record, parent=None, default_tags=default_tags
        )
        return

    items = _eval(for_each_expr, record)
    if not isinstance(items, list):
        return
    for item in items:
        scope = {"_item": item, "_parent": record}
        yield from _apply_rule_body(
            rule,
            context=scope,
            parent=record,
            in_for_each=True,
            default_tags=default_tags,
        )


def _apply_rule_body(
    rule: dict,
    context: Any,
    parent: Any,
    in_for_each: bool = False,
    default_tags: list[dict] | None = None,
) -> Iterable[Tuple4]:
    asset = _render_template(rule["asset"], context)
    if asset is None:
        return
    rule_ts_expr = rule.get("timestamp")
    rule_ts = _resolve_timestamp(rule_ts_expr, context) if rule_ts_expr else None

    # `from` expansion runs first so that explicit datapoints declared on the
    # same rule win on a name collision.
    from_expr = rule.get("from")
    if from_expr:
        yield from _expand_from(
            rule, from_expr, context, asset, rule_ts, default_tags=default_tags
        )

    for dp_name, dp_spec in (rule.get("datapoints") or {}).items():
        rendered_name = _render_template(dp_name, context)
        if rendered_name is None:
            continue

        value_expr = dp_spec["value"]
        try:
            raw_value = _eval(value_expr, context)
        except Exception as exc:  # noqa: BLE001 - JMESPath can raise broadly
            _LOGGER.debug("dropping %s: JMESPath %r failed: %s", rendered_name, value_expr, exc)
            continue
        if raw_value is None:
            continue

        cast = dp_spec.get("cast")
        if cast:
            try:
                raw_value = _apply_cast(raw_value, cast)
            except (TypeError, ValueError) as exc:
                _LOGGER.debug(
                    "dropping %s: cast %r on %r failed: %s",
                    rendered_name,
                    cast,
                    raw_value,
                    exc,
                )
                continue

        dp_ts_expr = dp_spec.get("timestamp")
        ts = _resolve_timestamp(dp_ts_expr, context) if dp_ts_expr else rule_ts
        if ts is None:
            ts = _now_iso()

        yield (asset, rendered_name, raw_value, ts)


def _expand_from(
    rule: dict,
    from_expr: str,
    context: Any,
    asset: str,
    rule_ts: str | None,
    default_tags: list[dict] | None = None,
) -> Iterable[Tuple4]:
    """Expand a `from` object into one datapoint per key.

    Keys are discovered from the data, so a field absent from this record
    simply produces no datapoint - no error. `rename` / `cast` / `exclude`
    all key on the original source key, never on the renamed output name.

    A non-empty tag map (rule-level ``tags``, else document-level) is an
    allowlist: only listed names are emitted, each with its own asset and
    datapoint (EtherNet/IP-style).
    """
    obj = _eval(from_expr, context)
    if not isinstance(obj, dict):
        _LOGGER.debug(
            "`from` %r yielded %s, not an object - expanding nothing",
            from_expr,
            type(obj).__name__,
        )
        return

    rename = rule.get("rename") or {}
    casts = rule.get("cast") or {}
    exclude = set(rule.get("exclude") or ())
    default_cast = casts.get("*")
    ts = rule_ts if rule_ts is not None else _now_iso()
    tags = rule.get("tags") or default_tags

    if tags:
        yield from _expand_from_tags(obj, tags, asset, casts, default_cast, ts)
        return

    for key, value in obj.items():
        if key in exclude:
            continue
        if value is None:
            continue

        cast = casts.get(key, default_cast)
        if cast:
            try:
                value = _apply_cast(value, cast)
            except (TypeError, ValueError) as exc:
                _LOGGER.debug(
                    "dropping %s: cast %r on %r failed: %s", key, cast, value, exc
                )
                continue

        yield (asset, rename.get(key, key), value, ts)


def _expand_from_tags(
    obj: dict,
    tags: list[dict],
    default_asset: str,
    casts: dict,
    default_cast: str | None,
    ts: str,
) -> Iterable[Tuple4]:
    """Emit only mapped tags, in map order, with per-tag asset/datapoint."""
    for tag in tags:
        key = tag["name"]
        if key not in obj:
            _LOGGER.debug("tag %r missing from `from` object this poll - skipping", key)
            continue
        value = obj[key]
        if value is None:
            continue

        cast = tag.get("cast") or casts.get(key, default_cast)
        if cast:
            try:
                value = _apply_cast(value, cast)
            except (TypeError, ValueError) as exc:
                _LOGGER.debug(
                    "dropping %s: cast %r on %r failed: %s", key, cast, value, exc
                )
                continue

        yield (
            tag.get("asset") or default_asset,
            tag.get("datapoint") or key,
            value,
            ts,
        )


def _apply_cast(value: Any, cast: str) -> Any:
    if cast == "int":
        return int(value)
    if cast == "float":
        return float(value)
    if cast == "string":
        return str(value)
    if cast == "bool":
        return bool(value)
    raise ValueError(f"unknown cast: {cast!r}")


def _render_template(template: str, context: Any) -> str | None:
    """Substitute ``{JMESPath}`` segments in ``template`` against ``context``.

    Returns ``None`` if any segment resolves to ``None`` (caller drops).
    """

    def repl(match: re.Match) -> str:
        expr = match.group(1).strip()
        value = _eval(expr, context)
        if value is None:
            raise _TemplateNullSegment()
        return str(value)

    try:
        return _TEMPLATE_RE.sub(repl, template)
    except _TemplateNullSegment:
        return None


class _TemplateNullSegment(Exception):
    pass


def evaluate(expr: str, context: Any, options: jmespath.Options | None = None) -> Any:
    """Evaluate a plugin JMESPath expression against ``context``.

    Applies the same ``$item`` / ``$parent`` rewrite and custom functions
    (``from_json``, ``to_array``, ``flatten``, plus any mapping ``functions``)
    as the extraction engine. Unlike ``_eval``, JMESPath syntax and type
    errors are raised so interactive callers can surface them instead of
    treating a bad query as a missing path.
    """
    rewritten = _DOLLAR_VAR_RE.sub(lambda m: f"_{m.group(1)}", expr)
    rewritten = _rewrite_flatten_json_key_lists(rewritten)
    active = options if options is not None else (_ACTIVE_OPTIONS.get() or _JMESPATH_OPTIONS)
    return jmespath.search(rewritten, context, options=active)


def _rewrite_flatten_json_key_lists(expr: str) -> str:
    """Rewrite ``flatten(x, ["A", "B"])`` to ``flatten(x, ['A', 'B'])``.

    In JMESPath, ``["SiteId"]`` is a multi-select of the *field*
    ``SiteId`` against the current context. On an OData envelope that
    yields ``[null]``, so ``flatten`` silently does a structural walk and
    leaves ``SiteId`` as a leaf. Users mean a list of identity *names*,
    written ``['SiteId']`` or the JSON literal `` `["SiteId"]` ``.
    """
    pieces: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        idx = expr.find("flatten", i)
        if idx < 0:
            pieces.append(expr[i:])
            break
        pieces.append(expr[i:idx])
        j = idx + len("flatten")
        while j < n and expr[j].isspace():
            j += 1
        if j >= n or expr[j] != "(":
            pieces.append(expr[idx:j])
            i = j
            continue
        close, commas = _match_paren_call(expr, j)
        if close < 0 or not commas:
            end = close + 1 if close >= 0 else n
            pieces.append(expr[idx:end])
            i = end
            continue
        starts = [j + 1] + [c + 1 for c in commas]
        ends = commas + [close]
        arg_texts = [expr[s:e] for s, e in zip(starts, ends)]
        if len(arg_texts) >= 2:
            arg_texts[1] = _json_style_string_array_to_literals(arg_texts[1])
        pieces.append("flatten(")
        pieces.append(",".join(arg_texts))
        pieces.append(")")
        i = close + 1
    return "".join(pieces)


def _match_paren_call(expr: str, open_paren: int) -> tuple[int, list[int]]:
    """Return ``(close_index, depth-1 comma indexes)`` for a ``(`` at ``open_paren``."""
    depth = 0
    square = 0
    curly = 0
    commas: list[int] = []
    in_single = in_double = in_backtick = False
    k = open_paren
    while k < len(expr):
        c = expr[k]
        prev = expr[k - 1] if k else ""
        if in_single:
            if c == "'" and prev != "\\":
                in_single = False
        elif in_double:
            if c == '"' and prev != "\\":
                in_double = False
        elif in_backtick:
            if c == "`":
                in_backtick = False
        elif c == "'":
            in_single = True
        elif c == '"':
            in_double = True
        elif c == "`":
            in_backtick = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return k, commas
        elif c == "[":
            square += 1
        elif c == "]":
            square -= 1
        elif c == "{":
            curly += 1
        elif c == "}":
            curly -= 1
        elif c == "," and depth == 1 and square == 0 and curly == 0:
            commas.append(k)
        k += 1
    return -1, commas


def _json_style_string_array_to_literals(second: str) -> str:
    if not _JSON_STYLE_STRING_ARRAY_RE.match(second):
        return second
    lead_len = len(second) - len(second.lstrip())
    trail_len = len(second) - len(second.rstrip())
    lead = second[:lead_len]
    trail = second[len(second) - trail_len :] if trail_len else ""
    converted = re.sub(r'"([^"]*)"', r"'\1'", second.strip())
    return f"{lead}{converted}{trail}"


def options_for_mapping(mapping: dict | None, functions_root: Path | None = None) -> jmespath.Options:
    """Compile ``mapping['functions']`` / ``functionsFile`` onto the plugin engine."""
    if not mapping:
        return _JMESPATH_OPTIONS
    source = user_functions.combined_source(mapping, functions_root=functions_root)
    return user_functions.options_for_source(source, _RestClientFunctions, _JMESPATH_OPTIONS)


def _eval(expr: str, context: Any) -> Any:
    try:
        return evaluate(expr, context)
    except jmespath.exceptions.JMESPathError as exc:
        _LOGGER.debug("JMESPath %r against %s failed: %s", expr, type(context).__name__, exc)
        return None


def _resolve_timestamp(expr: str, context: Any) -> str | None:
    raw = _eval(expr, context)
    if raw is None or raw == "":
        return None
    return _format_timestamp(raw)


def _format_timestamp(raw: Any) -> str | None:
    """Normalize a timestamp value to the form Fledge's C++ ingest parses.

    Fledge's ``Reading::stringToTimestamp`` reads the date and time with
    ``strptime("%Y-%m-%d %H:%M:%S")``, so an ISO ``T`` separator makes it drop
    the time of day and file every reading at midnight UTC. It also takes the
    fraction as a microsecond count without trimming it, so 7 digits overflow.
    Every timestamp therefore leaves the plugin as UTC with a space separator
    and exactly 6 fractional digits: ``2026-11-24 15:40:08.209751+00:00``.

    ``Z`` and numeric offsets are converted to UTC here, and naive strings are
    assumed UTC. An unparseable value is logged and returns None, so the caller
    falls back to the poll time.
    """
    s = (raw if isinstance(raw, str) else str(raw)).strip()
    m = _ISO_TS_RE.match(s)
    if not m:
        _LOGGER.warning("unparseable timestamp %r - using poll time", s)
        return None
    year, month, day, hour, minute, second, frac, tz = m.groups()
    micro = int((frac or "")[:6].ljust(6, "0"))
    if tz is None:
        _LOGGER.warning("naive timestamp %r - assuming UTC", s)
        offset = timezone.utc
    elif tz.upper() == "Z":
        offset = timezone.utc
    else:
        digits = tz[1:].replace(":", "")
        minutes = int(digits[:2]) * 60 + int(digits[2:4] or 0)
        offset = timezone(timedelta(minutes=minutes if tz[0] == "+" else -minutes))
    try:
        dt = datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second),
            micro, tzinfo=offset,
        )
    except ValueError:
        _LOGGER.warning("invalid timestamp %r - using poll time", s)
        return None
    return _to_fledge(dt)


def _to_fledge(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


def _now_iso() -> str:
    return _to_fledge(datetime.now(tz=timezone.utc))
