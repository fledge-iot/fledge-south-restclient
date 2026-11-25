"""Mapping document loader + validator.

Parses a mapping document (YAML or JSON) and validates it against the
grammar. Raises ``MappingError`` with a clear message on any structural problem. The plugin treats these as hard
configuration errors at ``plugin_init`` time.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

try:
    from . import omf_hints
    from .tag_map import parse_tags
    from .user_functions import UserFunctionError, check_source, combined_source
except ImportError:  # pragma: no cover - Fledge standalone script load
    import omf_hints  # type: ignore[no-redef]
    from tag_map import parse_tags
    from user_functions import UserFunctionError, check_source, combined_source


_VALID_CASTS = {"int", "float", "string", "bool"}


class MappingError(ValueError):
    """Raised when a mapping document fails to parse or validate."""


def load(source: str = "inline", inline: Any = None, path: str | None = None) -> dict:
    """Load and validate a mapping document.

    ``source`` is ``"inline"`` (use ``inline`` arg - a string or dict) or
    ``"file"`` (use ``path``). Returns the validated mapping dict.
    """
    if source == "file":
        if not path:
            raise MappingError("mappingSource=file requires a non-empty mappingFile path")
        return load_from_file(path)
    if source == "inline":
        if inline is None or inline == "" or inline == {}:
            raise MappingError("mappingSource=inline requires a non-empty mappingInline value")
        if isinstance(inline, dict):
            return validate(inline)
        if isinstance(inline, str):
            return load_from_text(inline)
        raise MappingError(f"unsupported inline mapping type: {type(inline).__name__}")
    raise MappingError(f"unknown mappingSource: {source!r} (expected 'inline' or 'file')")


def load_from_file(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise MappingError(f"mapping file not found: {p}")
    text = p.read_text(encoding="utf-8")
    try:
        return load_from_text(text, functions_root=p.parent)
    except MappingError as e:
        raise MappingError(f"{p}: {e}") from e


def load_from_text(text: str, functions_root: Path | None = None) -> dict:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise MappingError(f"mapping document is not valid YAML/JSON: {e}") from e
    if parsed is None:
        raise MappingError("mapping document is empty")
    if not isinstance(parsed, dict):
        raise MappingError(
            f"mapping document must be an object at the top level, got {type(parsed).__name__}"
        )
    return validate(parsed, functions_root=functions_root)


def validate(mapping: dict, functions_root: Path | None = None) -> dict:
    """Structural validation."""
    if not isinstance(mapping, dict):
        raise MappingError(f"mapping must be a dict, got {type(mapping).__name__}")

    _validate_functions(mapping, functions_root)
    _validate_top_level_tags(mapping)
    if "omfHints" in mapping:
        mapping["omfHints"] = omf_hints.validate_mapping_hints(
            mapping["omfHints"], MappingError
        )

    if "source" in mapping:
        source = mapping["source"]
        if not isinstance(source, dict):
            raise MappingError(f"`source` must be a dict, got {type(source).__name__}")
        if "records" in source and not isinstance(source["records"], str):
            raise MappingError("`source.records` must be a string (JMESPath expression)")

    extractions = mapping.get("extractions")
    if not isinstance(extractions, list) or not extractions:
        raise MappingError("`extractions` must be a non-empty list")

    for i, rule in enumerate(extractions):
        _validate_rule(rule, i)

    return mapping


def _validate_rule(rule: Any, index: int) -> None:
    where = f"extractions[{index}]"
    if not isinstance(rule, dict):
        raise MappingError(f"{where} must be a dict, got {type(rule).__name__}")

    asset = rule.get("asset")
    if not isinstance(asset, str) or not asset:
        raise MappingError(f"{where}.asset must be a non-empty string")

    if "for_each" in rule and not isinstance(rule["for_each"], str):
        raise MappingError(f"{where}.for_each must be a JMESPath string")

    has_from = "from" in rule
    if has_from and (not isinstance(rule["from"], str) or not rule["from"]):
        raise MappingError(f"{where}.from must be a non-empty JMESPath string")

    has_datapoints = "datapoints" in rule
    datapoints = rule.get("datapoints")
    if has_datapoints and (not isinstance(datapoints, dict) or not datapoints):
        raise MappingError(f"{where}.datapoints must be a non-empty mapping")
    if not has_from and not has_datapoints:
        raise MappingError(f"{where} must contain at least one of `from` or `datapoints`")

    rule_has_timestamp = isinstance(rule.get("timestamp"), str) and rule["timestamp"]
    if "timestamp" in rule and not rule_has_timestamp:
        raise MappingError(f"{where}.timestamp must be a non-empty string when present")

    _validate_from_modifiers(rule, where, has_from)
    _validate_rule_tags(rule, where, has_from)

    # A `from` rule has no per-key stanza to carry a timestamp override, so the
    # rule-level expression is the only place one can be declared.
    if has_from and not rule_has_timestamp:
        raise MappingError(
            f"{where} uses `from` and therefore requires a non-empty rule-level `timestamp`"
        )

    for dp_name, dp_spec in (datapoints or {}).items():
        if not isinstance(dp_name, str) or not dp_name:
            raise MappingError(f"{where}.datapoints has non-string or empty key")
        if not isinstance(dp_spec, dict):
            raise MappingError(f"{where}.datapoints[{dp_name!r}] must be a dict")
        value_expr = dp_spec.get("value")
        if not isinstance(value_expr, str) or not value_expr:
            raise MappingError(
                f"{where}.datapoints[{dp_name!r}].value must be a non-empty JMESPath string"
            )
        cast = dp_spec.get("cast")
        if cast is not None and cast not in _VALID_CASTS:
            raise MappingError(
                f"{where}.datapoints[{dp_name!r}].cast must be one of "
                f"{sorted(_VALID_CASTS)}, got {cast!r}"
            )
        dp_has_timestamp = isinstance(dp_spec.get("timestamp"), str) and dp_spec["timestamp"]
        if "timestamp" in dp_spec and not dp_has_timestamp:
            raise MappingError(
                f"{where}.datapoints[{dp_name!r}].timestamp must be a non-empty string"
            )
        if not rule_has_timestamp and not dp_has_timestamp:
            raise MappingError(
                f"{where}.datapoints[{dp_name!r}] has no timestamp and the rule has no "
                f"rule-level `timestamp`; one is required"
            )


def _validate_top_level_tags(mapping: dict) -> None:
    if "tags" not in mapping:
        return
    mapping["tags"] = parse_tags(mapping["tags"], where="tags")


def _validate_rule_tags(rule: dict, where: str, has_from: bool) -> None:
    if "tags" not in rule:
        return
    if not has_from:
        raise MappingError(
            f"{where}.tags is only meaningful alongside `from`; the rule has no `from`"
        )
    for key in ("rename", "exclude"):
        if key in rule:
            raise MappingError(
                f"{where}.{key} cannot be combined with `tags`; "
                f"the tag map is the allowlist and rename"
            )
    rule["tags"] = parse_tags(rule["tags"], where=f"{where}.tags")


def _validate_from_modifiers(rule: dict, where: str, has_from: bool) -> None:
    """Validate `rename` / `cast` / `exclude`.

    All three key on the ORIGINAL source key and are meaningless without a
    `from` on the same rule - their presence alone is treated as an error
    rather than silently ignored.
    """
    for key in ("rename", "cast", "exclude"):
        if key in rule and not has_from:
            raise MappingError(
                f"{where}.{key} is only meaningful alongside `from`; "
                f"the rule has no `from`"
            )

    if "rename" in rule:
        rename = rule["rename"]
        if not isinstance(rename, dict):
            raise MappingError(
                f"{where}.rename must be a mapping of source key -> datapoint name, "
                f"got {type(rename).__name__}"
            )
        for src, target in rename.items():
            if not isinstance(src, str) or not src:
                raise MappingError(f"{where}.rename has a non-string or empty key")
            if not isinstance(target, str) or not target:
                raise MappingError(f"{where}.rename[{src!r}] must be a non-empty string")

    if "cast" in rule:
        casts = rule["cast"]
        if not isinstance(casts, dict):
            raise MappingError(
                f"{where}.cast must be a mapping of source key -> cast type, "
                f"got {type(casts).__name__}"
            )
        for src, cast in casts.items():
            if not isinstance(src, str) or not src:
                raise MappingError(f"{where}.cast has a non-string or empty key")
            if cast not in _VALID_CASTS:
                raise MappingError(
                    f"{where}.cast[{src!r}] must be one of {sorted(_VALID_CASTS)}, got {cast!r}"
                )

    if "exclude" in rule:
        exclude = rule["exclude"]
        if not isinstance(exclude, list):
            raise MappingError(
                f"{where}.exclude must be a list of source keys, got {type(exclude).__name__}"
            )
        for src in exclude:
            if not isinstance(src, str) or not src:
                raise MappingError(f"{where}.exclude entries must be non-empty strings")


def _validate_functions(mapping: dict, functions_root: Path | None) -> None:
    """Compile mapping ``functions`` / ``functionsFile`` so init fails fast."""
    if "functions" in mapping and mapping["functions"] is not None:
        if not isinstance(mapping["functions"], str):
            raise MappingError("`functions` must be a Python source string")
    if "functionsFile" in mapping and mapping["functionsFile"] is not None:
        if not isinstance(mapping["functionsFile"], str) or not mapping["functionsFile"].strip():
            raise MappingError("`functionsFile` must be a non-empty path string")
    if "functions" not in mapping and "functionsFile" not in mapping:
        return
    try:
        source = combined_source(mapping, functions_root=functions_root)
        if source.strip():
            check_source(source)
    except UserFunctionError as exc:
        raise MappingError(str(exc)) from exc

    file_field = mapping.get("functionsFile")
    if isinstance(file_field, str) and file_field.strip():
        path = Path(file_field.strip())
        if not path.is_absolute() and functions_root is not None:
            path = functions_root / path
        mapping["functionsFile"] = str(path.resolve())
