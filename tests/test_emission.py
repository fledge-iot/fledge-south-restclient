"""Unit tests for the emission layer."""
from __future__ import annotations

import uuid

import pytest

from python.fledge.plugins.south.restclient import emission


T1 = "2026-04-16 10:31:33.451000+00:00"
T2 = "2026-04-16 10:31:34.001000+00:00"


class TestReadingPerAsset:
    def test_groups_by_asset_and_timestamp(self):
        tuples = [
            ("a", "x", 1, T1),
            ("a", "y", 2, T1),
            ("b", "z", 3, T1),
        ]
        readings = emission.group(tuples, emission.READING_PER_ASSET)
        assert len(readings) == 2
        a = next(r for r in readings if r["asset"] == "a")
        b = next(r for r in readings if r["asset"] == "b")
        assert a["readings"] == {"x": 1, "y": 2}
        assert b["readings"] == {"z": 3}

    def test_different_timestamps_split_groups(self):
        tuples = [
            ("a", "x", 1, T1),
            ("a", "x", 2, T2),
        ]
        readings = emission.group(tuples, emission.READING_PER_ASSET)
        assert len(readings) == 2

    def test_preserves_insertion_order(self):
        tuples = [
            ("b", "x", 1, T1),
            ("a", "x", 2, T1),
        ]
        readings = emission.group(tuples, emission.READING_PER_ASSET)
        assert [r["asset"] for r in readings] == ["b", "a"]


class TestReadingPerTag:
    def test_one_reading_per_tuple(self):
        tuples = [
            ("a", "x", 1, T1),
            ("a", "y", 2, T1),
        ]
        readings = emission.group(tuples, emission.READING_PER_TAG)
        assert len(readings) == 2
        assert readings[0]["readings"] == {"x": 1}
        assert readings[1]["readings"] == {"y": 2}


class TestSingleReading:
    def test_flattens_into_one_reading(self):
        tuples = [
            ("a", "x", 1, T1),
            ("b", "y", 2, T2),
        ]
        readings = emission.group(tuples, emission.SINGLE_READING, asset_name="combined")
        assert len(readings) == 1
        assert readings[0]["asset"] == "combined"
        assert readings[0]["readings"] == {"x": 1, "y": 2}

    def test_uses_first_timestamp(self):
        tuples = [
            ("a", "x", 1, T1),
            ("a", "y", 2, T2),
        ]
        readings = emission.group(tuples, emission.SINGLE_READING, asset_name="c")
        assert readings[0]["timestamp"] == T1

    def test_last_write_wins_on_collision(self):
        tuples = [
            ("a", "x", 1, T1),
            ("b", "x", 99, T2),
        ]
        readings = emission.group(tuples, emission.SINGLE_READING, asset_name="c")
        assert readings[0]["readings"]["x"] == 99


class TestKeyAndShape:
    def test_every_reading_has_uuid_key(self):
        tuples = [("a", "x", 1, T1)]
        readings = emission.group(tuples, emission.READING_PER_ASSET)
        uuid.UUID(readings[0]["key"])

    def test_keys_unique_across_readings(self):
        tuples = [("a", "x", 1, T1), ("b", "y", 2, T2)]
        readings = emission.group(tuples, emission.READING_PER_ASSET)
        assert readings[0]["key"] != readings[1]["key"]

    def test_reading_shape(self):
        tuples = [("a", "x", 1, T1)]
        readings = emission.group(tuples, emission.READING_PER_ASSET)
        assert set(readings[0].keys()) == {"asset", "timestamp", "key", "readings"}


class TestEdgeCases:
    def test_empty_tuples_returns_empty(self):
        assert emission.group([], emission.READING_PER_ASSET) == []
        assert emission.group([], emission.READING_PER_TAG) == []
        assert emission.group([], emission.SINGLE_READING, "x") == []

    def test_unknown_structure_raises(self):
        with pytest.raises(ValueError, match="unknown readingStructure"):
            emission.group([("a", "x", 1, T1)], "frobnicate")
