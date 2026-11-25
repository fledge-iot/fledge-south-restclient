"""Read tag map.

A tag map is an allowlist that routes flattened source keys (or any
``from`` object keys) to a Fledge ``(asset, datapoint)``. The JSON shape
matches the fledge-south-ethernetip read map, so the same tooling can
produce maps for both plugins:

.. code-block:: json

    {
      "tags": [
        {
          "name": "SITE-A_Line1_Tank01_Readings_Level",
          "asset": "tank-01",
          "datapoint": "level"
        }
      ]
    }

``type`` and ``program`` are accepted and ignored (PLC-only fields).
``cast`` is optional and uses this plugin's casts (``int`` / ``float`` /
``string`` / ``bool``).

An empty map means "no filter": every key from ``from`` is emitted.
A non-empty map emits only the listed names, in map order. A listed
name that is missing from this poll is skipped.
"""
from __future__ import annotations

import json

try:
    from . import omf_hints
except ImportError:  # pragma: no cover - Fledge standalone script load
    import omf_hints  # type: ignore[no-redef]
from typing import Any

_VALID_CASTS = {"int", "float", "string", "bool"}


def _mapping_error(message: str) -> Exception:
    try:
        from .mapping_loader import MappingError
    except ImportError:  # pragma: no cover - Fledge standalone script load
        from mapping_loader import MappingError
    return MappingError(message)


def parse_tags(raw: Any, *, where: str = "tags") -> list[dict]:
    """Normalize a tag map to a list of ``{name, asset?, datapoint?, cast?}``.

    Accepts ``None``, ``""``, ``[]``, a bare list of tag objects, a
    ``{"tags": [...]}`` wrapper, or a JSON string of any of those.
    """
    raw = _unwrap_raw(raw, where=where)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _mapping_error(
            f"{where} must be a list or {{tags: [...]}}, got {type(raw).__name__}"
        )

    seen: set[str] = set()
    out: list[dict] = []
    for i, entry in enumerate(raw):
        tag = _parse_one(entry, where=f"{where}[{i}]")
        name = tag["name"]
        if name in seen:
            raise _mapping_error(f"{where}: duplicate tag name {name!r}")
        seen.add(name)
        out.append(tag)
    return out


def mapping_defines_tags(mapping: dict) -> bool:
    """True when the mapping document already has a non-empty tag map."""
    if mapping.get("tags"):
        return True
    for rule in mapping.get("extractions") or []:
        if isinstance(rule, dict) and rule.get("tags"):
            return True
    return False


def attach_config_tags(mapping: dict, raw: Any) -> dict:
    """Copy plugin-config ``tagsToRead`` onto the mapping when it has none."""
    tags = parse_tags(raw, where="tagsToRead")
    if tags and not mapping_defines_tags(mapping):
        mapping["tags"] = tags
    return mapping


def _unwrap_raw(raw: Any, *, where: str) -> Any:
    if raw is None or raw == "" or raw == {}:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text in ("{}", "[]"):
            return None
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _mapping_error(f"{where} is not valid JSON: {exc}") from exc
    if isinstance(raw, dict):
        if raw == {} or raw == {"tags": []}:
            return None
        if "tags" not in raw:
            raise _mapping_error(f"{where} object must contain a `tags` array")
        return raw["tags"]
    return raw


def _parse_one(entry: Any, *, where: str) -> dict:
    if not isinstance(entry, dict):
        raise _mapping_error(f"{where} must be an object, got {type(entry).__name__}")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise _mapping_error(f"{where}.name must be a non-empty string (the source tag)")

    tag: dict[str, Any] = {"name": name.strip()}
    for field in ("asset", "datapoint"):
        value = entry.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip():
            raise _mapping_error(f"{where}.{field} must be a non-empty string when present")
        tag[field] = value.strip()

    cast = entry.get("cast")
    if cast is not None and cast != "":
        if cast not in _VALID_CASTS:
            raise _mapping_error(
                f"{where}.cast must be one of {sorted(_VALID_CASTS)}, got {cast!r}"
            )
        tag["cast"] = cast

    if entry.get("omfHint") is not None:
        tag["omfHint"] = omf_hints.validate_hint(
            entry["omfHint"], f"{where}.omfHint", _mapping_error
        )
    return tag
