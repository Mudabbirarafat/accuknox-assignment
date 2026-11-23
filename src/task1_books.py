"""Task 1 - fetch books from a REST API, store them in SQLite, display them.

    python -m src.task1_books --query "security engineering" --limit 10

The API is Open Library's search endpoint. It was picked because it needs no
key, which means this repository can be cloned and run by somebody who has
never spoken to me, and there is no credential anywhere in it to leak.

The interesting part of this task is not the fetch. It is that an API is
somebody else's database, so every field is optional until proved otherwise.
Open Library genuinely returns records with no author, no year, an empty
title, and years in the future; `record["author_name"][0]` crashes on the
first of those and silently stores nonsense for the rest. So the pipeline is
fetch -> validate -> upsert, and a record that fails validation is kept with
its reason rather than dropped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple

from src.common import console, db, http

SEARCH_URL = "https://openlibrary.org/search.json"

#: Asking for only the fields we use. The full record is roughly a hundred
#: fields per book; requesting four of them is a smaller response, a smaller
#: parse and a smaller thing to reason about.
FIELDS = "title,author_name,first_publish_year"

#: Gutenberg's press is the floor. Anything "published" before it is a data
#: error, and anything published after next year is either a typo or a
#: pre-announcement, neither of which belongs in a table of publication years.
EARLIEST_YEAR = 1450

#: Open Library will return up to 100 per page; this is a sanity bound on what
#: the caller may ask for, so a typo in --limit cannot turn into a
#: thousand-page crawl of somebody else's service.
MAX_LIMIT = 100

TITLE_MAX = 500


class Book(NamedTuple):
    title: str
    author: str
    publication_year: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate(raw: Any, *, today: int | None = None) -> tuple[Book | None, str]:
    """Turn one API record into a Book, or say why it cannot be one.

    Returns `(book, "")` or `(None, reason)`. Returning the reason rather than
    raising is what lets the caller quarantine the row: an exception here would
    have to be caught and stringified anyway, and the string would be worse.
    """
    if not isinstance(raw, dict):
        return None, f"record is {type(raw).__name__}, not an object"

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "missing or empty title"
    title = " ".join(title.split())
    if len(title) > TITLE_MAX:
        # Truncating silently would store a different book's title than the
        # one the API sent, so this is a rejection rather than a fix.
        return None, f"title is {len(title)} characters, over the {TITLE_MAX} limit"

    authors = raw.get("author_name")
    if not isinstance(authors, list) or not authors:
        return None, "no author listed"
    author = authors[0]
    if not isinstance(author, str) or not author.strip():
        return None, "first author is empty"
    author = " ".join(author.split())

    year = raw.get("first_publish_year")
    if not isinstance(year, int) or isinstance(year, bool):
        # isinstance(True, int) is True in Python, so a bool year would sail
        # through an unguarded int check and store as 1.
        return None, f"publication year is {year!r}, not an integer"
    ceiling = (today or datetime.now(timezone.utc).year) + 1
    if not EARLIEST_YEAR <= year <= ceiling:
        return None, f"publication year {year} is outside {EARLIEST_YEAR}-{ceiling}"

    return Book(title, author, year), ""


def fetch(query: str, limit: int, *, offline: Path | None = None) -> list[Any]:
    """The API call, or a saved copy of one.

    `--offline` exists so the tests, and a reviewer on a train, exercise the
    same validation and storage code as a live run. It reads a file, so it
    never goes through the URL fetcher.
    """
    if offline is not None:
        payload = json.loads(offline.read_text(encoding="utf-8"))
    else:
        payload = http.get_json(
            SEARCH_URL,
            params={"q": query, "fields": FIELDS, "limit": limit},
        )

    if not isinstance(payload, dict):
        raise http.FetchError("search response was not an object")
    docs = payload.get("docs")
    if not isinstance(docs, list):
        raise http.FetchError("search response had no 'docs' list")
    return docs[:limit]


def store(connection: sqlite3.Connection, books: Iterable[Book], source: str) -> int:
    """Upsert books. Returns the number of rows written.

    ON CONFLICT ... DO UPDATE rather than INSERT OR IGNORE: a second run should
    refresh when the row was last seen, which is the difference between "this
    book is in the database" and "this book was still in the API an hour ago".

    The natural key is (title, author, publication_year), and all three are
    NOT NULL. That is not decoration. In SQLite - and in the SQL standard -
    NULL is not equal to NULL, so a UNIQUE index containing a nullable column
    does not constrain the rows where it is null: every re-run would insert
    another copy of every year-less book. Validation rejects records without a
    year partly for that reason.
    """
    now = _now()
    written = 0
    for book in books:
        connection.execute(
            "INSERT INTO books (title, author, publication_year, source, fetched_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (title, author, publication_year)"
            " DO UPDATE SET source = excluded.source, fetched_at = excluded.fetched_at",
            (book.title, book.author, book.publication_year, source, now),
        )
        written += 1
    connection.commit()
    return written


def display(connection: sqlite3.Connection, limit: int) -> str:
    """Read the stored rows back out and lay them out as a table.

    Reading back rather than printing what we just fetched is the point of the
    task: it proves the round trip through the database, and it is the only
    version of this function that would notice a storage bug.
    """
    rows = connection.execute(
        "SELECT title, author, publication_year FROM books"
        " ORDER BY publication_year DESC, title ASC LIMIT ?",
        (limit,),
    ).fetchall()
    if not rows:
        return "(no books stored)"

    title_width = min(60, max([len("TITLE")] + [len(r["title"]) for r in rows]))
    author_width = min(28, max([len("AUTHOR")] + [len(r["author"]) for r in rows]))

    def clip(value: str, width: int) -> str:
        return value if len(value) <= width else value[: width - 3] + "..."

    head = "TITLE".ljust(title_width) + "  " + "AUTHOR".ljust(author_width) + "  YEAR"
    rule = "-" * title_width + "  " + "-" * author_width + "  ----"
    lines = [head, rule]
    for row in rows:
        lines.append(
            clip(row["title"], title_width).ljust(title_width)
            + "  "
            + clip(row["author"], author_width).ljust(author_width)
            + "  "
            + str(row["publication_year"])
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    console.use_utf8()
    parser = argparse.ArgumentParser(description="Fetch books, store them, show them.")
    parser.add_argument("--query", default="security engineering")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    parser.add_argument(
        "--offline",
        type=Path,
        help="read a saved search response instead of calling the API",
    )
    args = parser.parse_args(argv)

    if not 1 <= args.limit <= MAX_LIMIT:
        parser.error("--limit must be between 1 and " + str(MAX_LIMIT))

    started = _now()
    source = str(args.offline) if args.offline else SEARCH_URL
    docs = fetch(args.query, args.limit, offline=args.offline)

    connection = db.connect(args.db)
    try:
        db.ensure_schema(connection)
        db.clear_rejections(connection, source)

        books: list[Book] = []
        rejected = 0
        for index, raw in enumerate(docs):
            book, reason = validate(raw)
            if book is None:
                rejected += 1
                db.reject(connection, source, index, reason, json.dumps(raw)[:2000], _now())
            else:
                books.append(book)

        written = store(connection, books, source)
        db.record_run(connection, "books", source, started, len(docs), written, rejected)

        print(display(connection, args.limit))
        print()
        print(f"{len(docs)} fetched, {written} stored, {rejected} rejected.")
        if rejected:
            print("Rejected rows and their reasons are in the rejected_rows table.")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
