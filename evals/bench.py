"""Benchmark a whole system on the checked test set: accuracy and cost (SHY-12).

A system is the live pipeline with a choice of model per step. A benchmark runs
every checked email through the whole loop, as live (classify, labels and
calendar, reply draft, agent tool loop), in dry run: Gmail and Calendar are only
read, never changed. Every Claude call is logged with its step, tokens and cost.

Usage:
    python -m evals.bench                                  # the current system, named "current"
    python -m evals.bench --name all-sonnet --classify sonnet
    python -m evals.bench --name no-loop --agent none      # skip the agent tool loop
    python -m evals.bench --name loop-always --loop-always # the loop on every email, as before
    python -m evals.bench --limit 20                       # a quick, cheaper check

By default it runs the whole system; only --agent none and --limit make it partial.
Needs token.json and ANTHROPIC_API_KEY, and costs money: it asks before starting.
Each run is saved to eval_runs/bench/<time>-<name>/ (results.jsonl, summary.json),
and a summary line is added to eval_runs/bench/leaderboard.csv. Compare two runs,
and flag the one in production, on the page from python -m evals.bench_review.
"""

import argparse
import contextlib
import csv
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from triage import agent, pipeline, run_context

from . import review
from .export import label_names, load_emails
from .calls import live_context, recording
from .prices import dollars, require_prices
from .scoring import MODELS, RUNS_DIR, URGENT, grade, share, weights
from .volume import monthly_emails

BENCH_DIR = RUNS_DIR / "bench"
LEADERBOARD = BENCH_DIR / "leaderboard.csv"
# Which saved run stands for what runs in production now: {"run": <folder name>}.
PROD_FILE = BENCH_DIR / "prod.json"
PARALLEL = 4  # each worker has its own Google clients; Gmail's quota allows about this many
STEPS = ["classify", "draft reply", "agent loop"]


def system(classify: str | None = None, draft: str | None = None, loop: str | None = None,
           loop_always: bool | None = None) -> dict:
    """The models per step and when the loop runs, defaulting to the live setup.
    loop "none" skips the agent loop; loop_always runs it on every email."""
    pick = lambda name, live: MODELS.get(name, name) if name else live  # noqa: E731
    always = (not pipeline.SKIP_IDLE_LOOP) if loop_always is None else loop_always
    return {
        "classify": pick(classify, pipeline.CLASSIFY_MODEL),
        "draft": pick(draft, pipeline.DRAFT_MODEL),
        "agent": "none" if loop == "none" else pick(loop, agent.MODEL),
        "loop_runs": "never" if loop == "none" else ("every email" if always else "only for replies and calendar time"),
        "emails_dated": "as if just received",
    }


@contextmanager
def use_system(models: dict):
    """Swap the pipeline's models for the length of a benchmark."""
    saved = (pipeline.CLASSIFY_MODEL, pipeline.DRAFT_MODEL, agent.MODEL, pipeline.act, pipeline.SKIP_IDLE_LOOP)
    pipeline.CLASSIFY_MODEL, pipeline.DRAFT_MODEL = models["classify"], models["draft"]
    pipeline.SKIP_IDLE_LOOP = models.get("loop_runs") != "every email"
    if models["agent"] == "none":
        pipeline.act = lambda email, result, done: None
    else:
        agent.MODEL = models["agent"]
    try:
        yield
    finally:
        pipeline.CLASSIFY_MODEL, pipeline.DRAFT_MODEL, agent.MODEL, pipeline.act, pipeline.SKIP_IDLE_LOOP = saved


def labels_text(result) -> str:
    return "; ".join(f"{label}/{sub}" if sub else label for label, sub in pipeline.pick_labels(result))


def record(row: dict, result, calls: list[dict], writes: list[dict], seconds: float, error: str | None) -> dict:
    """One email's benchmark result: what the system decided, what it would have
    done, and every Claude call."""
    draft = next((w for w in writes if w["action"] == "create_reply_draft"), None)
    predicted = None if result is None else {
        "labels": labels_text(result), "urgency": result.urgency,
        "needs_reply": "yes" if result.needs_reply else "no", "minutes_needed": result.minutes_needed,
    }
    return {
        "id": row["id"], "subject": row["subject"], "sender": row["sender"],
        "expected": {k: row[k] for k in ("labels", "urgency", "needs_reply")},
        "predicted": predicted,
        "correct": grade(row, predicted),
        "draft": None if draft is None else {k: draft.get(k) for k in ("to", "cc", "body")},
        "actions": [w for w in writes if w["action"] != "create_reply_draft"],
        "calls": calls,
        "dollars": sum(c["dollars"] for c in calls),
        "seconds": round(seconds, 1),
        "error": error,
    }


_local = threading.local()


def run_one(row: dict, email: dict, names: list[str]) -> dict:
    """The whole live loop on one saved email, in this worker's dry-run context."""
    if not hasattr(_local, "ctx"):
        _local.ctx = live_context()
    ctx = _local.ctx
    first_write = len(ctx.would_do)
    fields = {k: v for k, v in email.items() if k in pipeline.Email.model_fields}
    result, error, start = None, None, time.monotonic()
    with run_context.use(ctx), recording() as log:
        try:
            result = pipeline.run(pipeline.Email(**fields), names)
        except Exception as e:
            error = str(e)
    return record(row, result, log.calls, ctx.would_do[first_write:], time.monotonic() - start, error)


def summarize(name: str, models: dict, records: list[dict], rows: list[dict], emails_a_month: int | None) -> dict:
    """Accuracy and cost of one benchmark run."""
    by_id = {r["id"]: r for r in records}
    rows = [r for r in rows if r["id"] in by_id]
    marks = {r["id"]: by_id[r["id"]]["correct"] for r in rows}
    weight = weights(rows)
    urgent = [r for r in rows if r["urgency"] in URGENT]
    replies = [r for r in rows if r["needs_reply"] == "yes"]
    n = len(records) or 1
    spent = sum(r["dollars"] for r in records)
    # Per email as in your real inbox: frequent senders count as often as they email you.
    total_weight = sum(weight.values()) or 1
    real_cost = sum(by_id[i]["dollars"] * w for i, w in weight.items()) / total_weight
    by_step = {step: sum(c["dollars"] for r in records for c in r["calls"] if c["step"] == step) / n for step in STEPS}
    tokens = {kind: sum(c[f"{kind}_tokens"] for r in records for c in r["calls"]) / n for kind in ("input", "output")}
    return {
        "name": name, "run": "", **{f"model_{k}": v for k, v in models.items()},
        "emails": len(records),
        "all_right": share(rows, marks, "all"),
        "all_right_real_inbox": share(rows, marks, "all", weight),
        "labels": share(rows, marks, "labels"),
        "urgency": share(rows, marks, "urgency"),
        "urgent_caught": share(urgent, marks, "urgency"),
        "reply": share(rows, marks, "reply"),
        "replies_caught": share(replies, marks, "reply"),
        "drafts": sum(1 for r in records if r["draft"]),
        "dollars_per_email": spent / n,
        "dollars_per_email_real_inbox": real_cost,
        **{f"dollars_{step.replace(' ', '_')}": v for step, v in by_step.items()},
        "input_tokens_per_email": tokens["input"],
        "output_tokens_per_email": tokens["output"],
        "seconds_per_email": sum(r["seconds"] for r in records) / n,
        "monthly_emails": emails_a_month,
        "dollars_per_month": real_cost * emails_a_month if emails_a_month else None,
        "emails_per_dollar": 1 / real_cost if real_cost else None,
        "errors": sum(1 for r in records if r["error"]),
        "total_dollars": spent,
    }


def save(summary: dict, records: list[dict], stamp: str) -> Path:
    folder = BENCH_DIR / f"{stamp}-{summary['name']}"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "results.jsonl").open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    old, columns = [], list(summary)
    if LEADERBOARD.exists():
        with LEADERBOARD.open(newline="") as f:
            reader = csv.DictReader(f)
            old = list(reader)
            columns = list(reader.fieldnames or []) + [c for c in columns if c not in (reader.fieldnames or [])]
    with LEADERBOARD.open("w", newline="") as f:
        writer = csv.DictWriter(f, columns, restval="")
        writer.writeheader()
        writer.writerows(old + [summary])
    return folder


def load_runs() -> list[dict]:
    """Every saved benchmark's summary, oldest first, with its folder name."""
    return [{**json.loads(p.read_text()), "folder": p.parent.name} for p in sorted(BENCH_DIR.glob("*/summary.json"))]


def run_folders() -> list[str]:
    return [p.parent.name for p in sorted(BENCH_DIR.glob("*/summary.json"))]


def find_run(wanted: str) -> str:
    """A run's folder name, from the folder name or a system name (its newest run)."""
    folders = run_folders()
    if wanted in folders:
        return wanted
    named = [f for f in folders if json.loads((BENCH_DIR / f / "summary.json").read_text())["name"] == wanted]
    if not named:
        raise SystemExit(f"No benchmark run called {wanted}. Saved: {', '.join(folders) or 'none'}")
    return named[-1]


def prod_run() -> str | None:
    """The folder of the run flagged as production, if it still exists."""
    if not PROD_FILE.exists():
        return None
    folder = json.loads(PROD_FILE.read_text()).get("run")
    return folder if folder in run_folders() else None


def set_prod(wanted: str) -> str:
    folder = find_run(wanted)
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    PROD_FILE.write_text(json.dumps({"run": folder, "set_at": datetime.now().isoformat(timespec="seconds")}))
    return folder


def matches_live(s: dict) -> bool:
    """Whether a run used the same models and loop rule as the code does now."""
    live = system()
    return all(s.get(f"model_{k}") == v for k, v in live.items() if k != "emails_dated")


def tags(s: dict) -> str:
    """"prod" for the flagged run, "live code" for runs matching today's settings."""
    out = []
    if s.get("folder") and s["folder"] == prod_run():
        out.append("prod")
    if matches_live(s):
        out.append("live code")
    return ", ".join(out)


COLUMNS = [
    ("run", "Run", "{}"), ("name", "System", "{}"), ("tags", "Tags", "{}"),
    ("all_right", "All right", "{:.0f}%"), ("all_right_real_inbox", "Real inbox", "{:.0f}%"),
    ("labels", "Labels", "{:.0f}%"), ("urgency", "Urgency", "{:.0f}%"),
    ("urgent_caught", "Urgent caught", "{:.0f}%"), ("replies_caught", "Replies caught", "{:.0f}%"),
    ("dollars_per_email_real_inbox", "$/email", "{:.4f}"), ("dollars_per_month", "$/month", "{:.2f}"),
    ("emails_per_dollar", "Emails/$", "{:,.0f}"),
    ("seconds_per_email", "s/email", "{:.1f}"), ("emails", "Emails", "{}"), ("errors", "Errors", "{}"),
]


def with_derived(s: dict) -> dict:
    """Runs saved before a metric existed get it computed from what they stored."""
    if s.get("emails_per_dollar") is None and s.get("dollars_per_email_real_inbox"):
        s = {**s, "emails_per_dollar": 1 / s["dollars_per_email_real_inbox"]}
    s = {**s, "tags": tags(s)}
    return s


def table(summaries: list[dict]) -> str:
    summaries = [with_derived(s) for s in summaries]
    cells = [[title for _, title, _ in COLUMNS]] + [
        ["–" if s.get(key) in (None, "") else fmt.format(s[key]) for key, _, fmt in COLUMNS] for s in summaries
    ]
    widths = [max(len(r[i]) for r in cells) for i in range(len(COLUMNS))]
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(r)) for r in cells]
    return "\n".join([lines[0], "  ".join("-" * w for w in widths), *lines[1:]])


def cost_breakdown(s: dict) -> str:
    lines = [f"Cost per email, test set average (models: classify {s['model_classify']}, "
             f"draft {s['model_draft']}, agent loop {s['model_agent']})"]
    for step in STEPS:
        key = "dollars_" + step.replace(" ", "_")
        lines.append(f"  {step:<12}${s[key]:.5f}")
    lines.append(f"  {'total':<12}${s['dollars_per_email']:.5f}   "
                 f"({s['input_tokens_per_email']:,.0f} input + {s['output_tokens_per_email']:,.0f} output tokens)")
    lines.append(f"  real inbox  ${s['dollars_per_email_real_inbox']:.5f}   (weighted like your actual mail)")
    if s.get("emails_per_dollar"):
        lines.append(f"\n$1 handles about {s['emails_per_dollar']:,.0f} emails")
    if s["dollars_per_month"] is not None:
        lines.append(f"\nMonthly: {s['monthly_emails']:,} emails × ${s['dollars_per_email_real_inbox']:.5f} "
                     f"= about ${s['dollars_per_month']:.2f} a month")
    else:
        lines.append("\nMonthly: unknown (Gmail's email count could not be read).")
    return "\n".join(lines)


def estimate(models: dict, rows: list[dict]) -> str:
    """A rough cost before starting: from the newest run of the same models, else
    from the tokens per step of the newest run of any system, priced at these models."""
    for s in reversed(load_runs()):
        same = all(s.get(f"model_{k}") == v for k, v in models.items() if k != "emails_dated")
        # Only runs that mostly worked: a run that failed (e.g. a bad API key) cost nothing.
        if same and s["emails"] and s["errors"] < s["emails"] / 2 and s["total_dollars"] > 0:
            return f"about ${s['dollars_per_email'] * len(rows):.2f} (from run {s['run']})"
    worked = [s for s in load_runs() if s["emails"] and s["errors"] < s["emails"] / 2 and s["total_dollars"] > 0]
    if not worked:
        return "unknown: no earlier run to estimate from"
    records = [json.loads(line) for line in (BENCH_DIR / worked[-1]["folder"] / "results.jsonl").read_text().splitlines()]
    model_of = {"classify": models["classify"], "draft reply": models["draft"], "agent loop": models["agent"]}

    def step(name: str) -> float:
        """Average cost of this step for an email that runs it, at the chosen model."""
        ran = [[c for c in r["calls"] if c["step"] == name] for r in records]
        ran = [calls for calls in ran if calls]
        model = model_of[name]
        if model == "none" or not ran:
            return 0.0
        return sum(dollars(model, c["input_tokens"], c["output_tokens"]) for calls in ran for c in calls) / len(ran)

    replies = sum(r["needs_reply"] == "yes" for r in rows)
    looped = len(rows) if models["loop_runs"] == "every email" else sum(
        r["needs_reply"] == "yes" or r["urgency"] != "not_urgent" for r in rows)
    total = len(rows) * step("classify") + looped * step("agent loop") + replies * step("draft reply")
    return f"about ${total:.2f} (rough, from the tokens of run {worked[-1]['run']})"


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the whole pipeline on the checked test set.")
    parser.add_argument("--name", default="current", help="name to save the run under")
    parser.add_argument("--classify", help="classify model: haiku, sonnet, opus or a model id")
    parser.add_argument("--draft", help="draft model")
    parser.add_argument("--agent", help="agent loop model, or none to skip the loop")
    parser.add_argument("--loop-always", action="store_true", help="run the agent loop on every email, as before")
    parser.add_argument("--limit", type=int, help="only the first N checked emails")
    parser.add_argument("--yes", action="store_true", help="don't ask before spending")
    parser.add_argument("--list", action="store_true", help="show every saved benchmark and stop")
    parser.add_argument("--prod", metavar="RUN", help="flag a saved run (folder or system name) as production")
    args = parser.parse_args()

    if args.prod:
        print(f"Production run: {set_prod(args.prod)}")
        return
    if args.list:
        runs = load_runs()
        print(table(runs) if runs else "No benchmarks yet. Run make bench.")
        return

    models = system(args.classify, args.draft, args.agent, args.loop_always or None)
    require_prices([models["classify"], models["draft"], models["agent"]])
    rows = [r for r in review.read_rows() if r["checked"] == "yes"][: args.limit]
    if not rows:
        raise SystemExit("No checked answers yet. Check some with make eval-review first.")
    print(f"Benchmark '{args.name}': {len(rows)} emails, whole loop in dry run (Gmail and Calendar unchanged)")
    print(f"  classify {models['classify']} · draft {models['draft']} · agent loop {models['agent']}, "
          f"runs {models['loop_runs']}")
    print(f"  estimated cost: {estimate(models, rows)}")
    if not args.yes and not input("Start? [y/N] ").strip().lower().startswith("y"):
        print("Stopped, nothing spent.")
        return

    emails, names = load_emails(), label_names()
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    records, chatter = [], io.StringIO()
    # The pipeline prints as it goes; keep that out of the progress line.
    with use_system(models), contextlib.redirect_stdout(chatter), ThreadPoolExecutor(PARALLEL) as pool:
        for i, rec in enumerate(pool.map(lambda r: run_one(r, emails[r["id"]], names), rows), 1):
            records.append(rec)
            spent = sum(r["dollars"] for r in records)
            print(f"\r  {i}/{len(rows)} emails, ${spent:.3f} so far", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    failed = [r for r in records if r["error"]]
    if records and len(failed) == len(records):
        # Nothing worked (usually the API key): don't save a run that would skew comparisons.
        raise SystemExit(f"Every email failed, nothing saved. First error:\n  {failed[0]['error'][:300]}")

    summary = summarize(args.name, models, records, review.read_rows(), monthly_emails())
    summary["run"] = stamp
    folder = save(summary, records, stamp)
    (folder / "pipeline-output.txt").write_text(chatter.getvalue())
    for r in records:
        if r["error"]:
            print(f"  [error] {r['subject'][:50]}: {r['error']}")
    print("\n" + table([summary]) + "\n\n" + cost_breakdown(summary))
    print(f"\nSaved: {folder}\nCompare with other runs: make bench-review")


if __name__ == "__main__":
    main()
