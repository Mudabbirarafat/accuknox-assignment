"""Tests for task 2.

The average is the thing worth testing. It is one number that will be quoted
out of context, so the tests pin down what it is an average *of*.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.common import db
from src.task2_scores import Score, average, chart, fetch, store, validate

FIXTURE = Path(__file__).parents[1] / "data" / "scores.json"


@pytest.fixture()
def connection(tmp_path):
    handle = db.connect(tmp_path / "test.db")
    db.ensure_schema(handle)
    yield handle
    handle.close()


def test_a_complete_record_validates():
    item, reason = validate(
        {"student_id": "S-1", "name": "Aarav Sharma", "subject": "Mathematics", "score": 91}
    )
    assert reason == ""
    assert item == Score("S-1", "Aarav Sharma", "Mathematics", 91.0)


def test_a_score_of_zero_is_a_real_score():
    # The failure this guards against is a falsy check - `if not score` - which
    # treats a zero as missing and quietly raises the class average.
    item, reason = validate({"student_id": "S-1", "name": "A", "score": 0})
    assert reason == ""
    assert item is not None
    assert item.score == 0.0


def test_a_missing_subject_is_allowed():
    item, _ = validate({"student_id": "S-1", "name": "A", "score": 50})
    assert item is not None
    assert item.subject is None


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"name": "A", "score": 50}, "missing student_id"),
        ({"student_id": "  ", "name": "A", "score": 50}, "missing student_id"),
        ({"student_id": "S-1", "score": 50}, "missing name"),
        ({"student_id": "S-1", "name": "A", "score": "83"}, "not a number"),
        ({"student_id": "S-1", "name": "A", "score": None}, "not a number"),
        ({"student_id": "S-1", "name": "A", "score": True}, "not a number"),
        ({"student_id": "S-1", "name": "A", "score": 105}, "outside"),
        ({"student_id": "S-1", "name": "A", "score": -1}, "outside"),
        ({"student_id": "S-1", "name": "A", "score": float("nan")}, "NaN"),
    ],
)
def test_bad_records_are_rejected_with_a_reason(record, expected):
    item, reason = validate(record)
    assert item is None
    assert expected in reason


def test_the_average_reports_how_many_it_averaged():
    scores = [
        Score("1", "A", None, 90.0),
        Score("2", "B", None, 80.0),
        Score("3", "C", None, 70.0),
    ]
    assert average(scores) == (80.0, 3)


def test_the_average_of_nothing_is_not_a_crash():
    # sum([]) / len([]) is a ZeroDivisionError, and this is the path a wholly
    # failed import takes.
    assert average([]) == (0.0, 0)


def test_the_average_ignores_the_records_that_failed_validation():
    records = fetch(offline=FIXTURE)
    good = [item for item, _ in (validate(r) for r in records) if item is not None]
    mean, count = average(good)
    # Twelve records in the file; two are deliberately broken - a score sent
    # as the string "83" and a score of 105 out of 100.
    assert len(records) == 12
    assert count == 10
    assert mean == pytest.approx(69.9, abs=0.05)


def test_the_broken_records_are_the_ones_expected():
    records = fetch(offline=FIXTURE)
    reasons = [reason for item, reason in (validate(r) for r in records) if item is None]
    assert len(reasons) == 2
    assert any("not a number" in reason for reason in reasons)
    assert any("outside" in reason for reason in reasons)


def test_importing_twice_does_not_duplicate(connection):
    scores = [Score("S-1", "A", None, 90.0), Score("S-2", "B", None, 80.0)]
    store(connection, scores)
    store(connection, scores)
    count = connection.execute("SELECT COUNT(*) AS n FROM students").fetchone()["n"]
    assert count == 2


def test_a_re_import_updates_a_changed_score(connection):
    store(connection, [Score("S-1", "A", None, 60.0)])
    store(connection, [Score("S-1", "A", None, 72.0)])
    row = connection.execute("SELECT score FROM students WHERE student_id = 'S-1'").fetchone()
    assert row["score"] == 72.0


def test_the_chart_is_written_and_is_a_png(tmp_path):
    scores = [
        Score("1", "Aarav Sharma", None, 91.0),
        Score("2", "Diya Patel", None, 78.0),
        Score("3", "Arjun Nair", None, 52.0),
    ]
    mean, _ = average(scores)
    destination = chart(scores, mean, tmp_path / "charts" / "scores.png")
    assert destination.is_file()
    # Checking the magic bytes rather than the extension: savefig picks the
    # format from the suffix, and a typo there writes a PDF called .png.
    assert destination.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert destination.stat().st_size > 5000
