"""SQLite + sqlite-vec database layer for the Confluence semantic indexer."""

from __future__ import annotations

import hashlib
import logging
import sqlite3

import sqlite_vec

from confluence_indexer.config import Perspective, get_settings

logger = logging.getLogger(__name__)

_connections: dict[str, sqlite3.Connection] = {}

SCHEMA_VERSION = 1

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS perspectives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    instruction TEXT NOT NULL,
    instruction_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL DEFAULT '',
    space_key TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    last_indexed TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_db_id INTEGER NOT NULL REFERENCES pages(id),
    heading_path TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    char_start INTEGER NOT NULL DEFAULT 0,
    char_end INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_db_id);
CREATE INDEX IF NOT EXISTS idx_pages_page_id ON pages(page_id);
"""


def _instruction_hash(instruction: str) -> str:
    return hashlib.sha256(instruction.encode()).hexdigest()


def init_db(
    label: str, db_path: str, perspectives: list[Perspective]
) -> sqlite3.Connection:
    from pathlib import Path

    db_dir = Path(db_path).parent
    db_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    _ensure_schema(conn, perspectives)
    _connections[label] = conn
    return conn


def get_conn(label: str) -> sqlite3.Connection:
    if label not in _connections:
        raise RuntimeError(f"DB for '{label}' not initialized — call init_db() first")
    return _connections[label]


def get_all_connections() -> dict[str, sqlite3.Connection]:
    return dict(_connections)


def close_all_dbs() -> None:
    for label in list(_connections):
        _connections[label].close()
    _connections.clear()


def _ensure_schema(
    conn: sqlite3.Connection, perspectives: list[Perspective]
) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()

    if not existing:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
        )

    dims = get_settings().embed_dimensions

    for p in perspectives:
        h = _instruction_hash(p.instruction)
        conn.execute(
            "INSERT OR IGNORE INTO perspectives (name, instruction, instruction_hash) "
            "VALUES (?, ?, ?)",
            (p.name, p.instruction, h),
        )

    for row in conn.execute("SELECT id FROM perspectives"):
        pid = row["id"]
        table = f"vec_p{pid}"
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE [{table}] USING vec0("
                f"embedding float[{dims}] distance_metric=cosine)"
            )
        except sqlite3.OperationalError:
            pass

    conn.commit()


def sync_perspectives(
    conn: sqlite3.Connection, configured: list[Perspective]
) -> bool:
    """Sync configured perspectives with stored ones.

    Returns True if any perspectives were added or had instruction changes
    (requiring re-embedding).
    """
    changed = False
    dims = get_settings().embed_dimensions

    for p in configured:
        h = _instruction_hash(p.instruction)
        row = conn.execute(
            "SELECT id, instruction_hash FROM perspectives WHERE name = ?",
            (p.name,),
        ).fetchone()

        if row is None:
            cursor = conn.execute(
                "INSERT INTO perspectives (name, instruction, instruction_hash) "
                "VALUES (?, ?, ?)",
                (p.name, p.instruction, h),
            )
            pid = cursor.lastrowid
            try:
                conn.execute(
                    f"CREATE VIRTUAL TABLE [vec_p{pid}] USING vec0("
                    f"embedding float[{dims}] distance_metric=cosine)"
                )
            except sqlite3.OperationalError:
                pass
            changed = True
            logger.info("Added new perspective: %s", p.name)

        elif row["instruction_hash"] != h:
            pid = row["id"]
            table = f"vec_p{pid}"
            try:
                conn.execute(f"DROP TABLE [{table}]")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                f"CREATE VIRTUAL TABLE [{table}] USING vec0("
                f"embedding float[{dims}] distance_metric=cosine)"
            )
            conn.execute(
                "UPDATE perspectives SET instruction = ?, instruction_hash = ? "
                "WHERE id = ?",
                (p.instruction, h, pid),
            )
            changed = True
            logger.info(
                "Perspective '%s' instruction changed — vec table recreated", p.name
            )

    conn.commit()
    return changed


def get_perspectives(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name, instruction FROM perspectives").fetchall()
    return [{"id": r["id"], "name": r["name"], "instruction": r["instruction"]} for r in rows]
