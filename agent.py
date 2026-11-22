"""Minimal email agent: classifies the newest inbox emails by label and urgency,
runs the required actions for that urgency, drafts a reply when one is needed,
then lets Claude take more.

Usage:
    python agent.py [N]    # process the newest N inbox emails (default 5)
"""

import sys
from email.utils import parseaddr
from pathlib import Path
from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_model_call
from langchain_anthropic import ChatAnthropic
from pydantic import BaseModel, create_model

import actions
import gmail
import labels
import run_context

MODEL = "claude-sonnet-5-5"
DEFAULT_COUNT = 5
SKILLS_DIR = Path(__file__).parent / "skills"

Urgency = Literal["not_urgent", "schedule_time", "instant"]

SYSTEM_PROMPT = """You triage incoming emails. Return one result per email with the fields below.

Inputs: the allowed labels, each with its description and sub-label rule, and each label's existing sub-labels.

labels (list)
Pick 0 to 3 labels from the allowed labels, best fit first. Each label's description says what belongs under it and what doesn't; follow it. Usually one label is right. Add another only when the email clearly belongs under it too. If no label fits, return an empty list rather than forcing one.

urgency (one value)
- not_urgent: no prompt attention needed, including emails worth knowing about that can wait
- schedule_time: needs a reply that takes some time to prepare, and can wait for the next block of email time, usually within a day
- instant: super urgent, needs an answer within the hour; rare, for example a same-day deadline, a problem that is happening now, or a person blocked until the user answers
Always not_urgent, even when they sound time-sensitive:
- event reminders, such as "your event starts in 1 hour" from Luma or a calendar app: the user already knows about the event and nothing needs an answer
- one-time codes and sign-in codes: the user uses them the moment they arrive, so they never need time set aside
Security alerts about the user's own accounts, such as a sign-in attempt or a password change, are not covered by this rule: judge them as usual.

minutes_needed (integer)
How many minutes the user needs to respond to the email, for example to find a meeting time and reply. Count only the response, not any work the email asks for.

needs_reply (true or false)
True only if a person is waiting for a written answer from the user. False for newsletters, notifications, receipts and no-reply senders.

sub-labels (one entry per label in labels)
For each label:
- sublabel_needed (true or false): True when the label's sub-label rule says what to group by, such as one per trip, per repository or per person; this includes notifications, e.g. GitHub emails grouped by repository. Without such a rule, true only if the label covers several separate things that each get more than one email, such as an event, a project, a trip or an application. The sub-label then groups the emails about that one thing. Never true for streams with no such grouping, such as newsletters or receipts, and never for a label whose sub-label rule is "never".
- sublabel (string or null): null when sublabel_needed is false. Otherwise, use the exact name of one of that label's existing sub-labels if one fits. If none fits, give a new name of 1 to 4 words without "/".

Total cap: labels and sub-labels together make at most 3. A sub-label counts as a label, so one label with a sub-label plus one more label is the maximum.."""


class Email(BaseModel):
    id: str
    thread_id: str
    sender: str
    to: str
    cc: str
    subject: str
    body: str


def result_model(label_names: list[str]) -> type[BaseModel]:
    # Built per run so the label enum always matches what Gmail returns.
    label_choice = create_model(
        "LabelChoice",
        label=(Literal[tuple(label_names)], ...),
        sublabel_needed=(bool, ...),
        sublabel=(str | None, ...),
    )
    return create_model(
        "Result",
        labels=(list[label_choice], ...),
        urgency=(Urgency, ...),
        minutes_needed=(int, ...),
        needs_reply=(bool, ...),
    )


def label_menu(label_names: list[str]) -> tuple[list[str], str]:
    """The top-level labels, and a prompt listing each with its description from
    labels.md and its existing sub-labels."""
    top = [name for name in label_names if "/" not in name]
    lines = []
    for parent in top:
        line = f"- {labels.describe(parent)}"
        if labels.allows_sublabel(parent):
            # Only one level deep, so deeper labels made by hand are left out.
            subs = [
                name.split("/", 1)[1]
                for name in label_names
                if name.startswith(parent + "/") and name.count("/") == 1
            ]
            if subs:
                line += f" Existing sub-labels: {', '.join(subs)}."
        lines.append(line)
    return top, "\n".join(lines)


def ask(system: str, user: str, schema: type[BaseModel], max_tokens: int) -> BaseModel:
    """One Claude call that returns `schema`, using Claude's native structured output."""
    llm = ChatAnthropic(model=MODEL, max_tokens=max_tokens)
    structured = llm.with_structured_output(schema, method="json_schema", include_raw=True)
    out = structured.invoke([("system", system), ("human", user)])
    if out["raw"].response_metadata.get("stop_reason") == "refusal":
        raise RuntimeError("Model refused")
    if out["parsing_error"]:
        raise out["parsing_error"]
    return out["parsed"]


def classify(email: Email, label_names: list[str]) -> BaseModel:
    top, menu = label_menu(label_names)
    user = f"Allowed labels:\n{menu}\n\nEmail:\n" + email.model_dump_json(indent=2)
    return ask(SYSTEM_PROMPT, user, result_model(top), max_tokens=1024)


def pick_labels(result) -> list[tuple[str, str | None]]:
    """The classifier's (label, sub-label) pairs, without repeats and cut to
    actions.MAX_LABELS in total, each sub-label counting as one."""
    picked, room = [], actions.MAX_LABELS
    for choice in result.labels:
        if any(label == choice.label for label, _ in picked):
            continue
        sublabel = (
            choice.sublabel
            if choice.sublabel_needed and labels.allows_sublabel(choice.label)
            else None
        )
        if room < 1:
            print(f"[labels] dropped {choice.label}: max {actions.MAX_LABELS} labels")
            continue
        if sublabel and room < 2:
            print(
                f"[labels] dropped sub-label {sublabel}: max {actions.MAX_LABELS} labels"
            )
            sublabel = None
        picked.append((choice.label, sublabel))
        room -= 2 if sublabel else 1
    return picked


def sender_name(email: Email) -> str:
    name, address = parseaddr(email.sender)
    return name or address


def block_notes(email: Email, result) -> str:
    """Description for the calendar block: who, about what, how it was labelled."""
    picked = ", ".join(choice.label for choice in result.labels) or "none"
    return f"From: {email.sender}\nSubject: {email.subject}\nLabels: {picked}"


# The minimum that always happens, run by code (not Claude): labelling, then the
# actions for the urgency. Each entry: (action, how to build its arguments from the email and result).
def reply_block(urgent: bool):
    return lambda email, r: {
        "message_id": email.id,
        "title": f"Reply to {sender_name(email)}: {email.subject}",
        "minutes": r.minutes_needed,
        "notes": block_notes(email, r),
        "urgent": urgent,
    }


REQUIRED = {
    "not_urgent": [],
    # Grouped with other email time, usually within a day.
    "schedule_time": [(actions.block_time, reply_block(urgent=False))],
    # Not grouped: the first free time, so right after the meeting the user is in.
    "instant": [(actions.block_time, reply_block(urgent=True))],
}

MAX_STEPS = 5


def load_skill(name: str) -> str:
    """The instructions of skills/<name>/SKILL.md, without its frontmatter,
    with {user_name} and {first_name} filled in for the current user."""
    text = (SKILLS_DIR / name / "SKILL.md").read_text()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    ctx = run_context.current()
    user_name = f"{ctx.first_name} {ctx.last_name}".strip()
    text = text.replace("{user_name}", user_name).replace("{first_name}", ctx.first_name)
    return text.strip()


class Reply(BaseModel):
    body: str
    reply_all: bool
    reply_all_reason: str


ACT_PROMPT = """You handle an email that has already been classified, and its required actions are already done.
Use the tools to take any further actions that would genuinely help the user.
Don't repeat an action that has already been done. If nothing more is needed, take no action.
Finish with one line saying what you did."""


def run_required(email: Email, result) -> list[str]:
    done = []
    for label, sublabel in pick_labels(result):
        args = {"message_id": email.id, "label_name": label, "sublabel": sublabel}
        done.append(f"apply_label({args}) -> {actions.apply_label(**args)}")
    for action, build_args in REQUIRED[result.urgency]:
        args = build_args(email, result)
        done.append(f"{action.__name__}({args}) -> {action(**args)}")
    if result.needs_reply:
        done.append(auto_draft(email, result, done))
    return done


def write_reply(email: Email, result, done: list[str]) -> Reply:
    user = (
        f"Email:\n{email.model_dump_json(indent=2)}\n\n"
        f"Classification:\n{result.model_dump_json(indent=2)}\n\n"
        "Already done:\n" + ("\n".join(done) or "nothing")
    )
    return ask(load_skill("write-reply"), user, Reply, max_tokens=4096)


def auto_draft(email: Email, result, done: list[str]) -> str:
    # Checked before calling Claude so reruns don't pay for a draft that gets skipped.
    if gmail.thread_has_draft(email.thread_id):
        return "draft_reply -> skipped: this thread already has a draft"
    reply = write_reply(email, result, done)
    outcome = actions.draft_reply(
        email.id, reply.body, reply.reply_all, reply.reply_all_reason
    )
    return f"draft_reply(message_id={email.id!r}, reply_all={reply.reply_all}) -> {outcome}"


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


def run(email: Email, label_names: list[str]):
    result = classify(email, label_names)
    print(result.model_dump_json(indent=2))

    done = run_required(email, result)
    act(email, result, done)
    return result


def main() -> None:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_COUNT
    label_names = [label["name"] for label in gmail.list_labels()]
    if not [name for name in label_names if "/" not in name]:
        sys.exit(
            "No user labels in Gmail. Create the labels the agent should choose from."
        )
    for warning in labels.check(label_names):
        print(f"[labels] {warning}")

    for message in gmail.list_newest(count):
        email = Email(**message)
        print(f"\n=== {email.subject} ({email.sender})")
        try:
            # Read per email, so a sub-label made for one email is reused by the next.
            label_names = [label["name"] for label in gmail.list_labels()]
            run(email, label_names)
        except Exception as e:
            print(f"[error] {email.id}: {e}")


if __name__ == "__main__":
    main()
