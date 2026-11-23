"""How a system's decisions are graded against your checked answers (SHY-12)."""

from collections import Counter
from pathlib import Path

from .export import address

# Short names for --classify, --draft and --agent.
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
RUNS_DIR = Path(__file__).parent.parent / "eval_runs"
URGENT = {"schedule_time", "instant"}


def top_labels(text: str) -> set[str]:
    """"Work/Launch; Team" -> {"Work", "Team"}: the labels without their sub-labels."""
    return {name.strip().split("/")[0] for name in text.split(";") if name.strip()}


def weights(rows: list[dict]) -> dict[str, float]:
    """How many real emails each row stands for: its sender's emails in the scanned
    period, shared equally among that sender's rows. Exports add rows over time, each
    with the count of its own scan; one count per sender (the highest) keeps a later
    row from changing the sender's share. 1 when the count is missing."""
    rows_per_sender = Counter(address(row["sender"]) for row in rows)
    per_sender: dict[str, float] = {}
    for row in rows:
        sender = address(row["sender"])
        per_sender[sender] = max(per_sender.get(sender, 1.0), float(row.get("sender_emails") or 1))
    return {row["id"]: per_sender[address(row["sender"])] / rows_per_sender[address(row["sender"])] for row in rows}


def grade(row: dict, guess: dict | None) -> dict:
    """Which parts of the guess match the checked answer. No guess (an error) matches
    nothing. Sub-labels aren't graded: they have no right answer."""
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
