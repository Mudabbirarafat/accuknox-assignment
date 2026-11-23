"""Task 2 - fetch student test scores from an API, average them, chart them.

    python -m src.task2_scores --offline data/scores.json
    python -m src.task2_scores            # fetches over HTTPS

A note on the API, because it is a real choice and not an oversight. There is
no stable public "student test scores" API; the ones that turn up in search
results are either demo endpoints that return random data on every call or
they have stopped existing. Either makes a chart that cannot be reproduced,
which defeats the point of charting it. So the scores are a JSON document in
this repository, served over HTTPS by GitHub raw, and `--offline` reads the
same file from disk. The fetch is a real HTTP request against a real remote
URL - it goes through the same capped, timed, scheme-checked fetcher as task
1 - and the numbers do not move under the chart between runs.

The averaging is where the care is. An average is one number standing in for
a whole distribution, so:

* a score of 0 is a real score and a missing score is not a 0;
* the mean is computed over the scores that passed validation, and the chart
  says how many that was, because "average 84" over 3 of 12 students is a
  different claim from "average 84" over all 12;
* dividing by a count that can be zero is checked rather than caught.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, NamedTuple

import matplotlib

# Chosen before pyplot is imported. Without it matplotlib tries to find a GUI
# toolkit, which on a build server means either a warning or a hang, and this
# script never shows a window - it writes a file.
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  (import order is deliberate)

from src.common import console, db, http  # noqa: E402

SCORES_URL = (
    "https://raw.githubusercontent.com/Mudabbirarafat/accuknox-assignment/main/data/scores.json"
)

#: The marks are out of 100. A negative score or one over the maximum is a
#: data error, and letting it through would move the average in a direction
#: nothing in the real world could have moved it.
MIN_SCORE = 0.0
MAX_SCORE = 100.0


class Score(NamedTuple):
    student_id: str
    name: str
    subject: str | None
    score: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate(raw: Any) -> tuple[Score | None, str]:
    """One record in, a Score or a reason out."""
    if not isinstance(raw, dict):
        return None, f"record is {type(raw).__name__}, not an object"

    student_id = raw.get("student_id")
    if not isinstance(student_id, str) or not student_id.strip():
        return None, "missing student_id"
    student_id = student_id.strip()

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, f"{student_id}: missing name"
    name = " ".join(name.split())

    subject = raw.get("subject")
    if subject is not None and not isinstance(subject, str):
        return None, f"{student_id}: subject is {type(subject).__name__}, not a string"

    score = raw.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        # A string score is the common shape of this bug - "88" sorts and
        # averages nothing like 88 - and bool is an int in Python, so True
        # would otherwise become a score of 1.
        return None, f"{student_id}: score is {score!r}, not a number"
    score = float(score)
    if score != score:  # NaN, which compares false against everything
        return None, f"{student_id}: score is NaN"
    if not MIN_SCORE <= score <= MAX_SCORE:
        return None, f"{student_id}: score {score} is outside {MIN_SCORE}-{MAX_SCORE}"

    return Score(student_id, name, subject, score), ""


def fetch(*, offline: Path | None = None) -> list[Any]:
    if offline is not None:
        payload = json.loads(offline.read_text(encoding="utf-8"))
    else:
        payload = http.get_json(SCORES_URL)

    # Accepting both a bare list and {"scores": [...]} because both are
    # reasonable API shapes and the caller should not have to care.
    if isinstance(payload, dict):
        payload = payload.get("scores")
    if not isinstance(payload, list):
        raise http.FetchError("scores response was not a list")
    return payload


def average(scores: Iterable[Score]) -> tuple[float, int]:
    """The mean, and the number of scores it is a mean of.

    Returning the count alongside the value is the whole point. A caller that
    only gets the number cannot tell a real average from an average over two
    survivors of a broken import, and will print it either way.
    """
    values = [item.score for item in scores]
    if not values:
        return 0.0, 0
    return sum(values) / len(values), len(values)


def store(connection: sqlite3.Connection, scores: Iterable[Score]) -> int:
    now = _now()
    written = 0
    for item in scores:
        connection.execute(
            "INSERT INTO students (student_id, name, subject, score, fetched_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (student_id)"
            " DO UPDATE SET name = excluded.name, subject = excluded.subject,"
            " score = excluded.score, fetched_at = excluded.fetched_at",
            (item.student_id, item.name, item.subject, item.score, now),
        )
        written += 1
    connection.commit()
    return written


def chart(scores: list[Score], mean: float, destination: Path) -> Path:
    """Draw the bar chart and write it to `destination`.

    Design choices worth defending, since a chart is an argument:

    * The y axis starts at 0. A bar chart whose axis starts at 60 makes a
      four-mark gap look like a chasm, which is the most common way a chart
      tells a lie its own data does not support.
    * The mean is a line across the bars, not a thirteenth bar. It is a
      different kind of quantity from a student's score and should not look
      like one.
    * Bars below the mean are drawn in a second colour. That is the one
      comparison anybody actually wants from this chart, and colour answers
      it faster than reading twelve numbers off an axis.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(scores, key=lambda item: item.score, reverse=True)
    names = [item.name.split()[0] for item in ordered]
    values = [item.score for item in ordered]

    above = "#1f6f8b"
    below = "#c05746"
    colours = [above if value >= mean else below for value in values]

    figure, axes = plt.subplots(figsize=(max(7.0, len(values) * 0.75), 4.6))
    bars = axes.bar(names, values, color=colours, width=0.62)

    axes.axhline(mean, color="#2b2b2b", linestyle="--", linewidth=1.2)
    axes.annotate(
        f"mean {mean:.1f} (n={len(values)})",
        xy=(len(values) - 0.45, mean),
        xytext=(0, 6),
        textcoords="offset points",
        ha="right",
        fontsize=9,
    )

    for bar, value in zip(bars, values):
        axes.annotate(
            f"{value:g}",
            xy=(bar.get_x() + bar.get_width() / 2, value),
            xytext=(0, 3),
            textcoords="offset points",
            ha="center",
            fontsize=8,
        )

    axes.set_ylim(0, MAX_SCORE)
    axes.set_ylabel("Score (out of 100)")
    axes.set_title("Student test scores")
    axes.spines["top"].set_visible(False)
    axes.spines["right"].set_visible(False)
    axes.grid(axis="y", linewidth=0.4, alpha=0.4)
    axes.set_axisbelow(True)
    figure.tight_layout()
    figure.savefig(destination, dpi=150)
    # Closing explicitly: pyplot keeps every figure alive until told
    # otherwise, so a loop that charts without closing leaks until it dies.
    plt.close(figure)
    return destination


def main(argv: list[str] | None = None) -> int:
    console.use_utf8()
    parser = argparse.ArgumentParser(description="Fetch scores, average them, chart them.")
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    parser.add_argument("--offline", type=Path, help="read scores from a local file")
    parser.add_argument("--out", type=Path, default=Path("output/scores.png"))
    args = parser.parse_args(argv)

    started = _now()
    source = str(args.offline) if args.offline else SCORES_URL
    records = fetch(offline=args.offline)

    connection = db.connect(args.db)
    try:
        db.ensure_schema(connection)
        db.clear_rejections(connection, source)

        scores: list[Score] = []
        rejected = 0
        for index, raw in enumerate(records):
            item, reason = validate(raw)
            if item is None:
                rejected += 1
                print(f"  rejected record {index}: {reason}")
                db.reject(connection, source, index, reason, json.dumps(raw)[:2000], _now())
            else:
                scores.append(item)

        written = store(connection, scores)
        db.record_run(connection, "scores", source, started, len(records), written, rejected)

        mean, count = average(scores)
        if count == 0:
            print("No valid scores: nothing to average and nothing to chart.")
            return 1

        print(f"{len(records)} fetched, {written} stored, {rejected} rejected.")
        print(f"Average score: {mean:.2f} over {count} students.")
        print(f"Highest: {max(scores, key=lambda s: s.score).name}"
              f" ({max(s.score for s in scores):g})")
        print(f"Lowest:  {min(scores, key=lambda s: s.score).name}"
              f" ({min(s.score for s in scores):g})")

        written_to = chart(scores, mean, args.out)
        print(f"Chart written to {written_to}")
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
