"""Fafnir's store: state files and config.toml. Fafnir keeps no database of its own: the ledger is
telemetry in Mimir (`fafnir.*` streams) and the small counters are `fafnir.meta` records.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.CONFIG`) the way Jarvis's selftest does with `core.ROOT`.
Stdlib only: imported by Jarvis's venv, Mimir's venv and Fafnir's own.
"""
from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

from ._mimir import records as mr

ROOT = Path(os.environ.get("FAFNIR_HOME", Path.home() / ".budget"))
STATE = ROOT / "state"
CONFIG = ROOT / "config.toml"
SOURCE = "fafnir"
META = "fafnir.meta"
log = logging.getLogger("fafnir")


def load_config() -> dict:
    """~/.budget/config.toml; missing means every module's own defaults."""
    try:
        with open(CONFIG, "rb") as f:
            return tomllib.load(f)
    except OSError:
        return {}


def set_meta(key: str, value: str) -> None:
    mr.put(SOURCE, META, key, {"value": value})


def get_meta(key: str) -> str | None:
    """Raises mimirrecords.MimirDown when Mimir does not answer."""
    row = mr.get(META, key, source=SOURCE)
    return row["value"] if row else None
