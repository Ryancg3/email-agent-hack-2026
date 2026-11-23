"""Add emails to the eval dataset, in one step (SHY-12):

1. Pull the newest received emails not yet in the dataset, from Gmail, read-only.
   For variety, at most DOMAIN_CAPS per sender domain and MAX_PER_SENDER per sender
   across the whole dataset, never two from a sender with the same subject apart
   from numbers.
2. Pre-fill each with what the agent decides live (the classify model and your
   label descriptions): labels, urgency, needs reply. Asks before spending.
3. Open the review page on the new emails. They stay "to do", and out of every
   benchmark, until you mark them done there.

Usage:
    python -m evals.export [N]    # add up to N new emails (default 50)

Everything goes to eval_data/, which is gitignored, so your emails never reach the repo:
    emails.jsonl   one email per line, as the agent sees it
    answers.csv    your answer for each email, which account it came from, and
                   sender_emails: how many emails that sender sent in the scan, so
                   the benchmark can weight frequent senders like your real inbox
    labels.json    the account's Gmail label names, refreshed on each export
    labels.md      (copied by hand) your label descriptions, as on the web app's Labels tab
    profile.json   (by hand) {"first_name", "last_name"} to sign drafts with
"""

import argparse
import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from email.utils import parseaddr
from pathlib import Path

from triage import gmail, labels, run_context

DATA_DIR = Path(__file__).parent.parent / "eval_data"
EMAILS_FILE = DATA_DIR / "emails.jsonl"
ANSWERS_FILE = DATA_DIR / "answers.csv"
LABELS_FILE = DATA_DIR / "labels.json"
# The label descriptions the classifier sees during the eval, kept with the test set
# so changes to triage/labels.md (the starter labels for new users) don't move scores.
DESCRIPTIONS_FILE = DATA_DIR / "labels.md"
# {"first_name", "last_name"}: who drafts are signed by in the eval (the CLI default is Jane Doe).
PROFILE_FILE = DATA_DIR / "profile.json"
DEFAULT_COUNT = 50
PARALLEL = 8
# Received mail only: not sent, drafts, chats, spam or trash. Archived emails count.
QUERY = "-in:sent -in:drafts -in:chats -in:spam -in:trash"
# Most emails kept per sender domain, so frequent senders don't dominate the set.
DOMAIN_CAPS = {"github.com": 5, "substack.com": 10}
# Most near-duplicates kept per sender address.
MAX_PER_SENDER = 2
# checked is "yes" once a row's answers were confirmed on the review page.
ANSWER_COLUMNS = ["id", "account", "date", "sender", "subject", "labels", "urgency", "needs_reply",
                  "checked", "sender_emails", "notes"]


def load_emails() -> dict[str, dict]:
    """The saved emails by id, each as the agent sees it plus its date."""
    return {e["id"]: e for e in map(json.loads, EMAILS_FILE.read_text().splitlines())}


def label_names() -> list[str]:
    """The account's Gmail label names, as saved at export."""
    return json.loads(LABELS_FILE.read_text())


def offline_context() -> run_context.RunContext:
    """A run context for classifying and drafting saved emails without Google: no
    credentials, the label descriptions saved with the test set (else
    triage/labels.md), and the name from profile.json for signing drafts."""
    saved = labels.parse(DESCRIPTIONS_FILE.read_text()) if DESCRIPTIONS_FILE.exists() else None
    profile = json.loads(PROFILE_FILE.read_text()) if PROFILE_FILE.exists() else {}
    return run_context.RunContext(None, labels=saved, **profile)


def address(sender: str) -> str:
    return parseaddr(sender)[1].lower()


def domain(sender: str) -> str:
    return address(sender).rsplit("@", 1)[-1]


def subject_key(subject: str) -> str:
    """The subject without numbers, punctuation or case, so "Code 4821" and "code 1734" match."""
    return re.sub(r"[\d\W_]+", " ", subject.lower()).strip()


def capped_domain(sender: str) -> str | None:
    """The DOMAIN_CAPS entry the sender falls under (subdomains too), or None."""
    host = domain(sender)
    return next((d for d in DOMAIN_CAPS if host == d or host.endswith("." + d)), None)


def pick(count: int, dataset: list[dict]) -> tuple[list[dict], Counter]:
    """Up to `count` of the newest received emails that aren't in the dataset yet,
    keeping the variety rules over the dataset and the new emails together. Skips
    the user's own (like the agent's reminders). Also returns how many emails each
    sender had among all scanned."""
    me = gmail.my_address().lower()
    have = {row["id"] for row in dataset}
    per_domain = Counter(capped_domain(row["sender"]) for row in dataset if capped_domain(row["sender"]))
    subjects: dict[str, set] = {}
    for row in dataset:
        subjects.setdefault(address(row["sender"]), set()).add(subject_key(row["subject"]))
    kept, seen = [], Counter()
    for message_id in gmail.search_ids(QUERY):
        if message_id in have:
            continue
        summary = gmail.get_summary(message_id)
        sender = address(summary["sender"])
        if sender == me:
            continue
        seen[sender] += 1
        capped, key = capped_domain(summary["sender"]), subject_key(summary["subject"])
        mine = subjects.setdefault(sender, set())
        if (capped and per_domain[capped] >= DOMAIN_CAPS[capped]) or len(mine) >= MAX_PER_SENDER or key in mine:
            continue
        per_domain[capped] += 1 if capped else 0
        mine.add(key)
        email = gmail.get_email(message_id)
        email["date"] = summary["date"].isoformat()
        email["account"] = me
        kept.append(email)
        print(f"[{len(kept)}/{count}] {summary['sender'][:40]} | {summary['subject'][:60]}")
        if len(kept) == count:
            break
    for email in kept:
        email["sender_emails"] = seen[address(email["sender"])]
    return kept, seen


def agent_guess(email: dict, names: list[str], ctx: run_context.RunContext) -> dict:
    """What the agent decides for this email live: its labels (without sub-labels,
    which have no right answer), urgency and whether a reply is needed."""
    from triage import pipeline

    fields = {k: v for k, v in email.items() if k in pipeline.Email.model_fields}
    with run_context.use(ctx):  # each worker thread sets it itself; threads don't inherit it
        result = pipeline.classify(pipeline.Email(**fields), names)
    tops = list(dict.fromkeys(label for label, _ in pipeline.pick_labels(result)))
    return {"labels": "; ".join(tops), "urgency": result.urgency, "needs_reply": "yes" if result.needs_reply else "no"}


def prefill(emails: list[dict], names: list[str]) -> dict[str, dict]:
    """The agent's guesses by email id; an email that fails is left blank to fill in by hand."""
    ctx = offline_context()

    def one(email):
        try:
            return email["id"], agent_guess(email, names, ctx)
        except Exception as e:
            print(f"[not pre-filled] {email['subject'][:50]}: {e}")
            return email["id"], None

    with ThreadPoolExecutor(PARALLEL) as pool:
        return {email_id: guess for email_id, guess in pool.map(one, emails) if guess}


def prefill_estimate(count: int) -> str:
    from triage import pipeline

    from .prices import dollars
    # About 4,000 tokens in (the label descriptions and the email) and 250 out per email.
    return f"about ${count * dollars(pipeline.CLASSIFY_MODEL, 4000, 250):.2f} with {pipeline.CLASSIFY_MODEL}"


def append(emails: list[dict], guesses: dict[str, dict]) -> None:
    """Add the new emails to emails.jsonl and, as to-do rows, to answers.csv. The
    answers are read again under the lock, so reviews saved meanwhile are kept."""
    from . import review

    DATA_DIR.mkdir(exist_ok=True)
    with review.answers_locked():
        current = review.read_rows() if ANSWERS_FILE.exists() else []
        have = {row["id"] for row in current}
        new = [e for e in emails if e["id"] not in have]
        with EMAILS_FILE.open("a") as f:
            for email in new:
                agent_view = {k: v for k, v in email.items() if k not in ("sender_emails", "account")}
                f.write(json.dumps(agent_view, ensure_ascii=False) + "\n")
        review.write_rows(current + [{
            "id": e["id"], "account": e["account"], "date": e["date"][:10], "sender": e["sender"],
            "subject": e["subject"], "checked": "", "sender_emails": e["sender_emails"], "notes": "",
            **(guesses.get(e["id"]) or {"labels": "", "urgency": "", "needs_reply": ""}),
        } for e in new])


def main() -> None:
    from . import review

    parser = argparse.ArgumentParser(description="Add emails to the eval dataset: pull, pre-fill, review.")
    parser.add_argument("count", nargs="?", type=int, default=DEFAULT_COUNT, help="new emails to add (default 50)")
    parser.add_argument("--yes", action="store_true", help="pre-fill without asking")
    args = parser.parse_args()

    dataset = review.read_rows() if ANSWERS_FILE.exists() else []
    print(f"Dataset: {len(dataset)} emails. Pulling up to {args.count} new ones from Gmail (read-only)...")
    emails, seen = pick(args.count, dataset)
    if not emails:
        raise SystemExit("No new emails that fit the variety rules. Nothing added.")
    names = [label["name"] for label in gmail.list_labels()]
    DATA_DIR.mkdir(exist_ok=True)
    LABELS_FILE.write_text(json.dumps(sorted(names), ensure_ascii=False, indent=1))

    question = f"Pre-fill {len(emails)} emails with the agent's decisions, {prefill_estimate(len(emails))}? [y/N] "
    guesses = prefill(emails, names) if args.yes or input(question).strip().lower().startswith("y") else {}
    append(emails, guesses)
    print(f"\nAdded {len(emails)} emails ({len(guesses)} pre-filled) from {len(seen)} senders. "
          f"They stay out of the benchmark until you mark them done.")
    review.serve(to_do=True)


if __name__ == "__main__":
    main()
