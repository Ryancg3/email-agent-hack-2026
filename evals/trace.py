"""Tokens and cost of one email through the whole live pipeline (SHY-12).

Runs the agent exactly as it runs live (classify, labels and calendar, reply
draft, the agent's tool loop) on one email, in dry run (SHY-11): Gmail and
Calendar are only read, never changed. Every Claude call is logged with its
step, model, tokens and cost.

Usage:
    python -m evals.trace [MESSAGE_ID ...]   # default: the first eval email you marked as needing a reply

Needs token.json (it reads the email from Gmail) and ANTHROPIC_API_KEY. Costs
one email's worth of Claude calls, a few cents. Saves eval_runs/trace-<time>-<id>.json.
"""

import argparse
import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

from triage import agent, gmail, google_auth, labels, pipeline, run_context

from . import review
from .export import DESCRIPTIONS_FILE, PROFILE_FILE
from .prices import PRICES, dollars
from .run import RUNS_DIR


def step_of(system_prompt: str) -> str:
    """Which pipeline step a Claude call belongs to, told apart by its system prompt."""
    if pipeline.SYSTEM_PROMPT[:80] in system_prompt:
        return "classify"
    if agent.ACT_PROMPT[:80] in system_prompt:
        return "agent loop"
    if "draft email replies" in system_prompt:
        return "draft reply"
    return "other"


class CallLog(BaseCallbackHandler):
    """Records every chat model call made while it is active: step, model, tokens, seconds."""

    def __init__(self):
        self.calls: list[dict] = []
        self._started: dict = {}
        self._lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        system = next((m.content for m in messages[0] if m.type == "system"), "") if messages else ""
        params = kwargs.get("invocation_params") or {}
        with self._lock:
            self._started[run_id] = (step_of(str(system)), params.get("model") or params.get("model_name") or "", time.monotonic())

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self._lock:
            step, model, start = self._started.pop(run_id, ("other", "", time.monotonic()))
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        message = getattr(generation, "message", None)
        usage = getattr(message, "usage_metadata", None) or {}
        # The model the API reports; the requested one if that name has no price (e.g. a dated alias).
        reported = (getattr(message, "response_metadata", {}) or {}).get("model_name")
        model = reported if reported in PRICES else model
        call = {
            "step": step, "model": model,
            "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
            "seconds": round(time.monotonic() - start, 2),
        }
        call["dollars"] = dollars(model, call["input_tokens"], call["output_tokens"])
        with self._lock:
            self.calls.append(call)


_log: ContextVar[CallLog | None] = ContextVar("eval_trace_log", default=None)
# Like LangChain's get_usage_metadata_callback: every model call inside recording() reports to the log.
register_configure_hook(_log, inheritable=True)


@contextmanager
def recording():
    log = CallLog()
    token = _log.set(log)
    try:
        yield log
    finally:
        _log.reset(token)


def table(calls: list[dict]) -> str:
    """One line per Claude call, numbering repeated steps, plus a total."""
    seen: dict[str, int] = {}
    lines = [f"{'#':>2}  {'Step':<14}{'Model':<19}{'Input':>8}{'Output':>8}{'Cost $':>10}{'Secs':>6}"]
    for i, call in enumerate(calls, 1):
        seen[call["step"]] = seen.get(call["step"], 0) + 1
        step = call["step"] + (f" {seen[call['step']]}" if call["step"] == "agent loop" else "")
        lines.append(f"{i:>2}  {step:<14}{call['model']:<19}{call['input_tokens']:>8,}{call['output_tokens']:>8,}"
                     f"{call['dollars']:>10.4f}{call['seconds']:>6.1f}")
    total_in = sum(c["input_tokens"] for c in calls)
    total_out = sum(c["output_tokens"] for c in calls)
    total = sum(c["dollars"] for c in calls)
    lines.append("-" * 67)
    lines.append(f"{'':>2}  {'Total':<14}{'':<19}{total_in:>8,}{total_out:>8,}{total:>10.4f}"
                 f"{sum(c['seconds'] for c in calls):>6.1f}")
    if total:
        lines.append(f"\nAt this cost, $1 processes about {1 / total:,.0f} emails like this one.")
    return "\n".join(lines)


def default_message_id() -> str:
    """The first eval email you marked as needing a reply, so every step runs, the draft included."""
    row = next((r for r in review.read_rows() if r["needs_reply"] == "yes"), None)
    if row is None:
        raise SystemExit("Pass a Gmail message id: no eval email is marked as needing a reply.")
    return row["id"]


def live_context() -> run_context.RunContext:
    """The token.json account in dry run, with the label descriptions and name saved
    with the test set, so the prompts are as long as on the web. Every email is
    treated as just received, so the calendar step runs on old test emails too."""
    saved = labels.parse(DESCRIPTIONS_FILE.read_text()) if DESCRIPTIONS_FILE.exists() else None
    profile = json.loads(PROFILE_FILE.read_text()) if PROFILE_FILE.exists() else {}
    return run_context.RunContext(google_auth.credentials(), labels=saved, dry_run=True, as_if_new=True, **profile)


def trace(message_id: str, ctx: run_context.RunContext) -> dict:
    """Run one email through the whole pipeline in dry run; print and save its calls."""
    first_write = len(ctx.would_do)
    with run_context.use(ctx):
        email = pipeline.Email(**gmail.get_email(message_id))
        label_names = [label["name"] for label in gmail.list_labels()]
        print(f"\n=== Dry run, nothing in Gmail or Calendar changes: {email.subject} ({email.sender})\n")
        with recording() as log:
            result = pipeline.run(email, label_names)

    print("\n" + table(log.calls))
    if result.needs_reply and not any(c["step"] == "draft reply" for c in log.calls):
        print("No draft call: the thread already has a draft.")
    RUNS_DIR.mkdir(exist_ok=True)
    record = {"message_id": message_id, "subject": email.subject, "needs_reply": result.needs_reply,
              "urgency": result.urgency, "calls": log.calls, "would_do": ctx.would_do[first_write:]}
    path = RUNS_DIR / f"trace-{datetime.now():%Y%m%d-%H%M%S}-{message_id}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1))
    print(f"Saved: {path}")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Tokens and cost of emails through the whole pipeline.")
    parser.add_argument("message_ids", nargs="*", help="Gmail message ids (default: first eval email needing a reply)")
    args = parser.parse_args()
    ctx = live_context()
    records = [trace(message_id, ctx) for message_id in args.message_ids or [default_message_id()]]
    if len(records) > 1:
        total = sum(c["dollars"] for r in records for c in r["calls"])
        print(f"\n{len(records)} emails, ${total:.4f} in total, ${total / len(records):.4f} per email on average.")


if __name__ == "__main__":
    main()
