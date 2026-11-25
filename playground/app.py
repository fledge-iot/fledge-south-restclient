"""Local JMESPath playground for the restclient south plugin.

Evaluates expressions and mapping documents with the same engine the
plugin uses - including ``from_json``, ``to_array``, ``flatten``,
``$item`` / ``$parent``, and optional user ``def`` s from a functions
snippet.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

PLAYGROUND_DIR = Path(__file__).resolve().parent
REPO_ROOT = PLAYGROUND_DIR.parent
PLUGIN_DIR = REPO_ROOT / "python" / "fledge" / "plugins" / "south" / "restclient"
FIXTURES = REPO_ROOT / "tests" / "fixtures"

for path in (PLUGIN_DIR, PLAYGROUND_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from complete import complete as complete_query  # noqa: E402
from extraction import evaluate, extract, options_for_mapping  # noqa: E402
from mapping_loader import MappingError, load_from_text  # noqa: E402
from user_functions import UserFunctionError, load_callables  # noqa: E402

STATIC_DIR = PLAYGROUND_DIR / "static"

app = FastAPI(title="REST Client JMESPath Playground", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payload: Any = Field(alias="json")
    query: str = ""
    bind_iteration: bool = False
    functions: str = ""


class ExtractRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payload: Any = Field(alias="json")
    mapping: str
    functions: str = ""


class CompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    payload: Any = Field(alias="json")
    query: str = ""
    cursor: int = 0
    bind_iteration: bool = False
    functions: str = ""


def _iteration_context(payload: Any, bind_iteration: bool) -> Any:
    if bind_iteration:
        return {"_item": payload, "_parent": payload}
    return payload


def _compile_functions(source: str | None) -> tuple[Any, list[str]]:
    """Return ``(jmespath.Options | None, exported names)``.

    Comment-only / empty snippets are a no-op so the playground box can
    hold a commented example without failing compile.
    """
    if not source or not source.strip():
        return None, []
    try:
        names = list(load_callables(source))
    except UserFunctionError as exc:
        if "no public functions" in str(exc):
            return None, []
        raise
    options = options_for_mapping({"functions": source})
    return options, names


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/evaluate")
def api_evaluate(req: EvaluateRequest) -> dict[str, Any]:
    query = req.query.strip()
    try:
        options, names = _compile_functions(req.functions)
    except UserFunctionError as exc:
        return {"result": None, "error": f"functions: {exc}", "functions": []}
    if not query:
        return {"result": None, "error": None, "functions": names}
    context = _iteration_context(req.payload, req.bind_iteration)
    try:
        return {
            "result": evaluate(query, context, options=options),
            "error": None,
            "functions": names,
        }
    except Exception as exc:  # noqa: BLE001 - surface any eval failure
        return {"result": None, "error": str(exc), "functions": names}


@app.post("/api/extract")
def api_extract(req: ExtractRequest) -> dict[str, Any]:
    try:
        mapping = load_from_text(req.mapping)
    except MappingError as exc:
        return {"tuples": [], "error": str(exc), "functions": []}
    if (
        req.functions
        and req.functions.strip()
        and not mapping.get("functions")
        and not mapping.get("functionsFile")
    ):
        try:
            _, box_names = _compile_functions(req.functions)
        except UserFunctionError as exc:
            return {"tuples": [], "error": f"functions: {exc}", "functions": []}
        if box_names:
            mapping["functions"] = req.functions
    try:
        _, names = _compile_functions(mapping.get("functions") or "")
        tuples = extract(req.payload, mapping)
    except UserFunctionError as exc:
        return {"tuples": [], "error": f"functions: {exc}", "functions": []}
    except Exception as exc:  # noqa: BLE001 - mapping eval should not 500
        return {"tuples": [], "error": str(exc), "functions": []}
    return {
        "tuples": [
            {
                "asset": asset,
                "datapoint": datapoint,
                "value": value,
                "timestamp": timestamp,
            }
            for asset, datapoint, value, timestamp in tuples
        ],
        "error": None,
        "functions": names,
    }


@app.post("/api/complete")
def api_complete(req: CompleteRequest) -> dict[str, Any]:
    context = _iteration_context(req.payload, req.bind_iteration)
    extra: list[tuple[str, str]] = []
    options = None
    try:
        options, names = _compile_functions(req.functions)
        extra = [(name, "user - playground functions box") for name in names]
    except UserFunctionError:
        options = None
    result = complete_query(
        req.query, req.cursor, context, options=options, extra_functions=extra
    )
    return result


_EXAMPLE_QUERIES = {
    "batch_runs_single_active": "from_json(value[0].Data).M01.Import",
    "flatten_hierarchy": (
        "flatten(from_json(value[0].Data), ['SiteId', 'LineId', 'EquipmentId'])"
    ),
    "flatten_tag_map": (
        "flatten(from_json(value[0].Data), ['SiteId', 'LineId', 'EquipmentId'])"
    ),
    "site_snapshot": (
        "flatten(from_json(value[0].Data), ['SiteId', 'LineId', 'EquipmentId'])"
    ),
    "user_functions": "name",
}


@app.get("/api/examples")
def api_examples() -> dict[str, Any]:
    payload_dir = FIXTURES / "payloads"
    mapping_dir = FIXTURES / "mappings"
    examples = []
    if payload_dir.is_dir():
        for path in sorted(payload_dir.glob("*.json")):
            mapping_path = mapping_dir / f"{path.stem}.yaml"
            mapping_text = mapping_path.read_text(encoding="utf-8") if mapping_path.is_file() else None
            examples.append(
                {
                    "name": path.stem,
                    "payload": json.loads(path.read_text(encoding="utf-8")),
                    "mapping": mapping_text,
                    "functions": _functions_from_mapping(mapping_text),
                    "query": _EXAMPLE_QUERIES.get(path.stem),
                }
            )
    return {"examples": examples}


def _functions_from_mapping(mapping_text: str | None) -> str | None:
    if not mapping_text:
        return None
    try:
        parsed = yaml.safe_load(mapping_text)
    except yaml.YAMLError:
        return None
    if isinstance(parsed, dict) and isinstance(parsed.get("functions"), str):
        return parsed["functions"]
    return None


def main() -> None:
    import uvicorn

    uvicorn.run("app:app", host="127.0.0.1", port=8765, reload=True)


if __name__ == "__main__":
    main()
