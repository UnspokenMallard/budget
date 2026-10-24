"""Fafnir's store: state/budget.db (WAL), state files, config.toml.

Paths are module attributes read at call time, so a test points them at a scratch directory
(`store.ROOT`, `store.STATE`, `store.DB_PATH`, `store.CONFIG`) the way Jarvis's selftest does
with `core.ROOT`. Stdlib only: imported by Jarvis's venv, Mimir's venv and Fafnir's own.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("FAFNIR_HOME", Path.home() / ".budget"))
STATE = ROOT / "state"
DB_PATH = STATE / "budget.db"
CONFIG = ROOT / "config.toml"
log = logging.getLogger("fafnir")

_META = "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


@contextlib.contextmanager
def connect():
    """`with connect() as conn:` commits or rolls back, and always closes (one handle per call)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def load_config() -> dict:
    """~/.budget/config.toml; missing means every module's own defaults."""
    try:
        with open(CONFIG, "rb") as f:
            return tomllib.load(f)
    except OSError:
        return {}


def set_meta(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(_META)
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))


def get_meta(key: str) -> str | None:
    with connect() as conn:
        conn.execute(_META)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None
