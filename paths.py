"""Filesystem anchors for the app.

All app paths are anchored to the directory containing the source files
(``BASE_DIR``) rather than the current working directory, so the monitor
behaves the same regardless of where a scheduler/service launches it from
(review finding M8). User-edited ``config.toml`` stays in ``BASE_DIR``;
app-generated state/artifact files live under ``data/`` and logs under
``logs/`` (both created at startup).
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
