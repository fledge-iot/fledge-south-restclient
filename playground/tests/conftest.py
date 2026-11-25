"""Put the playground and plugin packages on sys.path for tests."""
from __future__ import annotations

import sys
from pathlib import Path

PLAYGROUND_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = (
    PLAYGROUND_DIR.parent / "python" / "fledge" / "plugins" / "south" / "restclient"
)

for path in (PLAYGROUND_DIR, PLUGIN_DIR):
    resolved = str(path)
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
