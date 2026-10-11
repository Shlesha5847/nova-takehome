"""Database persistence layer for the Nova freight audit harness.

Manages SQLite storage at <state-dir>/state.db with strict foreign keys,
WAL mode, transactions, document catalog, and page-level text provenance.
Properly closes connections to avoid Windows file locks.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

SCHEMA_VERSION = "1.0"

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_name TEXT PRIMARY KEY,
    batch_path TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    batch_name TEXT NOT NULL,
    file_name TEXT NOT NULL,
    parent_doc_id TEXT,
    doc_type TEXT NOT NULL,
    received_date TEXT,
    is_scan INTEGER DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (batch_name) REFERENCES batches(batch_name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS document_pages (
    doc_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    page_text TEXT NOT NULL,
    PRIMARY KEY (doc_id, page_number),
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback_records (
    feedback_id TEXT PRIMARY KEY,
    source_file TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    ingested_at TEXT NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    as_of TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    report_json TEXT NOT NULL,
    PRIMARY KEY (as_of, mode)
);
"""


def get_db_path(state_dir: str | Path) -> Path:
    """Resolve and return path to state.db under state_dir."""
    dir_path = Path(state_dir)
    dir_path.mkdir(parents=True, exist_ok=True)
    return dir_path / "state.db"


def get_connection(state_dir: str | Path) -> sqlite3.Connection:
    """Connect to SQLite at <state-dir>/state.db with foreign keys enabled."""
    db_path = get_db_path(state_dir)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextlib.contextmanager
def open_db(state_dir: str | Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager yielding a connection that is always closed on exit."""
    conn = get_connection(state_dir)
    try:
        yield conn
    finally:
        conn.close()


def init_db(state_dir: str | Path) -> None:
    """Initialize database and schema if not already present."""
    with open_db(state_dir) as conn:
        with conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?);",
                ("schema_version", SCHEMA_VERSION),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?);",
                ("initialized_at", datetime.now(timezone.utc).isoformat()),
            )


def set_meta(state_dir: str | Path, key: str, value: str) -> None:
    """Set a metadata key-value pair."""
    with open_db(state_dir) as conn:
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                (key, value),
            )


def get_meta(state_dir: str | Path, key: str) -> str | None:
    """Retrieve a metadata value by key."""
    with open_db(state_dir) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?;", (key,)).fetchone()
        return row["value"] if row else None


def record_batch(state_dir: str | Path, batch_name: str, batch_path: str) -> None:
    """Record an ingested batch."""
    now = datetime.now(timezone.utc).isoformat()
    with open_db(state_dir) as conn:
        with conn:
            conn.execute(
                """INSERT INTO batches (batch_name, batch_path, ingested_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(batch_name) DO UPDATE SET
                       batch_path = excluded.batch_path,
                       ingested_at = excluded.ingested_at;""",
                (batch_name, batch_path, now),
            )


def list_batches(state_dir: str | Path) -> list[dict[str, Any]]:
    """List all ingested batches."""
    with open_db(state_dir) as conn:
        rows = conn.execute(
            "SELECT batch_name, batch_path, ingested_at FROM batches ORDER BY ingested_at ASC;"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_batch_documents(state_dir: str | Path, batch_name: str) -> None:
    """Delete all documents and pages for a batch to allow idempotent re-ingestion."""
    with open_db(state_dir) as conn:
        with conn:
            conn.execute("DELETE FROM documents WHERE batch_name = ?;", (batch_name,))


def save_document(
    state_dir: str | Path,
    doc_id: str,
    batch_name: str,
    file_name: str,
    parent_doc_id: str | None = None,
    doc_type: str = "eml",
    received_date: str | None = None,
    is_scan: bool = False,
    pages: list[str] | None = None,
) -> None:
    """Persist a document and all its extracted pages transactionally."""
    now = datetime.now(timezone.utc).isoformat()
    pages_list = pages or []
    page_count = len(pages_list)

    with open_db(state_dir) as conn:
        with conn:
            conn.execute(
                """INSERT INTO documents (doc_id, batch_name, file_name, parent_doc_id, doc_type, received_date, is_scan, page_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(doc_id) DO UPDATE SET
                       parent_doc_id = excluded.parent_doc_id,
                       doc_type = excluded.doc_type,
                       received_date = excluded.received_date,
                       is_scan = excluded.is_scan,
                       page_count = excluded.page_count;""",
                (
                    doc_id,
                    batch_name,
                    file_name,
                    parent_doc_id,
                    doc_type,
                    received_date,
                    1 if is_scan else 0,
                    page_count,
                    now,
                ),
            )
            # Refresh page text entries
            conn.execute("DELETE FROM document_pages WHERE doc_id = ?;", (doc_id,))
            for idx, text in enumerate(pages_list, start=1):
                conn.execute(
                    """INSERT INTO document_pages (doc_id, page_number, page_text)
                       VALUES (?, ?, ?);""",
                    (doc_id, idx, text),
                )


def get_document(state_dir: str | Path, doc_id: str) -> dict[str, Any] | None:
    """Retrieve document metadata by doc_id."""
    with open_db(state_dir) as conn:
        row = conn.execute("SELECT * FROM documents WHERE doc_id = ?;", (doc_id,)).fetchone()
        return dict(row) if row else None


def get_document_pages(state_dir: str | Path, doc_id: str) -> list[str]:
    """Retrieve all pages of a document in order."""
    with open_db(state_dir) as conn:
        rows = conn.execute(
            "SELECT page_text FROM document_pages WHERE doc_id = ? ORDER BY page_number ASC;",
            (doc_id,),
        ).fetchall()
        return [r["page_text"] for r in rows]


def get_page_text(state_dir: str | Path, doc_id: str, page_number: int) -> str | None:
    """Retrieve text for a specific page (1-indexed)."""
    with open_db(state_dir) as conn:
        row = conn.execute(
            "SELECT page_text FROM document_pages WHERE doc_id = ? AND page_number = ?;",
            (doc_id, page_number),
        ).fetchone()
        return row["page_text"] if row else None


def list_documents(state_dir: str | Path, batch_name: str | None = None) -> list[dict[str, Any]]:
    """List documents, optionally filtered by batch."""
    with open_db(state_dir) as conn:
        if batch_name:
            rows = conn.execute(
                "SELECT * FROM documents WHERE batch_name = ? ORDER BY doc_id ASC;",
                (batch_name,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM documents ORDER BY doc_id ASC;").fetchall()
        return [dict(r) for r in rows]


def get_documents_up_to_date(state_dir: str | Path, as_of_date: str) -> list[dict[str, Any]]:
    """Retrieve all documents received on or before as_of_date."""
    with open_db(state_dir) as conn:
        rows = conn.execute(
            """SELECT * FROM documents
               WHERE received_date IS NOT NULL AND received_date <= ?
               ORDER BY received_date ASC, doc_id ASC;""",
            (as_of_date,),
        ).fetchall()
        return [dict(r) for r in rows]



def record_feedback(
    state_dir: str | Path,
    feedback_file: str,
    payload: dict[str, Any],
) -> None:
    """Record ingested feedback payload."""
    now = datetime.now(timezone.utc).isoformat()
    fb_id = str(payload.get("world", "")) + "_" + str(payload.get("after_batch", Path(feedback_file).stem))
    items = payload.get("items", [])
    item_count = len(items) if isinstance(items, list) else 0
    raw_str = json.dumps(payload)
    with open_db(state_dir) as conn:
        with conn:
            conn.execute(
                """INSERT INTO feedback_records (feedback_id, source_file, item_count, ingested_at, raw_json)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(feedback_id) DO UPDATE SET
                       source_file = excluded.source_file,
                       item_count = excluded.item_count,
                       ingested_at = excluded.ingested_at,
                       raw_json = excluded.raw_json;""",
                (fb_id, feedback_file, item_count, now, raw_str),
            )


def list_feedback_records(state_dir: str | Path) -> list[dict[str, Any]]:
    """List all ingested feedback records."""
    with open_db(state_dir) as conn:
        rows = conn.execute(
            "SELECT feedback_id, source_file, item_count, ingested_at FROM feedback_records ORDER BY ingested_at ASC;"
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_feedback_payloads(state_dir: str | Path) -> list[dict[str, Any]]:
    """Retrieve all parsed feedback payloads in order of ingestion."""
    with open_db(state_dir) as conn:
        rows = conn.execute(
            "SELECT raw_json FROM feedback_records ORDER BY ingested_at ASC;"
        ).fetchall()
        payloads = []
        for r in rows:
            try:
                payloads.append(json.loads(r["raw_json"]))
            except Exception:
                pass
        return payloads


def save_report(
    state_dir: str | Path,
    as_of: str,
    mode: str,
    report_dict: dict[str, Any],
) -> None:
    """Save generated report to database."""
    now = datetime.now(timezone.utc).isoformat()
    raw_str = json.dumps(report_dict)
    with open_db(state_dir) as conn:
        with conn:
            conn.execute(
                """INSERT INTO reports (as_of, mode, created_at, report_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(as_of, mode) DO UPDATE SET
                       created_at = excluded.created_at,
                       report_json = excluded.report_json;""",
                (as_of, mode, now, raw_str),
            )


def get_latest_previous_report(
    state_dir: str | Path,
    as_of: str,
    mode: str,
) -> dict[str, Any] | None:
    """Retrieve the most recent report strictly before as_of date."""
    with open_db(state_dir) as conn:
        row = conn.execute(
            """SELECT report_json FROM reports
               WHERE as_of < ? AND mode = ?
               ORDER BY as_of DESC, created_at DESC
               LIMIT 1;""",
            (as_of, mode),
        ).fetchone()
        if row and row["report_json"]:
            try:
                return json.loads(row["report_json"])
            except Exception:
                return None
        return None
