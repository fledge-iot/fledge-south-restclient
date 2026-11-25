"""Pytest configuration for fledge-south-restclient.

Shims the `fledge.common.logger` module so the plugin can be imported
outside a real Fledge install. Also makes the repository root importable
so the plugin module path resolves.
"""
from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_fledge_shim() -> None:
    if "fledge.common" in sys.modules:
        return
    fledge_pkg = sys.modules.setdefault("fledge", types.ModuleType("fledge"))
    fledge_pkg.__path__ = []  # mark as package for submodule imports

    fledge_common = types.ModuleType("fledge.common")
    logger_mod = types.ModuleType("fledge.common.logger")
    logger_mod.setup = lambda name, level=logging.INFO: logging.getLogger(name)
    fledge_common.logger = logger_mod

    sys.modules["fledge.common"] = fledge_common
    sys.modules["fledge.common.logger"] = logger_mod


def _install_async_ingest_shim() -> None:
    if "async_ingest" in sys.modules:
        return
    sys.modules["async_ingest"] = MagicMock()


_install_fledge_shim()
_install_async_ingest_shim()


FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
PAYLOADS_DIR = FIXTURES_DIR / "payloads"
MAPPINGS_DIR = FIXTURES_DIR / "mappings"
EXPECTED_DIR = FIXTURES_DIR / "expected"
