"""Keep the Labels tab and Gmail's labels in step. Both directions only add.

Push: the agent picks labels from Gmail's list, not from the database, so a label
that exists only on the Labels tab is invisible to it. push_to_gmail creates it.

Import: a label made by hand in Gmail gets a row, so the user can describe it.
import_from_gmail adds rows for top-level labels Gmail didn't have the last time
it looked, so deleting a row doesn't bring it back while its Gmail label stays.

Renaming or deleting never changes the other side.
"""

from triage import gmail
from triage import labels
from triage import run_context

from . import runner
from .forms import LABELS_MAX
from .models import GoogleConnection, Label, User, user_labels


def gmail_labels(user: User) -> tuple[list[dict] | None, str]:
    """The user's Gmail labels, or None and why Gmail couldn't be read."""
    connection = GoogleConnection.objects.filter(user=user).first()
    if not connection or connection.status != GoogleConnection.Status.CONNECTED:
        return None, "Connect Google to check your labels against Gmail."
    try:
        with run_context.use(runner.context(user)):
            return gmail.list_labels(), ""
    except Exception as e:
        return None, f"Couldn't read your Gmail labels: {e}"


def gmail_label_names(user: User) -> tuple[list[str] | None, str]:
    """The user's Gmail label names, or None and why Gmail couldn't be read."""
    found, problem = gmail_labels(user)
    return (None if found is None else [label["name"] for label in found]), problem


def import_from_gmail(user: User, found: list[dict]) -> list[str]:
    """Add a row for each top-level Gmail label that is new since the last look
    and not described yet. Returns the imported names. The first look only takes
    note of what's there: older undescribed labels stay with the "Add them" button."""
    connection = user.google
    # The scheduler loads users up front, so a long run before this one can leave
    # the cached value stale; a stale list would re-import a row deleted meanwhile.
    connection.refresh_from_db(fields=["seen_label_ids"])
    top =[label for label in found if "/" not in label["name"]]
    seen = connection.seen_label_ids
    connection.seen_label_ids = sorted(label["id"] for label in top)
    connection.save(update_fields=["seen_label_ids"])
    if seen is None:
        return []

    rows = user_labels(user)
    described = {labels.key(row.name) for row in rows}
    position = max((row.position for row in rows), default=-1) + 1
    added = []
    for label in top:
        name_key = labels.key(label["name"])
        if label["id"] in seen or not name_key or name_key in described:
            continue
        if len(rows) + len(added) >= LABELS_MAX:
            break  # still listed under "In Gmail but not described"
        described.add(name_key)
        emoji, name = labels.split_emoji(label["name"])
        added.append(Label(user=user, position=position + len(added), emoji=emoji, name=name))
    Label.objects.bulk_create(added)
    return [str(row) for row in added]


def gmail_name(label: Label) -> str:
    """The Gmail name for a row: "💼 Work", or "Work" without an emoji."""
    return f"{label.emoji} {label.name}".strip()


def push_to_gmail(user: User, rows: list[Label]) -> tuple[list[str], list[str]]:
    """Create each row Gmail doesn't have yet, matched by letters only like the
    "Not in Gmail" badge. Returns (created names, problems); never raises."""
    gmail_names, problem = gmail_label_names(user)
    if gmail_names is None:
        return [], [problem] if rows else []
    have = {labels.key(name) for name in gmail_names if "/" not in name}
    created, problems = [], []
    with run_context.use(runner.context(user)):
        for row in rows:
            if labels.key(row.name) in have:
                continue
            name = gmail_name(row)
            try:
                gmail.create_label(name)
            except Exception as e:
                problems.append(f"Couldn't create {name} in Gmail: {e}")
                continue
            have.add(labels.key(row.name))
            created.append(name)
    return created, problems
