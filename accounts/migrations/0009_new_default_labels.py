"""Adds the labels that became defaults in SHY-16 (Work, Personal, Coding) to existing users.

Users keep every label they already have, edited or not; a new label is skipped when the
user already has one with the same name (letters only). Users with no labels yet are left
alone: they get the whole of labels.md the first time their labels load. A user never ends
up with more than LABELS_MAX labels, the Labels tab's limit, so their next save still works.

The labels are copied here rather than read from labels.md, so this migration does the
same thing whenever it runs, even after labels.md changes again.
"""

from django.db import migrations

# accounts.forms.LABELS_MAX when this migration was written; copied so the migration never changes.
LABELS_MAX = 30

NEW_LABELS = [
    {
        "emoji": "💼",
        "name": "Work",
        "sublabels": "one per project or client",
        "cleanup": "once the project is finished or silent for two months",
        "include": "Your job: colleagues, managers, clients, meetings and projects.",
        "exclude": "Not code notifications (Coding), not newsletters from work tools (Newsletters).",
    },
    {
        "emoji": "🏠",
        "name": "Personal",
        "sublabels": "one per person",
        "cleanup": "never",
        "include": "Emails written to you by a real person you know: friends, family and personal conversations.",
        "exclude": "Not anything automated or sent to a big list (Newsletters).",
    },
    {
        "emoji": "💻",
        "name": "Coding",
        "sublabels": "one per repository or project",
        "cleanup": "once the repository or project has been quiet for two months",
        "include": (
            "GitHub, GitLab and other developer tools: pull requests, code reviews, issues, mentions, "
            "CI and build results, deploys, releases, and dependency or security alerts for your repositories."
        ),
        "exclude": (
            "Not sign-in codes (Authorisations), not billing for developer tools (Finance), "
            "not product news from developer tools (Newsletters)."
        ),
    },
]


def key(name: str) -> str:
    """Same as labels.key: a label name by letters only."""
    return "".join(c for c in name.lower() if c.isalpha())


def add_new_labels(apps, schema_editor):
    Label = apps.get_model("accounts", "Label")
    User = apps.get_model("accounts", "User")
    added = []
    for user in User.objects.filter(labels__isnull=False).distinct():
        rows = list(Label.objects.filter(user=user).values_list("name", "position"))
        have = {key(name) for name, _ in rows}
        position = max(position for _, position in rows) + 1
        room = LABELS_MAX - len(rows)
        for label in [label for label in NEW_LABELS if key(label["name"]) not in have][:max(room, 0)]:
            added.append(Label(user=user, position=position, **label))
            position += 1
    Label.objects.bulk_create(added)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_reconnect_reminder"),
    ]

    # Going back keeps the added labels: by then the user may have edited them.
    operations = [
        migrations.RunPython(add_new_labels, migrations.RunPython.noop),
    ]
