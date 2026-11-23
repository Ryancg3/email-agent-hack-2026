"""What the agent would cost per month: email volume times cost per email (SHY-12).

Usage:
    python -m evals.monthly [--months 6]

Volume: counts the emails you received per calendar month in Gmail (ids only;
free, changes nothing). It is an upper bound: the live agent handles only emails
that land in the inbox, so mail your filters move past the inbox costs nothing.

Cost per email: only from measurements already saved in eval_runs/, never guessed.
    Classify     every email     average cost in the newest full eval run of the classify model
    Draft        some emails     average cost of saved drafts by the draft model, times the share
                                 of emails the classifier says need a reply (real-inbox weighted)
    Agent loop   every email     average from eval-trace runs, split by needs reply or not
A step without measurements is shown as unknown, with the command that measures it.
Makes no model calls.
"""

import argparse
import json
from datetime import datetime, timedelta

from triage import agent, gmail, pipeline

from . import review
from .export import QUERY
from .prices import dollars
from .run import RUNS_DIR, weights

# The agent's own reminders are sent from the account to itself; it skips them.
VOLUME_QUERY = QUERY + " -from:me"
# A run counts as full when it scored at least this share of the checked emails.
FULL_RUN = 0.9
VOLUME_FILE = RUNS_DIR / "volume.json"


# ---------- Volume ----------

def month_starts(now: datetime, months: int) -> list[datetime]:
    """The first moment of each of the last `months` full months, then of this month."""
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    starts = [first]
    for _ in range(months):
        starts.insert(0, (starts[0] - timedelta(days=1)).replace(day=1))
    return starts


def count(start: datetime, end: datetime) -> int:
    # Epoch seconds: plain dates in a Gmail search are read in Pacific time.
    query = f"{VOLUME_QUERY} after:{int(start.timestamp())} before:{int(end.timestamp())}"
    return sum(1 for _ in gmail.search_ids(query, page_size=500))


def volume(months: int, now: datetime | None = None) -> tuple[list[tuple[str, int]], tuple[str, int, float]]:
    """[(month, emails)] for the full months, and (this month, emails so far, share of it gone)."""
    now = now or datetime.now().astimezone()
    starts = month_starts(now, months)
    full = [(f"{s:%b %Y}", count(s, e)) for s, e in zip(starts, starts[1:])]
    this = starts[-1]
    next_month = (this + timedelta(days=32)).replace(day=1)
    gone = (now - this) / (next_month - this)
    return full, (f"{this:%b %Y}", count(this, now), gone)


# ---------- Cost per email ----------

def prediction_runs(model: str) -> list[list[dict]]:
    """Saved predictions of one classify model, newest first."""
    paths = sorted(RUNS_DIR.glob(f"2*-{model}.jsonl"), reverse=True)
    return [[json.loads(line) for line in p.read_text().splitlines()] for p in paths]


def classify_cost(rows: list[dict]) -> tuple[float | None, str]:
    checked = sum(r["checked"] == "yes" for r in rows)
    for records in prediction_runs(pipeline.CLASSIFY_MODEL):
        guesses = [r["predicted"] for r in records if r.get("predicted")]
        if len(records) >= FULL_RUN * checked and guesses:
            spent = sum(dollars(pipeline.CLASSIFY_MODEL, g["input_tokens"], g["output_tokens"]) for g in guesses)
            return spent / len(guesses), f"measured on {len(guesses)} emails"
    return None, f"unknown: run make eval MODEL={pipeline.CLASSIFY_MODEL} (about $0.12)"


def reply_share(rows: list[dict]) -> tuple[float | None, str]:
    """Share of real mail the classifier says needs a reply, weighted so frequent
    senders count as often as they email you (the test set over-represents rare senders)."""
    checked = sum(r["checked"] == "yes" for r in rows)
    weight = weights(rows)
    for records in prediction_runs(pipeline.CLASSIFY_MODEL):
        scored = [r for r in records if r.get("predicted") and r["id"] in weight]
        if len(records) >= FULL_RUN * checked and scored:
            total = sum(weight[r["id"]] for r in scored)
            yes = sum(weight[r["id"]] for r in scored if r["predicted"]["needs_reply"] == "yes")
            return yes / total, f"{pipeline.CLASSIFY_MODEL} said a reply is needed"
    return None, "unknown: needs a full eval run of the classify model"


def draft_cost() -> tuple[float | None, str]:
    """Average cost of the saved drafts by the live draft model, from the newest run that has any."""
    for path in sorted(RUNS_DIR.glob("2*-*.jsonl"), reverse=True):
        drafts = [d for d in ((json.loads(line).get("predicted") or {}).get("draft") for line in path.read_text().splitlines())
                  if d and d.get("model") == agent.MODEL and d.get("body") is not None]
        if drafts:
            spent = sum(dollars(d["model"], d["input_tokens"], d["output_tokens"]) for d in drafts)
            return spent / len(drafts), f"measured on {len(drafts)} drafts"
    return None, f"unknown: run make eval MODEL=haiku DRAFT_MODEL=sonnet (about $0.20)"


def loop_cost() -> dict:
    """Average agent-loop cost per email from the saved traces, for emails that
    need a reply and those that don't: {True/False: (dollars, traces)}."""
    groups: dict[bool, list[float]] = {True: [], False: []}
    for path in RUNS_DIR.glob("trace-*.json"):
        trace = json.loads(path.read_text())
        if "needs_reply" not in trace:
            continue
        groups[trace["needs_reply"]].append(sum(c["dollars"] for c in trace["calls"] if c["step"] == "agent loop"))
    return {k: (sum(v) / len(v), len(v)) for k, v in groups.items() if v}


def per_email(rows: list[dict]) -> dict:
    """Cost of each step for an average email, None where unmeasured, with notes."""
    classify, classify_note = classify_cost(rows)
    share, share_note = reply_share(rows)
    draft, draft_note = draft_cost()
    loops = loop_cost()
    if True in loops and False in loops and share is not None:
        loop = share * loops[True][0] + (1 - share) * loops[False][0]
        loop_note = f"measured on {loops[True][1] + loops[False][1]} traces"
    elif loops:
        only = next(iter(loops))
        loop = loops[only][0]
        loop_note = (f"measured on {loops[only][1]} traces, all {'needing' if only else 'not needing'} a reply; "
                     f"trace the other kind too")
    else:
        loop, loop_note = None, "unknown: run make eval-trace (see below)"
    return {
        "classify": (classify, classify_note),
        "draft": (None if draft is None or share is None else draft * share,
                  draft_note if share is None or draft is None else
                  f"${draft:.4f} a draft × {share:.0%} of emails ({share_note})"),
        "agent loop": (loop, loop_note),
    }


def suggested_trace_ids(rows: list[dict]) -> list[str]:
    """A mix to trace: 2 newsletters or alerts, 1 other email needing no reply, 2 needing a reply."""
    checked = [r for r in rows if r["checked"] == "yes"]
    bulk = [r for r in checked if r["needs_reply"] == "no" and any(w in r["labels"] for w in ("Newsletters", "Alerts"))]
    other = [r for r in checked if r["needs_reply"] == "no" and r not in bulk]
    reply = [r for r in checked if r["needs_reply"] == "yes"]
    return [r["id"] for r in bulk[:2] + other[:1] + reply[:2]]


# ---------- Report ----------

def money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:,.2f}"


def report(full: list[tuple[str, int]], current: tuple[str, int, float], steps: dict, trace_ids: list[str]) -> str:
    known = sum(v for v, _ in steps.values() if v is not None)
    missing = [name for name, (v, _) in steps.items() if v is None]

    def cost(emails: int) -> str:
        return money(emails * known) if not missing else f"≥ {money(emails * known)}"

    lines = ["Emails received per month (upper bound: includes mail your filters keep out of the inbox)", ""]
    lines += [f"  {month:<10}{n:>7,}   {cost(n)}" for month, n in full]
    month, so_far, gone = current
    projected = round(so_far / gone) if gone else so_far
    lines.append(f"  {month:<10}{so_far:>7,}   so far ({gone:.0%} of the month): about {projected:,} by its end")
    average = round(sum(n for _, n in full) / len(full)) if full else projected
    lines += ["", f"  Average month: {average:,} emails, about {average / 30.4:.0f} a day", ""]

    lines.append(f"Cost per email (classify {pipeline.CLASSIFY_MODEL}; draft and agent loop {agent.MODEL})")
    for name, (value, note) in steps.items():
        lines.append(f"  {name:<11}{'unknown' if value is None else f'${value:.5f}':>10}   {note}")
    lines.append(f"  {'sub-label cleanup':<11} not measured: a few small calls a month")
    lines += ["", f"Average month: {cost(average)}"
              + (f"   ({', '.join(missing)} not measured yet)" if missing else "")]
    if known and not missing:
        lines.append(f"  $1 handles about {1 / known:,.0f} emails")
    if "agent loop" in missing or "trace the other kind" in steps["agent loop"][1]:
        cmd = f'make eval-trace ID="{" ".join(trace_ids)}"'
        lines += ["", "To measure the agent loop, trace 5 typical emails (2 newsletters or alerts, 1 other, 2 needing a reply),",
                  f"about $0.03 to $0.08 each:\n  {cmd}"]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monthly email volume and the agent's estimated cost.")
    parser.add_argument("--months", type=int, default=6, help="full months to count (default 6)")
    args = parser.parse_args()
    rows = review.read_rows()
    full, current = volume(args.months)  # the token.json account
    if full:  # saved for make bench, which scales its cost per email to a month
        RUNS_DIR.mkdir(exist_ok=True)
        VOLUME_FILE.write_text(json.dumps({"months": full, "average": round(sum(n for _, n in full) / len(full))}))
    print(report(full, current, per_email(rows), suggested_trace_ids(rows)))


if __name__ == "__main__":
    main()
