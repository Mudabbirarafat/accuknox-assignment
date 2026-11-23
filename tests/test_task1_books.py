"""Tests for task 1.

The validation tests are the point of this file. Each one is a record Open
Library actually returns, and each would be a bug if the code were written
the obvious way.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.common import db
from src.task1_books import Book, display, fetch, store, validate

FIXTURE = Path(__file__).parent / "fixtures" / "openlibrary_search.json"


@pytest.fixture()
def connection(tmp_path):
    handle = db.connect(tmp_path / "test.db")
    db.ensure_schema(handle)
    yield handle
    handle.close()


def test_a_complete_record_validates():
    book, reason = validate(
        {"title": "Security Engineering", "author_name": ["Ross Anderson"],
         "first_publish_year": 2001}
    )
    assert reason == ""
    assert book == Book("Security Engineering", "Ross Anderson", 2001)


def test_whitespace_in_a_title_is_collapsed():
    book, _ = validate(
        {"title": "  Applied   Cryptography\n", "author_name": ["Bruce Schneier"],
         "first_publish_year": 1994}
    )
    assert book is not None
    assert book.title == "Applied Cryptography"


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"author_name": ["X"], "first_publish_year": 2000}, "missing or empty title"),
        ({"title": "   ", "author_name": ["X"], "first_publish_year": 2000},
         "missing or empty title"),
        ({"title": "T", "first_publish_year": 2000}, "no author listed"),
        ({"title": "T", "author_name": [], "first_publish_year": 2000}, "no author listed"),
        ({"title": "T", "author_name": [""], "first_publish_year": 2000},
         "first author is empty"),
        ({"title": "T", "author_name": ["X"]}, "not an integer"),
    ],
)
def test_incomplete_records_are_rejected_with_a_reason(record, expected):
    book, reason = validate(record)
    assert book is None
    assert expected in reason


def test_a_year_in_the_future_is_rejected():
    book, reason = validate(
        {"title": "T", "author_name": ["X"], "first_publish_year": 3099}, today=2026
    )
    assert book is None
    assert "outside" in reason


def test_a_year_that_is_a_string_is_rejected():
    # The one that most often slips through. "1995" is truthy, it is not None,
    # and `int(year)` on it would work - but it sorts and compares as text.
    book, reason = validate(
        {"title": "T", "author_name": ["X"], "first_publish_year": "1995"}
    )
    assert book is None
    assert "not an integer" in reason


def test_a_boolean_year_is_rejected():
    # isinstance(True, int) is True in Python, so an unguarded int check
    # stores this as the year 1.
    book, reason = validate(
        {"title": "T", "author_name": ["X"], "first_publish_year": True}
    )
    assert book is None
    assert "not an integer" in reason


def test_a_record_that_is_not_an_object_is_rejected():
    book, reason = validate(["not", "a", "dict"])
    assert book is None
    assert "not an object" in reason


def test_the_saved_response_yields_three_good_records_and_four_bad():
    docs = fetch("ignored", 10, offline=FIXTURE)
    results = [validate(doc, today=2026) for doc in docs]
    good = [book for book, _ in results if book is not None]
    bad = [reason for book, reason in results if book is None]
    assert len(good) == 3
    assert len(bad) == 4


def test_stored_books_come_back_out(connection):
    books = [Book("Security Engineering", "Ross Anderson", 2001)]
    assert store(connection, books, "test") == 1
    table = display(connection, 10)
    assert "Security Engineering" in table
    assert "Ross Anderson" in table
    assert "2001" in table


def test_importing_twice_does_not_duplicate(connection):
    books = [
        Book("Security Engineering", "Ross Anderson", 2001),
        Book("Practical Malware Analysis", "Michael Sikorski", 2012),
    ]
    store(connection, books, "first")
    store(connection, books, "second")
    count = connection.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]
    assert count == 2


def test_a_second_import_refreshes_the_source(connection):
    books = [Book("Security Engineering", "Ross Anderson", 2001)]
    store(connection, books, "first")
    store(connection, books, "second")
    row = connection.execute("SELECT source FROM books").fetchone()
    assert row["source"] == "second"


def test_an_empty_table_displays_without_crashing(connection):
    assert display(connection, 10) == "(no books stored)"


def test_rejected_rows_keep_their_payload(connection):
    raw = {"title": "", "author_name": ["X"], "first_publish_year": 2000}
    db.reject(connection, "test", 4, "missing or empty title", json.dumps(raw), "now")
    connection.commit()
    row = connection.execute("SELECT line_number, reason, raw FROM rejected_rows").fetchone()
    assert row["line_number"] == 4
    assert row["reason"] == "missing or empty title"
    # The original record is recoverable, which is the difference between a
    # quarantine and a drop.
    assert json.loads(row["raw"]) == raw
