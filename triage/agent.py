"""The agent step: after the pipeline's fixed steps, Claude picks tools from
actions.py and decides when to stop, for at most MAX_STEPS calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_model_call
from langchain_anthropic import ChatAnthropic

from . import actions

if TYPE_CHECKING:
    from .pipeline import Email

MODEL = "claude-sonnet-5-5"
MAX_STEPS = 5

ACT_PROMPT = """You handle an email that has already been classified, and its required actions are already done.
Use the tools to take any further actions that would genuinely help the user.
Don't repeat an action that has already been done. If nothing more is needed, take no action.
Finish with one line saying what you did."""


@wrap_model_call
def one_tool_at_a_time(request, handler):
    # The Google API clients aren't thread-safe, and LangGraph runs the tool calls
    # of one turn in parallel, so Claude is told to ask for one tool per turn.
    settings = {**request.model_settings, "parallel_tool_calls": False}
    return handler(request.override(model_settings=settings))


def act_model() -> ChatAnthropic:
    return ChatAnthropic(model=MODEL, max_tokens=16000)


def act(email: Email, result, done: list[str]) -> None:
    user = (
        f"Email:\n{email.model_dump_json(indent=2)}\n\n"
        f"Classification:\n{result.model_dump_json(indent=2)}\n\n"
        "Already done:\n" + ("\n".join(done) or "nothing")
    )
    graph = create_agent(
        act_model(),
        tools=actions.TOOLS,
        system_prompt=ACT_PROMPT,
        middleware=[
            # Ends the loop quietly after MAX_STEPS Claude calls.
            ModelCallLimitMiddleware(run_limit=MAX_STEPS, exit_behavior="end"),
            one_tool_at_a_time,
        ],
    )
    final = graph.invoke({"messages": [("human", user)]})["messages"][-1]
    if final.response_metadata.get("stop_reason") == "refusal":
        raise RuntimeError("Model refused")
    print("[agent]", final.text)
