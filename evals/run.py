"""Score the classifier against your checked answers, per model (SHY-12).

Usage:
    python -m evals.run                    # every model in MODELS
    python -m evals.run --model haiku      # one model: haiku, sonnet, opus, or a full model id
    python -m evals.run --limit 20         # only the first 20 emails, for a quick check

Offline: reads eval_data/ and needs only ANTHROPIC_API_KEY, so Gmail and Calendar
are never touched. Scores only rows marked done on the review page. Each model's
predictions go to eval_runs/<time>-<model>.jsonl, and each run's scores are added
to eval_runs/scores.csv, so later prompt changes can be compared with this one.

Sub-labels and reply drafts are free text, so they aren't scored. Each model
drafts a reply to every email you marked as needing one, and each run writes a
page with every model's sub-labels and drafts side by side (evals/compare.py)
to judge by eye.
"""

import argparse
import csv
import json
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import get_usage_metadata_callback

from triage import pipeline, run_context

from . import compare, review
from .prices import dollars
from .export import address, label_names, load_emails, offline_context

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
PARALLEL = 8
RUNS_DIR = Path(__file__).parent.parent / "eval_runs"
SCORES_FILE = RUNS_DIR / "scores.csv"
URGENT = {"schedule_time", "instant"}


def top_labels(text: str) -> set[str]:
    """"Work/Launch; Team" -> {"Work", "Team"}: the labels without their sub-labels."""
    return {name.strip().split("/")[0] for name in text.split(";") if name.strip()}


def weights(rows: list[dict]) -> dict[str, float]:
    """How many real emails each row stands for: its sender's emails in the scanned
    period, shared among that sender's rows. 1 when the count is missing."""
    rows_per_sender = Counter(address(row["sender"]) for row in rows)
    return {
        row["id"]: float(row.get("sender_emails") or 1) / rows_per_sender[address(row["sender"])]
        for row in rows
    }


def measured(call, ctx) -> tuple[object, dict]:
    """Run one Claude call in ctx; returns its result plus tokens and seconds."""
    start = time.monotonic()
    with run_context.use(ctx), get_usage_metadata_callback() as usage:
        result = call()
    tokens = Counter()
    for counts in usage.usage_metadata.values():
        tokens.update(input=counts.get("input_tokens", 0), output=counts.get("output_tokens", 0))
    return result, {"seconds": round(time.monotonic() - start, 2),
                    "input_tokens": tokens["input"], "output_tokens": tokens["output"]}


def as_email(email: dict) -> pipeline.Email:
    return pipeline.Email(**{k: v for k, v in email.items() if k in pipeline.Email.model_fields})


def predict(email: dict, names: list[str], model: str, ctx) -> tuple[dict, object]:
    """One classification, with its tokens and seconds, and the raw result for drafting."""
    result, usage = measured(lambda: pipeline.classify(as_email(email), names, model=model), ctx)
    labels = "; ".join(f"{label}/{sub}" if sub else label for label, sub in pipeline.pick_labels(result))
    guess = {"labels": labels, "urgency": result.urgency,
             "needs_reply": "yes" if result.needs_reply else "no", **usage}
    return guess, result


def draft(email: dict, result, model: str, ctx) -> dict:
    """The reply the write-reply skill drafts, given the model's own classification.
    Already done lists only the labels: the eval blocks no calendar time."""
    done = [f"apply_label -> labelled {label}" + (f" and {label}/{sub}" if sub else "")
            for label, sub in pipeline.pick_labels(result)]
    reply, usage = measured(lambda: pipeline.write_reply(as_email(email), result, done, model=model), ctx)
    return {"model": model, "body": reply.body, "reply_all": reply.reply_all,
            "reply_all_reason": reply.reply_all_reason, **usage}


def grade(row: dict, guess: dict | None) -> dict:
    """Which parts of the guess match the checked answer. No guess (an error) matches nothing."""
    if guess is None:
        return {"labels": False, "urgency": False, "reply": False, "all": False}
    marks = {
        "labels": top_labels(row["labels"]) == top_labels(guess["labels"]),
        "urgency": row["urgency"] == guess["urgency"],
        "reply": row["needs_reply"] == guess["needs_reply"],
    }
    marks["all"] = marks["labels"] and marks["urgency"] and marks["reply"]
    return marks


def share(rows: list[dict], marks: dict[str, dict], key: str, weight: dict | None = None) -> float | None:
    """Percentage of rows where marks[key] is true, weighted if a weight is given."""
    if not rows:
        return None
    w = weight or {}
    total = sum(w.get(r["id"], 1) for r in rows)
    return 100 * sum(w.get(r["id"], 1) for r in rows if marks[r["id"]][key]) / total


def score(model: str, rows: list[dict], guesses: dict, weight: dict) -> dict:
    marks = {row["id"]: grade(row, guesses.get(row["id"])) for row in rows}
    ok = [g for g in guesses.values() if g]
    spent = sum(dollars(model, g["input_tokens"], g["output_tokens"]) for g in ok)
    drafts = [g["draft"] for g in ok if (g.get("draft") or {}).get("body") is not None]
    draft_spent = sum(dollars(d["model"], d["input_tokens"], d["output_tokens"]) for d in drafts)
    urgent = [r for r in rows if r["urgency"] in URGENT]
    replies = [r for r in rows if r["needs_reply"] == "yes"]
    return {
        "model": model,
        "emails": len(rows),
        "all_right": share(rows, marks, "all"),
        "all_right_real_inbox": share(rows, marks, "all", weight),
        "labels": share(rows, marks, "labels"),
        "urgency": share(rows, marks, "urgency"),
        "urgent_caught": share(urgent, marks, "urgency"),
        "reply": share(rows, marks, "reply"),
        "replies_caught": share(replies, marks, "reply"),
        "dollars_per_email": spent / len(ok) if ok else None,
        "emails_per_dollar": len(ok) / spent if spent else None,
        "dollars_per_draft": draft_spent / len(drafts) if drafts else None,
        "seconds_per_email": sum(g["seconds"] for g in ok) / len(ok) if ok else None,
        "errors": len(rows) - len(ok),
    }


def run_model(model: str, rows: list[dict], emails: dict, names: list[str], draft_model: str | None) -> dict:
    """Classify every row with one model, and draft a reply with draft_model (None:
    no drafts) to each row you marked as needing one. Returns {id: guess or None}."""
    ctx = offline_context()

    def one(row):
        try:
            guess, result = predict(emails[row["id"]], names, model, ctx)
        except Exception as e:
            print(f"  [error] {row['subject'][:50]}: {e}")
            return row["id"], None
        if draft_model and row["needs_reply"] == "yes":
            try:
                guess["draft"] = draft(emails[row["id"]], result, draft_model, ctx)
            except Exception as e:
                print(f"  [draft error] {row['subject'][:50]}: {e}")
                guess["draft"] = {"model": draft_model, "error": str(e)}
        return row["id"], guess

    guesses = {}
    with ThreadPoolExecutor(PARALLEL) as pool:
        for i, (row_id, guess) in enumerate(pool.map(one, rows), 1):
            guesses[row_id] = guess
            print(f"\r  {model}: {i}/{len(rows)}", end="", flush=True)
    print()
    return guesses


def save_predictions(model: str, stamp: str, rows: list[dict], guesses: dict) -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"{stamp}-{model}.jsonl"
    with path.open("w") as f:
        for row in rows:
            guess = guesses.get(row["id"])
            f.write(json.dumps({
                "id": row["id"], "subject": row["subject"], "sender": row["sender"],
                "expected": {k: row[k] for k in ("labels", "urgency", "needs_reply")},
                "predicted": guess, "correct": grade(row, guess),
            }, ensure_ascii=False) + "\n")
    return path


def save_scores(stamp: str, results: list[dict]) -> None:
    """Add this run's scores to scores.csv, with the commit they came from. When
    the metrics change, the file is rewritten with old and new columns, so earlier
    rows keep their numbers and every value stays under its own heading."""
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "triage"], capture_output=True, text=True).stdout.strip()
    new = [{"run": stamp, "commit": commit + ("+changes" if dirty else ""), **r} for r in results]
    old, columns = [], list(new[0])
    if SCORES_FILE.exists():
        with SCORES_FILE.open(newline="") as f:
            reader = csv.DictReader(f)
            old = list(reader)
            columns = list(reader.fieldnames or []) + [c for c in columns if c not in (reader.fieldnames or [])]
    RUNS_DIR.mkdir(exist_ok=True)
    with SCORES_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, columns, restval="")
        writer.writeheader()
        writer.writerows(old + new)


COLUMNS = [
    ("model", "Model", "{}"),
    ("all_right", "All right", "{:.0f}%"),
    ("all_right_real_inbox", "Real inbox", "{:.0f}%"),
    ("labels", "Labels", "{:.0f}%"),
    ("urgency", "Urgency", "{:.0f}%"),
    ("urgent_caught", "Urgent caught", "{:.0f}%"),
    ("reply", "Reply", "{:.0f}%"),
    ("replies_caught", "Replies caught", "{:.0f}%"),
    ("dollars_per_email", "$/email", "{:.4f}"),
    ("emails_per_dollar", "Emails/$", "{:,.0f}"),
    ("dollars_per_draft", "$/draft", "{:.4f}"),
    ("seconds_per_email", "s/email", "{:.1f}"),
    ("errors", "Errors", "{}"),
]


def table(results: list[dict]) -> str:
    cells = [[title for _, title, _ in COLUMNS]] + [
        ["–" if r[key] is None else fmt.format(r[key]) for key, _, fmt in COLUMNS] for r in results
    ]
    widths = [max(len(row[i]) for row in cells) for i in range(len(COLUMNS))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in cells]
    return "\n".join([lines[0], "  ".join("-" * w for w in widths), *lines[1:]])


def main() -> None:
    parser = argparse.ArgumentParser(description="Score models against the checked eval answers.")
    parser.add_argument("--model", action="append", help="haiku, sonnet, opus or a model id; repeat for several")
    parser.add_argument("--limit", type=int, help="score only the first N checked emails")
    parser.add_argument("--draft-model", help="draft with this model instead of each --model (e.g. sonnet)")
    parser.add_argument("--no-drafts", action="store_true", help="classify only, draft no replies")
    args = parser.parse_args()
    models = [MODELS.get(m, m) for m in (args.model or MODELS)]

    all_rows = review.read_rows()
    rows = [r for r in all_rows if r["checked"] == "yes"][: args.limit]
    if not rows:
        raise SystemExit("No checked answers yet. Check some with make eval-review first.")
    emails, names, weight = load_emails(), label_names(), weights(all_rows)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    replies = sum(r["needs_reply"] == "yes" for r in rows)
    print(f"Scoring {len(rows)} checked emails ({len(all_rows) - len(rows)} skipped) with {', '.join(models)}"
          + ("" if args.no_drafts else f"; drafting {replies} replies each"))

    results = []
    for model in models:
        draft_model = None if args.no_drafts else MODELS.get(args.draft_model, args.draft_model) or model
        guesses = run_model(model, rows, emails, names, draft_model)
        save_predictions(model, stamp, rows, guesses)
        results.append(score(model, rows, guesses, weight))

    save_scores(stamp, results)
    page = compare.write_page(stamp)
    print("\n" + table(results))
    print(f"""
All right       labels, urgency and needs_reply all match your answer
Real inbox      the same, weighted so frequent senders count as often as they email you
Urgent caught   only emails you marked schedule_time or instant
Replies caught  only emails you marked as needing a reply
Emails/$        how many emails one dollar classifies
$/draft         average cost of one reply draft

Sub-labels and drafts to compare by eye: {page}
Predictions: {RUNS_DIR}/{stamp}-<model>.jsonl   History: {SCORES_FILE}""")


if __name__ == "__main__":
    main()
