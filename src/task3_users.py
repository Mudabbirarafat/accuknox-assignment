"""Task 3 - read user information from a CSV and insert it into SQLite.

    python -m src.task3_users --csv data/users.csv
    python -m src.task3_users --csv data/users.csv --export output/users_export.csv

`data/users.csv` is deliberately messy. It contains a duplicate email in
different case, an address with no domain, a blank name, a row with a missing
column, and a cell beginning with `=`. Those are not padding - each one is a
bug this script would have if it were the four-line version of itself, and
the test suite asserts on every one of them.

Three decisions in here are worth reading the code for.

**A rejected row is kept.** Every row that fails validation goes into
`rejected_rows` with its line number, the reason, and the original text. An
import that drops what it cannot parse reports success while losing data,
and the loss is only discovered when somebody asks where a user went.

**Email is the identity, so it is normalised before it is the key.**
`Alice@Example.com` and `alice@example.com` are one person. A UNIQUE index on
the raw string would say otherwise and let both in, after which every count
of users is wrong. The domain is lower-cased because DNS is case-insensitive;
strictly the local part is not, but no mail provider in practice treats two
addresses differing only in case as two people, and treating them as one is
the behaviour that matches what users expect.

**Formula injection is handled at the right end.** A cell holding
`=cmd|'/c calc'!A0` is inert in SQLite - it is a string, and the query is
parameterised, so it cannot be anything else. It becomes dangerous when it is
written back out to a CSV and somebody opens that file in Excel, which reads
a leading `=` as a formula (CWE-1236). So it is flagged on the way in and
escaped on the way out, in the exporter, which is the only place the danger
actually exists. Rejecting such rows on import would be the wrong fix in the
wrong place: it loses data to protect a program this script does not run.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, NamedTuple

from src.common import console, db

#: RFC 5321's limit on a forward path. Anything longer is not deliverable.
EMAIL_MAX = 254
LOCAL_MAX = 64
NAME_MAX = 200

#: The characters a spreadsheet treats as the start of a formula. Tab and
#: carriage return are in the list because Excel strips leading whitespace
#: before deciding, so a cell starting with a tab then `=` is still a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class User(NamedTuple):
    name: str
    email: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def looks_like_formula(value: str) -> bool:
    return value.startswith(FORMULA_PREFIXES)


def csv_safe(value: str) -> str:
    """Make a cell safe to open in a spreadsheet.

    Prefixing with an apostrophe is the standard mitigation: Excel and
    LibreOffice both read it as "this is text", display the value without it,
    and never evaluate it. The data is not altered in the database - only the
    exported representation is.
    """
    if looks_like_formula(value):
        return "'" + value
    return value


def valid_email(email: str) -> tuple[bool, str]:
    """A practical email check, and the reason when it fails.

    This is not RFC 5322. A regex that implements RFC 5322 correctly is
    several hundred characters long, accepts addresses no mail server will
    deliver to, and is a liability of its own - the well known ones are
    vulnerable to catastrophic backtracking, which turns a validation
    function into a denial of service. The real test of an address is sending
    mail to it. What this does is reject the things that are certainly not
    addresses, cheaply and in linear time.
    """
    if not email:
        return False, "empty email"
    if len(email) > EMAIL_MAX:
        return False, f"email is {len(email)} characters, over the {EMAIL_MAX} limit"
    if any(character.isspace() for character in email):
        return False, "email contains whitespace"
    if any(ord(character) < 32 or ord(character) == 127 for character in email):
        return False, "email contains a control character"
    if email.count("@") != 1:
        return False, f"email has {email.count('@')} @ signs, expected exactly 1"

    local, domain = email.split("@")
    if not local or len(local) > LOCAL_MAX:
        return False, "local part is empty or too long"
    if not domain:
        return False, "domain is empty"
    if "." not in domain:
        return False, "domain has no dot"
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return False, "domain has an empty label"
    for label in domain.split("."):
        if not label or label.startswith("-") or label.endswith("-"):
            return False, f"domain label {label!r} is malformed"
        if not all(character.isalnum() or character == "-" for character in label):
            return False, f"domain label {label!r} has an illegal character"
    if len(domain.rsplit(".", 1)[1]) < 2:
        return False, "top-level domain is too short"

    return True, ""


def normalise(raw_name: str | None, raw_email: str | None) -> tuple[User | None, str]:
    """Clean one row into a User, or say why it is not one."""
    name = " ".join((raw_name or "").split())
    if not name:
        return None, "missing name"
    if len(name) > NAME_MAX:
        return None, f"name is {len(name)} characters, over the {NAME_MAX} limit"
    if any(ord(character) < 32 for character in name):
        return None, "name contains a control character"

    email = (raw_email or "").strip()
    # Lower-cased before the uniqueness check, not after: see the module
    # docstring. The domain is the part DNS guarantees is case-insensitive,
    # and treating the whole address that way is what matches user
    # expectations everywhere it matters.
    email = email.lower()
    ok, reason = valid_email(email)
    if not ok:
        return None, reason

    return User(name, email), ""


def insert(connection: sqlite3.Connection, users: Iterable[User]) -> int:
    """Upsert users on their email. Returns rows written."""
    now = _now()
    written = 0
    for user in users:
        connection.execute(
            "INSERT INTO users (email, name, imported_at) VALUES (?, ?, ?)"
            " ON CONFLICT (email)"
            " DO UPDATE SET name = excluded.name, imported_at = excluded.imported_at",
            (user.email, user.name, now),
        )
        written += 1
    connection.commit()
    return written


def read_csv(path: Path, connection: sqlite3.Connection) -> tuple[list[User], int, int]:
    """Read the CSV, validating as it goes.

    Returns (users, rows seen, rows rejected). Rejections are written to
    `rejected_rows` as they are found, so a crash halfway through still
    leaves a record of what had been examined.
    """
    users: list[User] = []
    seen = 0
    rejected = 0
    source = str(path)
    db.clear_rejections(connection, source)
    # newline="" is required by the csv module: without it, a quoted field
    # containing a newline is split across two records on Windows.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} is empty")
        missing = {"name", "email"} - {
            (field or "").strip().lower() for field in reader.fieldnames
        }
        if missing:
            raise ValueError(f"{path} is missing column(s): {', '.join(sorted(missing))}")

        for row in reader:
            seen += 1
            line = reader.line_num
            # A short row leaves later columns as None; a long one puts the
            # overflow under the None key. Both are data errors worth naming.
            if None in row and row[None]:
                rejected += 1
                db.reject(connection, source, line, "row has more fields than the header",
                          str(row)[:2000], _now())
                continue

            lookup = {(key or "").strip().lower(): value for key, value in row.items()
                      if key is not None}
            user, reason = normalise(lookup.get("name"), lookup.get("email"))
            if user is None:
                rejected += 1
                db.reject(connection, source, line, reason, str(row)[:2000], _now())
                continue

            if looks_like_formula(user.name):
                # Noted, not rejected. The row is real data; the hazard is in
                # whatever opens the export, and that is where it is handled.
                print(f"  note: line {line} starts with a formula character;"
                      f" it will be escaped on export")

            users.append(user)

    connection.commit()
    return users, seen, rejected


def export(connection: sqlite3.Connection, destination: Path) -> int:
    """Write the stored users back out as a CSV, spreadsheet-safe."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        "SELECT name, email, imported_at FROM users ORDER BY email"
    ).fetchall()
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "email", "imported_at"])
        for row in rows:
            writer.writerow([
                csv_safe(row["name"]),
                csv_safe(row["email"]),
                row["imported_at"],
            ])
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    console.use_utf8()
    parser = argparse.ArgumentParser(description="Import users from a CSV into SQLite.")
    parser.add_argument("--csv", type=Path, default=Path("data/users.csv"))
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    parser.add_argument("--export", type=Path, help="also write a safe CSV of the table")
    args = parser.parse_args(argv)

    if not args.csv.is_file():
        print(f"No such file: {args.csv}", file=sys.stderr)
        return 2

    started = _now()
    connection = db.connect(args.db)
    try:
        db.ensure_schema(connection)
        users, seen, rejected = read_csv(args.csv, connection)
        written = insert(connection, users)
        db.record_run(connection, "users", str(args.csv), started, seen, written, rejected)

        print(f"{seen} rows read, {written} inserted or updated, {rejected} rejected.")
        total = connection.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        print(f"users table now holds {total} row(s).")

        if rejected:
            print("\nRejected:")
            for row in connection.execute(
                "SELECT line_number, reason FROM rejected_rows"
                " WHERE source = ? ORDER BY line_number",
                (str(args.csv),),
            ):
                print(f"  line {row['line_number']}: {row['reason']}")

        if args.export:
            count = export(connection, args.export)
            print(f"\n{count} row(s) exported to {args.export}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
