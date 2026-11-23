"""Save real emails once as the eval test set (SHY-12). Reads Gmail, changes nothing.

Usage:
    python -m evals.export [N] [--force]   # pull the newest N received emails (default 300)

Pulls N emails, then drops near-duplicates: at most MAX_PER_SENDER per sender, and
never two with the same subject apart from numbers. Each kept email records
sender_emails: how many emails its sender sent in the scanned period, capped ones
included. The eval uses it to also score the inbox as it really is, where a
frequent sender counts for more, not only the varied set.

Writes two files to eval_data/, which is gitignored so your emails never reach the repo:
    emails.jsonl   one email per line, as the agent sees it
    answers.csv    the right answer for each email, plus which account it came from;
                   labels are pre-filled from Gmail. Check them with make eval-review.
                   Delete a row to drop that email from the eval.
    labels.json    the account's Gmail label names, for the review page's label picker
    labels.md      (copied by hand) your label descriptions, as on the web app's Labels tab
An existing answers.csv is never overwritten without --force, so corrections aren't lost.
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter
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
DEFAULT_COUNT = 300
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


def gmail_labels(label_ids: list[str], names: dict[str, str]) -> str:
    """The email's user labels as the agent writes them ("Work/Launch; Team"):
    a parent is left out when its sub-label is there."""
    on_email = sorted(names[i] for i in label_ids if i in names)
    return "; ".join(n for n in on_email if not any(o.startswith(n + "/") for o in on_email))


def pick(count: int) -> tuple[list[dict], Counter]:
    """The newest received emails, skipping the user's own (like the agent's
    reminders) and anything over a DOMAIN_CAPS cap. Also returns how many emails
    each sender had among all scanned, capped ones included."""
    me = gmail.my_address().lower()
    names = {label["id"]: label["name"] for label in gmail.list_labels()}
    kept, per_domain, seen, skipped = [], Counter(), Counter(), 0
    for message_id in gmail.search_ids(QUERY):
        summary = gmail.get_summary(message_id)
        if address(summary["sender"]) == me:
            continue
        seen[address(summary["sender"])] += 1
        capped = capped_domain(summary["sender"])
        if capped and per_domain[capped] >= DOMAIN_CAPS[capped]:
            skipped += 1
            continue
        if capped:
            per_domain[capped] += 1
        email = gmail.get_email(message_id)
        email["date"] = summary["date"].isoformat()
        email["account"] = me
        email["labels"] = gmail_labels(gmail.get_label_ids(message_id), names)
        kept.append(email)
        print(f"[{len(kept)}/{count}] {summary['sender'][:40]} | {summary['subject'][:60]}")
        if len(kept) == count:
            break
    print(f"Skipped {skipped} emails over the caps {DOMAIN_CAPS}.")
    return kept, seen


def prune(emails: list[dict], seen: Counter) -> list[dict]:
    """Drop near-duplicates (see the module docstring), and record on each kept
    email how many emails its sender had among all scanned."""
    kept, subjects = [], {}
    for email in emails:
        sender, key = address(email["sender"]), subject_key(email["subject"])
        if len(subjects.setdefault(sender, set())) >= MAX_PER_SENDER or key in subjects[sender]:
            continue
        subjects[sender].add(key)
        kept.append(email)
    for email in kept:
        email["sender_emails"] = seen[address(email["sender"])]
    return kept


def coverage(emails: list[dict]) -> str:
    """How many kept emails carry each pre-filled label, parents and sub-labels together."""
    counts = Counter(
        label.split("/")[0] for email in emails for label in email["labels"].split("; ") if label
    )
    counts["(no label)"] = sum(1 for email in emails if not email["labels"])
    return "\n".join(f"  {n:3}  {label}" for label, n in counts.most_common())


def write(emails: list[dict], label_names: list[str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    LABELS_FILE.write_text(json.dumps(sorted(label_names), ensure_ascii=False, indent=1))
    with EMAILS_FILE.open("w") as f:
        for email in emails:
            agent_view = {k: v for k, v in email.items() if k not in ("labels", "sender_emails", "account")}
            f.write(json.dumps(agent_view, ensure_ascii=False) + "\n")
    with ANSWERS_FILE.open("w", newline="") as f:
        writer = csv.DictWriter(f, ANSWER_COLUMNS)
        writer.writeheader()
        for email in emails:
            writer.writerow({
                "id": email["id"], "account": email["account"], "date": email["date"][:10],
                "sender": email["sender"], "subject": email["subject"], "labels": email["labels"],
                "urgency": "", "needs_reply": "", "checked": "", "sender_emails": email["sender_emails"], "notes": "",
            })


def main() -> None:
    parser = argparse.ArgumentParser(description="Save real emails once as the eval test set.")
    parser.add_argument("count", nargs="?", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--force", action="store_true", help="overwrite an existing answers.csv")
    args = parser.parse_args()
    if ANSWERS_FILE.exists() and not args.force:
        sys.exit(f"{ANSWERS_FILE} already exists and may hold your corrections. Use --force to overwrite it.")
    pulled, seen = pick(args.count)
    emails = prune(pulled, seen)
    write(emails, [label["name"] for label in gmail.list_labels()])
    print(f"\nPulled {len(pulled)}, kept {len(emails)} after dropping near-duplicates "
          f"(max {MAX_PER_SENDER} per sender), from {len(seen)} senders.")
    print(f"Pre-filled labels of the kept emails:\n{coverage(emails)}")
    print(f"\nSaved to {EMAILS_FILE}")
    print("Next: make eval-prefill, then make eval-review to check the answers.")


if __name__ == "__main__":
    main()
