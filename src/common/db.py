"""SQLite access, in one place.

The three tasks all touch a database, so the connection rules live here
rather than being re-decided in each script.

Two things are deliberate and are the reason this file exists at all:

**Every query is parameterised.** There is no string formatting into SQL
anywhere in this project, including the places where the value is "obviously
safe" because it came from our own CSV. A scanner's input is, by definition,
somebody else's data; the moment one query is built by concatenation, the
rule is no longer checkable by reading the file.

**Writes are idempotent.** Each table has a natural key and each insert is
an upsert against it, so running an import twice leaves the same rows rather
than twice as many. An import script that cannot be safely re-run is one
that has to be babysat.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB = Path("accuknox.db")


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open a connection with the pragmas this project assumes."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    # Off by default in SQLite, for backwards compatibility. A schema with
    # foreign keys that are not enforced is documentation, not a constraint.
    connection.execute("PRAGMA foreign_keys = ON")
    # Readers do not block the writer, which matters as soon as anything
    # reads the database while an import is running.
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY,
    -- The natural key. Open Library has no stable id across all records,
    -- and the same work appears under several editions, so a book is
    -- identified by what makes it that book.
    title            TEXT    NOT NULL,
    author           TEXT    NOT NULL,
    publication_year INTEGER,
    source           TEXT    NOT NULL,
    fetched_at       TEXT    NOT NULL,
    UNIQUE (title, author, publication_year)
);

CREATE TABLE IF NOT EXISTS students (
    id         INTEGER PRIMARY KEY,
    student_id TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    subject    TEXT,
    score      REAL    NOT NULL,
    fetched_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    -- Stored lower-cased, because Alice@x.com and alice@x.com are one
    -- person and a UNIQUE index on the raw string would not say so.
    email      TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    imported_at TEXT   NOT NULL
);

/*
 * Rows that did not make it, and why.
 *
 * A row that fails validation is not dropped. Dropping is the dangerous
 * failure: the import reports success, the row is gone, and nobody finds
 * out until somebody asks where a user went. Quarantining keeps the
 * original line, the line number and the reason, so the import is both
 * safe to re-run and auditable afterwards.
 */
CREATE TABLE IF NOT EXISTS rejected_rows (
    id          INTEGER PRIMARY KEY,
    source      TEXT    NOT NULL,
    line_number INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    raw         TEXT    NOT NULL,
    rejected_at TEXT    NOT NULL
);

/* One row per run, so "did the import work" is answerable after the fact. */
CREATE TABLE IF NOT EXISTS import_runs (
    id         INTEGER PRIMARY KEY,
    task       TEXT    NOT NULL,
    source     TEXT    NOT NULL,
    started_at TEXT    NOT NULL,
    seen       INTEGER NOT NULL,
    written    INTEGER NOT NULL,
    rejected   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS books_by_year  ON books (publication_year);
CREATE INDEX IF NOT EXISTS users_by_name  ON users (name);
CREATE INDEX IF NOT EXISTS rejected_by_source ON rejected_rows (source, line_number);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.commit()


def record_run(
    connection: sqlite3.Connection,
    task: str,
    source: str,
    started_at: str,
    seen: int,
    written: int,
    rejected: int,
) -> None:
    connection.execute(
        "INSERT INTO import_runs (task, source, started_at, seen, written, rejected)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (task, source, started_at, seen, written, rejected),
    )
    connection.commit()


def clear_rejections(connection: sqlite3.Connection, source: str) -> None:
    """Forget the previous import's verdicts on this source.

    Called at the start of each import, for the same reason the data tables
    upsert: re-reading a source supersedes what the last read concluded about
    it. Without this, the quarantine grows a fresh copy of every bad row on
    every run, and the report prints each rejection as many times as the
    import has ever been run - which makes the one table whose job is to be
    trustworthy the least trustworthy thing in the database.

    `import_runs` is the opposite case and deliberately appends: it is a log
    of attempts, and an attempt that happened does not stop having happened.
    """
    connection.execute("DELETE FROM rejected_rows WHERE source = ?", (source,))


def reject(
    connection: sqlite3.Connection,
    source: str,
    line_number: int,
    reason: str,
    raw: str,
    when: str,
) -> None:
    connection.execute(
        "INSERT INTO rejected_rows (source, line_number, reason, raw, rejected_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (source, line_number, reason, raw, when),
    )
