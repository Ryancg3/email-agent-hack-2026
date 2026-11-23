"""Records every Claude call the pipeline makes, with its step, tokens and cost (SHY-12).

make bench wraps each email in recording(), so its results show every call; the
benchmark comparison page lists them per email. live_context() is the dry-run
context the benchmark runs the whole pipeline in.
"""

import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook

from triage import agent, google_auth, labels, pipeline, run_context

from .export import DESCRIPTIONS_FILE, PROFILE_FILE
from .prices import PRICES, dollars


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


def live_context() -> run_context.RunContext:
    """The token.json account in dry run, with the label descriptions and name saved
    with the test set, so the prompts are as long as on the web. Every email is
    treated as just received, so the calendar step runs on old test emails too."""
    saved = labels.parse(DESCRIPTIONS_FILE.read_text()) if DESCRIPTIONS_FILE.exists() else None
    profile = json.loads(PROFILE_FILE.read_text()) if PROFILE_FILE.exists() else {}
    return run_context.RunContext(google_auth.credentials(), labels=saved, dry_run=True, as_if_new=True, **profile)
