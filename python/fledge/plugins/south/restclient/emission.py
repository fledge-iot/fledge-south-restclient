"""Reading-structure emission layer.

Consumes ``(asset, datapoint, value, timestamp)`` tuples from
``extraction.extract`` and groups them into Fledge south readings per the
configured ``readingStructure``.

The output shape matches the Fledge interface 2.0 contract:
``{asset, timestamp, key (UUID), readings: {datapoint: value, …}}``.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable

try:
    from . import omf_hints
except ImportError:  # pragma: no cover - Fledge standalone script load
    import omf_hints  # type: ignore[no-redef]


READING_PER_ASSET = "reading_per_asset"
READING_PER_TAG = "reading_per_tag"
SINGLE_READING = "single_reading"

_VALID_STRUCTURES = {READING_PER_ASSET, READING_PER_TAG, SINGLE_READING}


def group(
    tuples: Iterable[tuple],
    reading_structure: str = READING_PER_ASSET,
    asset_name: str = "restclient",
    hints: dict | None = None,
) -> list[dict]:
    """Group extraction tuples into Fledge reading dicts.

    ``hints`` is an ``omf_hints`` resolver. Each reading holding a hinted
    datapoint gets an ``OMFHint`` datapoint naming those datapoints.
    """
    if reading_structure not in _VALID_STRUCTURES:
        raise ValueError(
            f"unknown readingStructure: {reading_structure!r} "
            f"(expected one of {sorted(_VALID_STRUCTURES)})"
        )

    tuples = list(tuples)
    if not tuples:
        return []

    if reading_structure == READING_PER_ASSET:
        return _group_per_asset(tuples, hints)
    if reading_structure == READING_PER_TAG:
        return _group_per_tag(tuples, hints)
    return _single_reading(tuples, asset_name, hints)


def _reading(asset: str, ts: str, readings: dict, dp_hints: dict) -> dict:
    omf_hints.attach(readings, dp_hints)
    return {
        "asset": asset,
        "timestamp": ts,
        "key": str(uuid.uuid4()),
        "readings": readings,
    }


def _note_hint(dp_hints: dict, hints: dict | None, asset: str, datapoint: str) -> None:
    hint = omf_hints.lookup(hints, asset, datapoint)
    if hint:
        dp_hints[datapoint] = hint


def _group_per_asset(tuples: list[tuple], hints: dict | None = None) -> list[dict]:
    """One reading per ``(asset, timestamp)`` group, in first-seen order."""
    groups: dict[tuple[str, str], tuple[dict[str, Any], dict]] = {}
    for asset, datapoint, value, ts in tuples:
        readings, dp_hints = groups.setdefault((asset, ts), ({}, {}))
        readings[datapoint] = value
        _note_hint(dp_hints, hints, asset, datapoint)

    return [
        _reading(asset, ts, readings, dp_hints)
        for (asset, ts), (readings, dp_hints) in groups.items()
    ]


def _group_per_tag(tuples: list[tuple], hints: dict | None = None) -> list[dict]:
    """One reading per tuple - single-datapoint readings."""
    out = []
    for asset, datapoint, value, ts in tuples:
        dp_hints: dict = {}
        _note_hint(dp_hints, hints, asset, datapoint)
        out.append(_reading(asset, ts, {datapoint: value}, dp_hints))
    return out


def _single_reading(
    tuples: list[tuple], asset_name: str, hints: dict | None = None
) -> list[dict]:
    """Exactly one reading per poll: flat datapoint dict, asset from config.

    The timestamp is taken from the first tuple. Datapoint name collisions
    follow last-write-wins. Hints are looked up against each tuple's own
    asset, before the assets collapse into ``asset_name``.
    """
    readings: dict[str, Any] = {}
    dp_hints: dict = {}
    for asset, datapoint, value, _ts in tuples:
        readings[datapoint] = value
        _note_hint(dp_hints, hints, asset, datapoint)
    return [_reading(asset_name, tuples[0][3], readings, dp_hints)]
