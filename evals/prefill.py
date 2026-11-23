"""Pre-fill the eval answers with the agent's own guesses, so checking them is
correcting, not writing from scratch (SHY-12). Changes nothing in Gmail.

Usage:
    python -m evals.prefill

Fills only empty cells of eval_data/answers.csv: urgency, needs_reply, and labels
for emails without any. Offline: needs no Google login. Cells you already filled or corrected are never touched,
so it is safe to run again. Rows stay unchecked until you confirm them on the
review page (make eval-review).
"""

from concurrent.futures import ThreadPoolExecutor

from triage import pipeline, run_context

from . import review
from .export import ANSWERS_FILE, label_names, load_emails, offline_context

PARALLEL = 8


def guess(email: pipeline.Email, label_names: list[str]) -> dict:
    result = pipeline.classify(email, label_names)
    return {
        "labels": "; ".join(f"{label}/{sub}" if sub else label for label, sub in pipeline.pick_labels(result)),
        "urgency": result.urgency,
        "needs_reply": "yes" if result.needs_reply else "no",
    }


def fill(row: dict, guessed: dict) -> None:
    """Put the guesses into the row's empty cells."""
    for column in ("urgency", "needs_reply", "labels"):
        if not row[column]:
            row[column] = guessed[column]


def main() -> None:
    emails, names, ctx = load_emails(), label_names(), offline_context()
    rows = review.read_rows()
    todo = [r for r in rows if not (r["urgency"] and r["needs_reply"])]
    print(f"Guessing {len(todo)} of {len(rows)} emails with {pipeline.CLASSIFY_MODEL}...")

    def one(row):
        fields = {k: v for k, v in emails[row["id"]].items() if k in pipeline.Email.model_fields}
        try:
            # Each worker thread needs the context set itself; threads don't inherit it.
            with run_context.use(ctx):
                return row, guess(pipeline.Email(**fields), names)
        except Exception as e:
            print(f"[error] {row['id']} {row['subject'][:50]}: {e}")
            return row, None

    done = 0
    with ThreadPoolExecutor(PARALLEL) as pool:
        for row, guessed in pool.map(one, todo):
            if guessed:
                fill(row, guessed)
                done += 1
                print(f"[{done}/{len(todo)}] {guessed['urgency']:13} reply={guessed['needs_reply']:3} | {row['subject'][:50]}")

    review.write_rows(rows)
    print(f"\nPre-filled {done} rows in {ANSWERS_FILE}. Failed rows stay empty; run again to retry them.")


if __name__ == "__main__":
    main()
