"""Tests for task 3.

Every row in `data/users.csv` is here for a reason, and this file is where the
reason is written down.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from src.common import db
from src.task3_users import (
    User,
    csv_safe,
    export,
    insert,
    looks_like_formula,
    normalise,
    read_csv,
    valid_email,
)

MESSY_CSV = Path(__file__).parents[1] / "data" / "users.csv"


@pytest.fixture()
def connection(tmp_path):
    handle = db.connect(tmp_path / "test.db")
    db.ensure_schema(handle)
    yield handle
    handle.close()


@pytest.mark.parametrize(
    "email",
    [
        "alice@example.com",
        "alice.mathew@example.co.in",
        "a@b.io",
        "first+tag@example.org",
        "under_score@sub.domain.example.com",
    ],
)
def test_real_addresses_pass(email):
    ok, reason = valid_email(email)
    assert ok, reason


@pytest.mark.parametrize(
    "email, expected",
    [
        ("", "empty email"),
        ("no-at-sign.example.com", "expected exactly 1"),
        ("two@at@example.com", "expected exactly 1"),
        ("@example.com", "local part is empty"),
        ("alice@", "domain is empty"),
        ("alice@example", "domain has no dot"),
        ("alice@example.", "domain has an empty label"),
        ("alice@.example.com", "domain has an empty label"),
        ("alice@exa..mple.com", "domain has an empty label"),
        ("alice@-example.com", "malformed"),
        ("alice@example.c", "top-level domain is too short"),
        ("alice bob@example.com", "whitespace"),
        ("a" * 65 + "@example.com", "local part is empty or too long"),
        ("a@" + "b" * 260 + ".com", "over the 254"),
    ],
)
def test_things_that_are_not_addresses_are_rejected(email, expected):
    ok, reason = valid_email(email)
    assert not ok
    assert expected in reason


def test_an_address_is_lower_cased_before_it_becomes_the_key():
    user, reason = normalise("Alice Mathew", "Alice.Mathew@Example.COM")
    assert reason == ""
    assert user == User("Alice Mathew", "alice.mathew@example.com")


def test_a_blank_name_is_rejected():
    user, reason = normalise("   ", "dinesh.verma@example.com")
    assert user is None
    assert reason == "missing name"


def test_whitespace_in_a_name_is_collapsed():
    user, _ = normalise("  Brijesh    Kumar\t", "b@example.com")
    assert user is not None
    assert user.name == "Brijesh Kumar"


def test_the_same_person_in_two_cases_is_one_row(connection):
    # The row this guards is in the fixture CSV: Alice appears as
    # Alice.Mathew@Example.com and again as alice.mathew@example.com.
    insert(connection, [
        User("Alice Mathew", "alice.mathew@example.com"),
        User("alice mathew", "alice.mathew@example.com"),
    ])
    rows = connection.execute("SELECT name FROM users").fetchall()
    assert len(rows) == 1
    # The later row wins, which is what an upsert means and is worth being
    # explicit about rather than discovering later.
    assert rows[0]["name"] == "alice mathew"


def test_importing_the_messy_file_keeps_the_good_and_quarantines_the_rest(connection):
    users, seen, rejected = read_csv(MESSY_CSV, connection)
    assert seen == 12
    # Five bad rows: no-dot domain, blank name, "at" instead of @, an extra
    # column, and a doubled @.
    assert rejected == 5
    assert len(users) == 7

    insert(connection, users)
    # Seven rows read but Alice is in there twice, so six people land.
    count = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert count == 6


def test_every_rejection_records_its_line_and_reason(connection):
    read_csv(MESSY_CSV, connection)
    rows = connection.execute(
        "SELECT line_number, reason FROM rejected_rows ORDER BY line_number"
    ).fetchall()
    reasons = {row["line_number"]: row["reason"] for row in rows}
    # Line numbers are the file's own, counting the header, so a person can
    # open the CSV and go straight to the row.
    assert reasons[5].startswith("domain has no dot")
    assert reasons[6] == "missing name"
    assert "whitespace" in reasons[7]
    assert "more fields than the header" in reasons[10]
    assert "expected exactly 1" in reasons[12]


def test_importing_the_messy_file_twice_is_the_same_as_once(connection):
    users, _, _ = read_csv(MESSY_CSV, connection)
    insert(connection, users)
    first = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    insert(connection, users)
    second = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    assert first == second == 6


def test_a_formula_cell_is_imported_not_dropped(connection):
    users, _, _ = read_csv(MESSY_CSV, connection)
    names = [user.name for user in users]
    assert "=cmd|' /C calc'!A0" in names


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1+1", "\r=1+1"])
def test_formula_prefixes_are_recognised(value):
    assert looks_like_formula(value)


@pytest.mark.parametrize("value", ["Alice", "a=b", "Jean-Luc Picard"])
def test_ordinary_text_is_not_mistaken_for_a_formula(value):
    # "Jean-Luc" contains a hyphen but does not start with one, and "a=b" has
    # an equals sign in the middle. Over-matching here would mangle real names.
    assert not looks_like_formula(value)


def test_the_export_escapes_a_formula_but_keeps_the_value(tmp_path, connection):
    insert(connection, [User("=cmd|' /C calc'!A0", "farhan.sheikh@example.com")])
    destination = tmp_path / "out" / "users.csv"
    assert export(connection, destination) == 1

    raw = destination.read_text(encoding="utf-8")
    # The apostrophe is what stops a spreadsheet evaluating the cell.
    assert "'=cmd" in raw

    with destination.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    # And the value itself is intact: escaping is a display concern, not a
    # licence to alter the data.
    assert rows[0]["name"] == "'=cmd|' /C calc'!A0"
    assert rows[0]["email"] == "farhan.sheikh@example.com"


def test_csv_safe_leaves_normal_values_alone():
    assert csv_safe("Alice Mathew") == "Alice Mathew"
    assert csv_safe("alice@example.com") == "alice@example.com"


def test_a_file_without_the_expected_columns_is_refused(tmp_path, connection):
    bad = tmp_path / "bad.csv"
    bad.write_text("full_name,address\nAlice,alice@example.com\n", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        read_csv(bad, connection)
    assert "missing column" in str(error.value)


def test_a_byte_order_mark_does_not_break_the_header(tmp_path, connection):
    # Excel writes UTF-8 with a BOM by default, which makes the first column
    # read as "﻿name" and every row come back with no name at all.
    marked = tmp_path / "bom.csv"
    marked.write_bytes("name,email\nAlice,alice@example.com\n".encode("utf-8-sig"))
    users, seen, rejected = read_csv(marked, connection)
    assert (seen, rejected) == (1, 0)
    assert users[0].name == "Alice"


def test_a_second_import_replaces_the_previous_rejections(connection):
    # Without this the quarantine table grows another copy of every bad row on
    # every run, and the report prints each rejection once per import ever run.
    read_csv(MESSY_CSV, connection)
    first = connection.execute("SELECT COUNT(*) AS n FROM rejected_rows").fetchone()["n"]
    read_csv(MESSY_CSV, connection)
    second = connection.execute("SELECT COUNT(*) AS n FROM rejected_rows").fetchone()["n"]
    assert first == second == 5
