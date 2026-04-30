"""SQLite + sqlite-vec database layer for the integrated Confluence indexer."""

from __future__ import annotations

import hashlib
import logging
import sqlite3

import sqlite_vec

from confluence_mcp.config import Perspective

logger = logging.getLogger(__name__)

_connections: dict[str, sqlite3.Connection] = {}

SCHEMA_VERSION = 3

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
    last_indexed TEXT NOT NULL DEFAULT (datetime('now')),
    last_accessed TEXT NOT NULL DEFAULT (datetime('now')),
    version_date TEXT,
    median_update_interval_days REAL,
    version_count INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_db_id INTEGER NOT NULL REFERENCES pages(id),
    heading_path TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    char_start INTEGER NOT NULL DEFAULT 0,
    char_end INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS index_scopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE,
    scope_type TEXT NOT NULL DEFAULT 'space',
    scope_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_db_id);
CREATE INDEX IF NOT EXISTS idx_pages_page_id ON pages(page_id);
"""


def _instruction_hash(instruction: str) -> str:
    return hashlib.sha256(instruction.encode()).hexdigest()


def init_db(
    db_path: str, perspectives: list[Perspective], dimensions: int
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

    _ensure_schema(conn, perspectives, dimensions)
    _connections["default"] = conn
    return conn


def get_conn(label: str = "default") -> sqlite3.Connection:
    if label not in _connections:
        raise RuntimeError(f"DB for '{label}' not initialized — call init_db() first")
    return _connections[label]


def close_all_dbs() -> None:
    for label in list(_connections):
        _connections[label].close()
    _connections.clear()


def _ensure_schema(
    conn: sqlite3.Connection, perspectives: list[Perspective], dimensions: int
) -> None:
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()

    if not existing:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
        )
    else:
        # Migrate: add new columns/tables if missing
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pages)").fetchall()
        }
        migrations = [
            ("last_accessed", "TEXT NOT NULL DEFAULT (datetime('now'))"),
            ("version_date", "TEXT"),
            ("median_update_interval_days", "REAL"),
            ("version_count", "INTEGER DEFAULT 1"),
        ]
        for col_name, col_def in migrations:
            if col_name not in cols:
                conn.execute(f"ALTER TABLE pages ADD COLUMN {col_name} {col_def}")

        # Add index_scopes table if missing
        has_scopes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='index_scopes'"
        ).fetchone()
        if not has_scopes:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS index_scopes ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "label TEXT NOT NULL UNIQUE, "
                "scope_type TEXT NOT NULL DEFAULT 'space', "
                "scope_id TEXT NOT NULL)"
            )

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
                f"embedding float[{dimensions}] distance_metric=cosine)"
            )
        except sqlite3.OperationalError:
            pass

    conn.commit()


def sync_perspectives(
    conn: sqlite3.Connection, configured: list[Perspective], dimensions: int
) -> bool:
    """Sync configured perspectives with stored ones.

    Returns True if any perspectives were added or had instruction changes.
    """
    changed = False

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
                    f"embedding float[{dimensions}] distance_metric=cosine)"
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
                f"embedding float[{dimensions}] distance_metric=cosine)"
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


def add_perspective(
    conn: sqlite3.Connection, name: str, instruction: str, dimensions: int
) -> dict:
    """Add a new perspective at runtime. Returns perspective info."""
    h = _instruction_hash(instruction)
    existing = conn.execute(
        "SELECT id FROM perspectives WHERE name = ?", (name,)
    ).fetchone()
    if existing:
        raise ValueError(f"Perspective '{name}' already exists")

    cursor = conn.execute(
        "INSERT INTO perspectives (name, instruction, instruction_hash) "
        "VALUES (?, ?, ?)",
        (name, instruction, h),
    )
    pid = cursor.lastrowid
    conn.execute(
        f"CREATE VIRTUAL TABLE [vec_p{pid}] USING vec0("
        f"embedding float[{dimensions}] distance_metric=cosine)"
    )
    conn.commit()
    return {"id": pid, "name": name, "instruction": instruction}


def remove_perspective(conn: sqlite3.Connection, name: str) -> bool:
    """Remove a perspective and its vec table. Returns True if found."""
    row = conn.execute(
        "SELECT id FROM perspectives WHERE name = ?", (name,)
    ).fetchone()
    if not row:
        return False

    pid = row["id"]
    table = f"vec_p{pid}"
    try:
        conn.execute(f"DROP TABLE [{table}]")
    except sqlite3.OperationalError:
        pass
    conn.execute("DELETE FROM perspectives WHERE id = ?", (pid,))
    conn.commit()
    return True


def get_perspectives(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, name, instruction FROM perspectives").fetchall()
    return [{"id": r["id"], "name": r["name"], "instruction": r["instruction"]} for r in rows]


# ---------------------------------------------------------------------------
# Index scopes (stored in DB, managed via MCP tools or CLI)
# ---------------------------------------------------------------------------


def list_index_scopes(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, label, scope_type, scope_id FROM index_scopes").fetchall()
    return [{"id": r["id"], "label": r["label"], "scope_type": r["scope_type"], "scope_id": r["scope_id"]} for r in rows]


def add_index_scope(conn: sqlite3.Connection, label: str, scope_type: str, scope_id: str) -> dict:
    """Add an index scope. scope_type is 'space' or 'page_tree'."""
    if scope_type not in ("space", "page_tree"):
        raise ValueError(f"scope_type must be 'space' or 'page_tree', got '{scope_type}'")
    existing = conn.execute("SELECT id FROM index_scopes WHERE label = ?", (label,)).fetchone()
    if existing:
        raise ValueError(f"Scope '{label}' already exists")
    cursor = conn.execute(
        "INSERT INTO index_scopes (label, scope_type, scope_id) VALUES (?, ?, ?)",
        (label, scope_type, scope_id),
    )
    conn.commit()
    return {"id": cursor.lastrowid, "label": label, "scope_type": scope_type, "scope_id": scope_id}


def remove_index_scope(conn: sqlite3.Connection, label: str) -> bool:
    """Remove an index scope by label. Returns True if found."""
    row = conn.execute("SELECT id FROM index_scopes WHERE label = ?", (label,)).fetchone()
    if not row:
        return False
    conn.execute("DELETE FROM index_scopes WHERE id = ?", (row["id"],))
    conn.commit()
    return True
