"""Type hints declared in the mapping and attached to emitted readings.

Fledge's OMF north plugin decides a datapoint's type from the first value
it sees. REST sources usually serialize whole numbers without a decimal
point, so an analog value that reads exactly ``0`` arrives as an integer,
and later fractional values are rejected. A hint fixes the type at
creation:

.. code-block:: yaml

    tags:
      - name: "SITE-A_Line1_Pump01_Readings_FlowRate"
        asset: "SITE-A-Pump01"
        datapoint: "flow_rate"
        omfHint: {number: float64}

    # Or once per datapoint name, for every asset and every rule:
    omfHints:
      flow_rate: {number: float64}

Each reading that carries a hinted datapoint gets an ``OMFHint`` datapoint
holding the per-datapoint hint form, which the OMF north plugin consumes
and does not send downstream::

    {"datapoint":[{"name":"flow_rate","number":"float64"}]}

A tag's own ``omfHint`` wins over the mapping-level ``omfHints`` entry for
the same datapoint. A tag with no ``asset`` hints its datapoint on every
asset. Any datapoint hint key passes through (``number``, ``integer``,
``tagName``, ``uom``, ``minimum``, ``maximum``, ...); only ``name`` is
reserved, since it is the datapoint's own name.
"""
from __future__ import annotations

import json
from typing import Any, Callable

HINT_DATAPOINT = "OMFHint"

# (asset or None, datapoint) -> hint object
Resolver = dict


def validate_hint(raw: Any, where: str, error: Callable[[str], Exception]) -> dict:
    """Return ``raw`` as a hint object, or raise ``error`` describing the problem."""
    if not isinstance(raw, dict) or not raw:
        raise error(f'{where} must be a non-empty object such as {{"number": "float64"}}')
    hint: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise error(f"{where} keys must be non-empty strings")
        if key == "name":
            raise error(f"{where} must not set `name`; the datapoint supplies it")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise error(f"{where}.{key} must be a string or a number")
        hint[key] = value
    return hint


def validate_mapping_hints(raw: Any, error: Callable[[str], Exception]) -> dict:
    """Validate a mapping's top-level ``omfHints``: datapoint name -> hint."""
    if not isinstance(raw, dict):
        raise error("`omfHints` must be an object mapping datapoint names to hints")
    return {
        dp: validate_hint(hint, f"omfHints[{dp!r}]", error)
        for dp, hint in raw.items()
        if _check_name(dp, error)
    }


def _check_name(dp: Any, error: Callable[[str], Exception]) -> bool:
    if not isinstance(dp, str) or not dp.strip():
        raise error("`omfHints` keys must be non-empty datapoint names")
    return True


def build_resolver(mapping: dict) -> Resolver:
    """Collect every hint in a validated mapping, keyed by (asset, datapoint)."""
    resolver: Resolver = {}
    for dp, hint in (mapping.get("omfHints") or {}).items():
        resolver[(None, dp)] = hint

    def add(tags: Any) -> None:
        for tag in tags or ():
            hint = tag.get("omfHint")
            if hint:
                resolver[(tag.get("asset"), tag.get("datapoint") or tag["name"])] = hint

    add(mapping.get("tags"))
    for rule in mapping.get("extractions") or ():
        if isinstance(rule, dict):
            add(rule.get("tags"))
    return resolver


def lookup(resolver: Resolver | None, asset: str, datapoint: str) -> dict | None:
    if not resolver:
        return None
    return resolver.get((asset, datapoint)) or resolver.get((None, datapoint))


def attach(readings: dict, hints: dict[str, dict]) -> None:
    """Add the ``OMFHint`` datapoint for ``hints`` (datapoint -> hint) to ``readings``.

    Leaves ``readings`` alone when there is nothing to hint or when the source
    data already produced an ``OMFHint`` datapoint of its own.
    """
    present = {dp: hint for dp, hint in hints.items() if dp in readings}
    if not present or HINT_DATAPOINT in readings:
        return
    readings[HINT_DATAPOINT] = json.dumps(
        {"datapoint": [{"name": dp, **hint} for dp, hint in present.items()]},
        separators=(",", ":"),
    )
