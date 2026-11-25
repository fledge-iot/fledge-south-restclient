"""Heuristic JMESPath completions using the plugin evaluation engine.

Not a language server. Completes:

* ``$item`` / ``$parent``
* plugin + standard function names
* object keys (and array ``[0]`` / ``[]``) by evaluating the expression
  prefix with :func:`extraction.evaluate`, so ``from_json(Data).`` can
  offer keys of the parsed inner document.
"""
from __future__ import annotations

import re
from typing import Any

import jmespath

from extraction import evaluate


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TRAILING_DOT_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)?$")
_TRAILING_DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?$")
_TRAILING_IDENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)$")

# Plugin functions first so they sort to the top of a matching prefix list
# when the caller does not re-sort. Details are shown in the editor gutter.
FUNCTIONS: list[tuple[str, str]] = [
    ("from_json", "plugin - parse a JSON string field"),
    ("to_array", "plugin - object -> [{key, value}, …]"),
    ("flatten", "plugin - nested JSON -> joined leaf paths (default sep _)"),
    ("abs", "absolute value"),
    ("avg", "average of an array of numbers"),
    ("ceil", "round up"),
    ("contains", "haystack contains needle"),
    ("ends_with", "string suffix test"),
    ("floor", "round down"),
    ("join", "join array of strings"),
    ("keys", "object keys"),
    ("length", "length of string, array, or object"),
    ("map", "apply expression to each element"),
    ("max", "largest number or string"),
    ("max_by", "max element by expression"),
    ("merge", "merge objects"),
    ("min", "smallest number or string"),
    ("min_by", "min element by expression"),
    ("not_null", "first non-null argument"),
    ("reverse", "reverse a string or array"),
    ("sort", "sort an array"),
    ("sort_by", "sort by expression"),
    ("starts_with", "string prefix test"),
    ("sum", "sum of numbers"),
    ("to_number", "convert to number"),
    ("to_string", "convert to string"),
    ("type", "JMESPath type name"),
    ("values", "object values"),
]

_DOLLAR_VARS = (
    ("$item", "current for_each element"),
    ("$parent", "enclosing record in for_each"),
)


def complete(
    query: str,
    cursor: int,
    context: Any,
    options: Any = None,
    extra_functions: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Return ``{suggestions, from}`` for the text before ``cursor``."""
    cursor = max(0, min(cursor, len(query)))
    text = query[:cursor]
    suggestions: list[dict[str, str]] = []
    replace_from = cursor
    function_table = list(FUNCTIONS)
    if extra_functions:
        function_table = list(extra_functions) + function_table

    dollar = _TRAILING_DOLLAR_RE.search(text)
    if dollar:
        partial = dollar.group(1) or ""
        replace_from = dollar.start()
        for name, detail in _DOLLAR_VARS:
            if name[1:].startswith(partial):
                suggestions.append({"label": name, "type": "variable", "detail": detail})
        return {"suggestions": suggestions, "from": replace_from}

    dotted = _TRAILING_DOT_RE.search(text)
    if dotted:
        prefix = text[: dotted.start()]
        partial = dotted.group(1) or ""
        # Replace from the dot so identifier keys become `.M01` and
        # non-identifiers become `["@odata.context"]` rather than `.["…"]`.
        replace_from = dotted.start()
        value = _try_eval(prefix, context, options)
        suggestions.extend(_key_suggestions(value, partial, dotted=True))
        return {"suggestions": suggestions, "from": replace_from}

    ident = _TRAILING_IDENT_RE.search(text)
    if ident and _in_function_position(text[: ident.start()]):
        partial = ident.group(1)
        replace_from = ident.start()
        suggestions.extend(_function_suggestions(partial, function_table))
        suggestions.extend(_root_key_suggestions(context, partial, dotted=False))
        value = _try_eval(ident.group(1), context, options)
        if value is not _MISSING and value is not None:
            suggestions.extend(_suffix_suggestions(value))
        return {"suggestions": _dedupe(suggestions), "from": replace_from}

    if _in_function_position(text):
        suggestions.extend(_function_suggestions("", function_table))
        suggestions.extend(_root_key_suggestions(context, "", dotted=False))
        return {"suggestions": _dedupe(suggestions), "from": cursor}

    if text.strip():
        value = _try_eval(text.strip(), context, options)
        if value is not _MISSING:
            replace_from = cursor
            suggestions.extend(_suffix_suggestions(value))
            return {"suggestions": suggestions, "from": replace_from}

    return {"suggestions": suggestions, "from": replace_from}


_MISSING = object()


def _try_eval(expr: str, context: Any, options: Any = None) -> Any:
    if not expr.strip():
        return context
    try:
        return evaluate(expr, context, options=options)
    except (jmespath.exceptions.JMESPathError, TypeError, ValueError):
        return _MISSING


def _in_function_position(before: str) -> bool:
    stripped = before.rstrip()
    if stripped == "":
        return True
    return stripped[-1] in "|(,"


def _function_suggestions(
    partial: str, table: list[tuple[str, str]] | None = None
) -> list[dict[str, str]]:
    out = []
    for name, detail in table or FUNCTIONS:
        if name.startswith(partial):
            out.append({"label": name, "type": "function", "detail": detail})
    return out


def _root_key_suggestions(context: Any, partial: str, *, dotted: bool) -> list[dict[str, str]]:
    if isinstance(context, dict) and set(context.keys()) == {"_item", "_parent"}:
        out = []
        for name, detail in _DOLLAR_VARS:
            if name.startswith(partial) or name[1:].startswith(partial):
                out.append({"label": name, "type": "variable", "detail": detail})
        return out
    return _key_suggestions(context, partial, dotted=dotted)


def _key_suggestions(value: Any, partial: str, *, dotted: bool) -> list[dict[str, str]]:
    if value is _MISSING or value is None:
        return []
    if isinstance(value, dict):
        out = []
        for key, child in value.items():
            if not str(key).startswith(partial):
                continue
            out.append(
                {
                    "label": _format_key(str(key), dotted=dotted),
                    "type": "property",
                    "detail": _type_name(child),
                }
            )
        return out
    if isinstance(value, list):
        if dotted:
            # `array.foo` is a projection - suggest keys of the first object.
            if value and isinstance(value[0], dict):
                return _key_suggestions(value[0], partial, dotted=False)
            return []
        return _array_suffixes()
    return []


def _suffix_suggestions(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        return _key_suggestions(value, "", dotted=True)
    if isinstance(value, list):
        return _array_suffixes()
    return []


def _array_suffixes() -> list[dict[str, str]]:
    return [
        {"label": "[0]", "type": "keyword", "detail": "first element"},
        {"label": "[]", "type": "keyword", "detail": "project each element"},
    ]


def _format_key(key: str, *, dotted: bool) -> str:
    if _IDENT_RE.match(key):
        return f".{key}" if dotted else key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'["{escaped}"]'


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _dedupe(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out = []
    for item in items:
        label = item["label"]
        if label in seen:
            continue
        seen.add(label)
        out.append(item)
    return out
