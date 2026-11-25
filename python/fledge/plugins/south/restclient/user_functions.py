"""Compile user-supplied Python into extra JMESPath functions.

Mapping documents may include a ``functions`` source string and/or a
``functionsFile`` path. Top-level ``def`` names become JMESPath functions
on the same engine as ``from_json`` / ``to_array`` / ``flatten``.

The exec environment is restricted - not a security boundary. It pre-injects
``json``, ``re``, ``math``, and ``datetime``, and only those modules may be
imported. File I/O, ``eval`` / ``exec``, and arbitrary imports are unavailable.
"""
from __future__ import annotations

import ast
import datetime
import inspect
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

import jmespath
from jmespath import functions as jmespath_functions


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Plugin functions plus the JMESPath built-in set. Users may not replace these.
RESERVED_FUNCTION_NAMES = frozenset(
    {
        "from_json",
        "to_array",
        "flatten",
        "abs",
        "avg",
        "ceil",
        "contains",
        "ends_with",
        "floor",
        "join",
        "keys",
        "length",
        "map",
        "max",
        "max_by",
        "merge",
        "min",
        "min_by",
        "not_null",
        "reverse",
        "sort",
        "sort_by",
        "starts_with",
        "sum",
        "to_number",
        "to_string",
        "type",
        "values",
    }
)

_ALLOWED_MODULES = {
    "json": json,
    "re": re,
    "math": math,
    "datetime": datetime,
}

_SAFE_BUILTINS: dict[str, Any] = {
    "True": True,
    "False": False,
    "None": None,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}

_ANY_TYPE = {"types": ["number", "string", "boolean", "array", "object", "null"]}

_OPTIONS_CACHE: dict[tuple[str, int], jmespath.Options] = {}
_OPTIONS_CACHE_MAX = 32


class UserFunctionError(ValueError):
    """Raised when user function source cannot be compiled or registered."""


def combined_source(mapping: dict, functions_root: Path | None = None) -> str:
    """Return the concatenated Python source from ``functionsFile`` + ``functions``.

    ``functionsFile`` is resolved against ``functions_root`` when relative.
    Missing files raise ``UserFunctionError``.
    """
    parts: list[str] = []
    file_field = mapping.get("functionsFile")
    if file_field is not None:
        if not isinstance(file_field, str) or not file_field.strip():
            raise UserFunctionError("`functionsFile` must be a non-empty path string")
        path = Path(file_field.strip())
        if not path.is_absolute() and functions_root is not None:
            path = functions_root / path
        if not path.is_file():
            raise UserFunctionError(f"functionsFile not found: {path}")
        parts.append(path.read_text(encoding="utf-8"))
    inline = mapping.get("functions")
    if inline is not None:
        if not isinstance(inline, str):
            raise UserFunctionError("`functions` must be a Python source string")
        if inline.strip():
            parts.append(inline)
    return "\n\n".join(parts)


def check_source(source: str) -> None:
    """Parse and exec ``source`` so configuration errors fail at mapping load."""
    if source.strip():
        load_callables(source)


def options_for_source(
    source: str,
    base_cls: type,
    default_options: jmespath.Options,
) -> jmespath.Options:
    """Return a ``jmespath.Options`` with user functions registered on ``base_cls``."""
    if not source.strip():
        return default_options
    key = (source, id(base_cls))
    cached = _OPTIONS_CACHE.get(key)
    if cached is not None:
        return cached
    cls = compile_function_class(source, base_cls)
    options = jmespath.Options(custom_functions=cls())
    if len(_OPTIONS_CACHE) >= _OPTIONS_CACHE_MAX:
        _OPTIONS_CACHE.pop(next(iter(_OPTIONS_CACHE)))
    _OPTIONS_CACHE[key] = options
    return options


def compile_function_class(source: str, base_cls: type) -> type:
    """Build a ``Functions`` subclass with one ``_func_*`` method per user def."""
    callables = load_callables(source)
    attrs: dict[str, Any] = {}
    for name, fn in callables.items():
        attrs[f"_func_{name}"] = _wrap(name, fn)
    return type("UserRestClientFunctions", (base_cls,), attrs)


def load_callables(source: str) -> dict[str, Callable[..., Any]]:
    """Exec ``source`` and return the top-level functions that should be exported."""
    names = _top_level_function_names(source)
    namespace = _exec_namespace(source)
    exported: dict[str, Callable[..., Any]] = {}
    for name in names:
        if name.startswith("_"):
            continue
        if name in RESERVED_FUNCTION_NAMES:
            raise UserFunctionError(
                f"function {name}() collides with a built-in or plugin JMESPath "
                f"function; choose another name"
            )
        if not _IDENT_RE.match(name):
            raise UserFunctionError(f"function name {name!r} is not a valid JMESPath identifier")
        fn = namespace.get(name)
        if not callable(fn):
            raise UserFunctionError(f"{name}() was declared but is not callable after exec")
        _assert_plain_signature(name, fn)
        exported[name] = fn
    if not exported:
        raise UserFunctionError(
            "no public functions found; define at least one top-level def that "
            "does not start with _"
        )
    return exported


def _top_level_function_names(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise UserFunctionError(f"syntax error: {exc}") from exc
    return [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]


def _exec_namespace(source: str) -> dict[str, Any]:
    namespace: dict[str, Any] = {
        "__builtins__": {**_SAFE_BUILTINS, "__import__": _safe_import},
        "json": json,
        "re": re,
        "math": math,
        "datetime": datetime,
    }
    try:
        exec(compile(source, "<mapping functions>", "exec"), namespace, namespace)  # noqa: S102
    except UserFunctionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any import/runtime error at load
        raise UserFunctionError(f"failed to execute functions: {type(exc).__name__}: {exc}") from exc
    return namespace


def _safe_import(name: str, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
    if level:
        raise ImportError("relative imports are not allowed in mapping functions")
    root = name.split(".", 1)[0]
    if root not in _ALLOWED_MODULES:
        allowed = ", ".join(sorted(_ALLOWED_MODULES))
        raise ImportError(
            f"import {name!r} is not allowed in mapping functions (allowed: {allowed})"
        )
    module = _ALLOWED_MODULES[root]
    if name == root:
        return module
    obj: Any = module
    for part in name.split(".")[1:]:
        obj = getattr(obj, part)
    return obj


def _assert_plain_signature(name: str, fn: Callable[..., Any]) -> None:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise UserFunctionError(f"{name}() has an uninspectable signature: {exc}") from exc
    for param in sig.parameters.values():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise UserFunctionError(f"{name}() cannot use *args or **kwargs")
        if param.kind is inspect.Parameter.KEYWORD_ONLY:
            raise UserFunctionError(f"{name}() cannot use keyword-only parameters")
        if param.default is not inspect.Parameter.empty:
            raise UserFunctionError(
                f"{name}() default arguments are not supported; "
                f"every JMESPath argument is required"
            )


def _wrap(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    arity = len(inspect.signature(fn).parameters)
    signature = tuple(_ANY_TYPE for _ in range(arity))

    def method(self, *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - convert to a JMESPath error
            raise jmespath.exceptions.JMESPathError(
                f"{name}() raised {type(exc).__name__}: {exc}"
            ) from exc

    method.__name__ = f"_func_{name}"
    method.signature = signature  # type: ignore[attr-defined]
    return method
