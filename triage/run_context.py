"""Whose Google account and settings the agent is working with right now.

The CLI uses the token.json account; the web scheduler wraps each user's run
in `use(RunContext(...))` so runs never share connections or cached values.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


@dataclass
class RunContext:
    creds: Credentials
    work_start: time = time(9, 0)
    work_end: time = time(18, 0)
    # Super-urgent emails get reply time outside working hours too.
    urgent_after_hours: bool = False
    # The CLI has no signup, so it keeps these placeholders; web runs use the names from signup.
    first_name: str = "Jane"
    last_name: str = "Doe"
    # The user's labels, as labels.parse gives them; None (the CLI) reads labels.md from the repo.
    labels: dict[str, dict] | None = None
    # Eval mode: every Gmail and Calendar write is recorded in would_do instead of done.
    dry_run: bool = False
    would_do: list[dict] = field(default_factory=list)
    # Eval only: treat every email as just received, so old test emails still get calendar
    # blocks (no age limit, no earlier block found) and the loop runs as it would live.
    as_if_new: bool = False
    _memo: dict[str, Any] = field(default_factory=dict)

    def cached(self, key: str, compute: Callable[[], Any]) -> Any:
        if key not in self._memo:
            self._memo[key] = compute()
        return self._memo[key]

    def service(self, name: str, version: str):
        return self.cached(f"service:{name}", lambda: build(name, version, credentials=self.creds))


_current: ContextVar[RunContext | None] = ContextVar("run_context", default=None)
_cli: RunContext | None = None


def current() -> RunContext:
    global _cli
    if ctx := _current.get():
        return ctx
    if _cli is None:
        from . import google_auth  # CLI only: may open a browser to log in

        _cli = RunContext(google_auth.credentials())
    return _cli


def skip_write(action: str, **details) -> bool:
    """In a dry run, record a write in would_do instead of doing it, and return
    True so the caller skips it. Outside a dry run, return False."""
    ctx = current()
    if ctx.dry_run:
        ctx.would_do.append({"action": action, **details})
    return ctx.dry_run


@contextmanager
def use(ctx: RunContext):
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
