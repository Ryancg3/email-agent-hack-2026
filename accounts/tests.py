import base64
import os
import csv
import importlib
import json
import tempfile
from datetime import datetime, time, timedelta, timezone
from collections import Counter
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone
from google.auth.exceptions import RefreshError
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from evals import bench as eval_bench
from evals import bench_review as eval_bench_review
from evals import calls as eval_calls
from evals import export as eval_export
from evals import prices as eval_prices
from evals import review as eval_review
from evals import scoring as eval_scoring
from evals import volume as eval_volume
from triage import actions
from triage import agent
from triage import pipeline
from triage import labels
from triage import run_context
from triage import sublabel_cleanup

from . import cleanup_runner, google_connect, push, reminder, runner
from .management.commands import run_scheduler
from .forms import LABELS_MAX
from .models import GoogleConnection, Label, ProcessedEmail, SublabelReview, User


def make_connected_user(email="a@example.com"):
    user = User.objects.create_user(email, "a-long-password")
    GoogleConnection.objects.create(
        user=user, google_email=email, refresh_token=google_connect.encrypt("refresh")
    )
    return user


class AccountViewsTests(TestCase):
    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_answers_any_host(self):
        response = self.client.get("/health", HTTP_HOST="10.0.0.5:10000")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/", HTTP_HOST="10.0.0.5:10000").status_code, 400)

    def test_landing_page_adapts_to_login_state(self):
        self.assertContains(self.client.get(reverse("home")), reverse("signup"))
        self.client.force_login(User.objects.create_user("a@example.com", "a-long-password"))
        self.assertContains(self.client.get(reverse("home")), "Open dashboard")

    def test_signup_logs_in_and_rejects_duplicate_email_in_any_case(self):
        data = {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "New@Example.com",
            "password1": "a-long-password",
            "password2": "a-long-password",
        }
        response = self.client.post(reverse("signup"), data)
        self.assertRedirects(response, reverse("dashboard"))
        user = User.objects.get(email="new@example.com")
        self.assertEqual((user.first_name, user.last_name), ("Ada", "Lovelace"))

        self.client.logout()
        response = self.client.post(reverse("signup"), {**data, "email": "NEW@example.com"})
        self.assertContains(response, "already exists")

    def test_signup_requires_first_and_last_name(self):
        data = {"email": "new@example.com", "password1": "a-long-password", "password2": "a-long-password"}
        response = self.client.post(reverse("signup"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    @override_settings(SIGNUP_CLOSED=True)
    def test_signup_closed_creates_no_account(self):
        data = {
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "new@example.com",
            "password1": "a-long-password",
            "password2": "a-long-password",
        }
        response = self.client.post(reverse("signup"), data)
        self.assertRedirects(response, reverse("login"))
        self.assertFalse(User.objects.filter(email="new@example.com").exists())

    def test_login_is_case_insensitive(self):
        User.objects.create_user("a@example.com", "a-long-password")
        response = self.client.post(reverse("login"), {"username": "A@Example.com", "password": "a-long-password"})
        self.assertRedirects(response, reverse("dashboard"))

    def test_settings_reject_end_before_start(self):
        user = make_connected_user()
        self.client.force_login(user)
        response = self.client.post(reverse("settings"), {"work_start": "18:00", "work_end": "09:00"})
        self.assertContains(response, "must end after")
        user.refresh_from_db()
        self.assertEqual(user.work_start, time(9, 0))

    def test_connect_explains_missing_google_client(self):
        self.client.force_login(User.objects.create_user("a@example.com", "a-long-password"))
        with self.settings(GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET=""):
            response = self.client.get(reverse("google_connect"), follow=True)
        self.assertContains(response, "isn&#x27;t set up yet")

    def test_connect_redirects_to_google(self):
        self.client.force_login(User.objects.create_user("a@example.com", "a-long-password"))
        with self.settings(GOOGLE_CLIENT_ID="id.apps.googleusercontent.com", GOOGLE_CLIENT_SECRET="secret"):
            response = self.client.get(reverse("google_connect"))
        self.assertTrue(response["Location"].startswith("https://accounts.google.com/o/oauth2/auth?"))
        self.assertIn("client_id=id.apps.googleusercontent.com", response["Location"])
        self.assertIn("state", self.client.session["google_oauth"])

    def test_run_now_needs_a_connection(self):
        user = User.objects.create_user("a@example.com", "a-long-password")
        self.client.force_login(user)
        self.client.post(reverse("run_now"))
        user.refresh_from_db()
        self.assertIsNone(user.run_requested_at)

        GoogleConnection.objects.create(user=user, google_email="a@example.com", refresh_token="x")
        self.client.post(reverse("run_now"))
        user.refresh_from_db()
        self.assertIsNotNone(user.run_requested_at)


@override_settings(GOOGLE_CLIENT_ID="id", GOOGLE_CLIENT_SECRET="secret")
class GoogleCallbackTests(TestCase):
    def setUp(self):
        self.user = make_connected_user("a@example.com")
        self.old_cutoff = django_timezone.now() - timedelta(days=30)
        GoogleConnection.objects.filter(user=self.user).update(connected_at=self.old_cutoff)
        self.client.force_login(self.user)

    def reconnect(self, address):
        session = self.client.session
        session["google_oauth"] = {"state": "s", "code_verifier": "v"}
        session.save()
        creds = SimpleNamespace(refresh_token="new-refresh")
        with mock.patch.object(google_connect, "exchange_code", return_value=creds), \
                mock.patch.object(google_connect, "has_all_scopes", return_value=True), \
                mock.patch.object(google_connect, "account_email", return_value=address):
            self.client.get(reverse("google_callback"), {"state": "s", "code": "c"})
        return GoogleConnection.objects.get(user=self.user)

    def test_same_account_keeps_cutoff_to_catch_up(self):
        connection = self.reconnect("A@example.com")
        self.assertEqual(connection.connected_at, self.old_cutoff)

    def test_different_account_starts_cutoff_now(self):
        connection = self.reconnect("b@example.com")
        self.assertEqual(connection.google_email, "b@example.com")
        self.assertGreater(connection.connected_at, self.old_cutoff + timedelta(days=29))


class DashboardProblemsBannerTests(TestCase):
    BANNER = "failed in the last 24 hours"

    def setUp(self):
        self.user = make_connected_user()
        self.client.force_login(self.user)

    def fail(self, message_id, error, hours_ago):
        email = ProcessedEmail.objects.create(
            user=self.user, message_id=message_id, status=ProcessedEmail.Status.ERROR, error=error
        )
        ProcessedEmail.objects.filter(pk=email.pk).update(
            processed_at=django_timezone.now() - timedelta(hours=hours_ago)
        )

    def test_hidden_without_failures(self):
        ProcessedEmail.objects.create(user=self.user, message_id="ok", subject="Fine")
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, self.BANNER)
        self.assertNotContains(response, "Recent activity")
        self.assertContains(response, "Run now")

    def test_shows_count_and_latest_error_for_failures_in_the_last_24_hours(self):
        self.fail("a", "Calendar insert failed", hours_ago=5)
        self.fail("b", "Anthropic API overloaded", hours_ago=1)
        self.fail("c", "Too old to count", hours_ago=30)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f"2 emails {self.BANNER}")
        self.assertContains(response, "Anthropic API overloaded")
        self.assertNotContains(response, "Calendar insert failed")

    def test_hidden_for_failures_older_than_24_hours(self):
        self.fail("old", "Too old to count", hours_ago=25)
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, self.BANNER)
        self.assertNotContains(response, "Too old to count")

    def test_reconnect_banner_when_google_disconnects(self):
        GoogleConnection.objects.filter(user=self.user).update(status=GoogleConnection.Status.RECONNECT_NEEDED)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Google is disconnected, so the agent has stopped.")
        self.assertContains(
            response, f'class="btn btn-primary btn-sm alert-action" href="{reverse("google_connect")}"'
        )

    def test_no_reconnect_banner_while_connected(self):
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Google is disconnected")


class DashboardStatusOverviewTests(TestCase):
    def setUp(self):
        self.user = make_connected_user()
        self.client.force_login(self.user)

    def test_shows_agent_google_run_count_and_delivery_status(self):
        self.user.google.last_run_at = django_timezone.now() - timedelta(minutes=10)
        self.user.google.save(update_fields=["last_run_at"])
        today_start = django_timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        ProcessedEmail.objects.create(user=self.user, message_id="ok-today")
        ProcessedEmail.objects.create(
            user=self.user, message_id="failed-today", status=ProcessedEmail.Status.ERROR
        )
        old = ProcessedEmail.objects.create(user=self.user, message_id="ok-yesterday")
        ProcessedEmail.objects.filter(pk=old.pk).update(
            processed_at=today_start - timedelta(microseconds=1)
        )

        response = self.client.get(reverse("dashboard"))

        self.assertContains(response, "Agent status")
        self.assertContains(response, "Active")
        self.assertContains(response, "Connected")
        self.assertContains(response, "Last run")
        self.assertContains(response, "Handled today (UTC)")
        self.assertContains(response, '<span class="status-value">1</span>', html=True)
        self.assertContains(response, "Poll · every 5 minutes")


@mock.patch.object(runner.google_connect, "credentials", return_value=None)
class RunnerTests(TestCase):
    def setUp(self):
        self.user = make_connected_user()
        patcher = mock.patch.multiple(
            runner.gmail,
            list_labels=mock.Mock(return_value=[{"name": "Work"}]),
            list_inbox_ids=mock.Mock(return_value=["m3", "m2", "m1"]),  # newest first
            get_email=lambda message_id: {
                "id": message_id, "thread_id": f"t{message_id}", "sender": "s", "to": "t", "cc": "",
                "subject": f"subject {message_id}", "body": "b",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_processes_only_new_emails_oldest_first(self, _):
        ProcessedEmail.objects.create(user=self.user, message_id="m1")
        seen = []
        result = SimpleNamespace(
            labels=[
                SimpleNamespace(label="Work", sublabel_needed=True, sublabel="Launch"),
                SimpleNamespace(label="Team", sublabel_needed=False, sublabel=None),
            ],
            urgency="not_urgent",
        )
        with mock.patch.object(runner.pipeline, "run", lambda email, labels: seen.append(email.id) or result):
            runner.run_user(self.user)
        self.assertEqual(seen, ["m2", "m3"])
        self.assertEqual(self.user.processed_emails.filter(status="ok").count(), 3)
        self.assertEqual(self.user.processed_emails.get(message_id="m3").labels, "Work/Launch\nTeam")
        self.assertEqual(self.user.google.last_error, "")

    def test_run_now_reruns_the_newest_emails_even_if_done(self, _):
        ProcessedEmail.objects.create(user=self.user, message_id="m1", subject="old")
        seen = []
        result = SimpleNamespace(labels=[], urgency="not_urgent")
        with mock.patch.object(runner.pipeline, "run", lambda email, labels: seen.append(email.id) or result):
            runner.run_user(self.user, rerun_newest=3)
        runner.gmail.list_inbox_ids.assert_called_once_with(3)  # no connected_at cutoff
        self.assertEqual(seen, ["m1", "m2", "m3"])
        self.assertEqual(self.user.processed_emails.get(message_id="m1").subject, "subject m1")

    def test_run_now_skips_the_agents_own_reminder(self, _):
        ProcessedEmail.objects.create(user=self.user, message_id="m2", status=ProcessedEmail.Status.SKIPPED)
        seen = []
        result = SimpleNamespace(labels=[], urgency="not_urgent")
        with mock.patch.object(runner.pipeline, "run", lambda email, labels: seen.append(email.id) or result):
            runner.run_user(self.user, rerun_newest=3)
        self.assertEqual(seen, ["m1", "m3"])
        self.assertEqual(self.user.processed_emails.get(message_id="m2").status, ProcessedEmail.Status.SKIPPED)

    def test_reply_skill_uses_the_users_name(self, _):
        self.user.first_name, self.user.last_name = "Ada", "Lovelace"
        skills = []
        with mock.patch.object(runner.pipeline, "run", lambda email, labels: skills.append(runner.pipeline.load_skill("write-reply"))):
            runner.run_user(self.user)
        self.assertIn('Sign off with "Ada" for casual emails, "Ada Lovelace" for formal ones.', skills[0])

    def test_classifier_sees_the_users_label_descriptions(self, _):
        Label.objects.create(user=self.user, name="Work", sublabels="never", include="Projects at the office.")
        menus = []
        with mock.patch.object(runner.pipeline, "run", lambda email, names: menus.append(pipeline.label_menu(names)[1])):
            runner.run_user(self.user)
        self.assertEqual(menus[0], "- Work: Projects at the office. Sub-labels: never.")

    def test_failed_email_is_recorded_and_not_retried(self, _):
        with mock.patch.object(runner.pipeline, "run", side_effect=ValueError("bad email")):
            runner.run_user(self.user)
        self.assertEqual(self.user.processed_emails.filter(status="error").count(), 3)

    def test_expired_google_access_asks_to_reconnect(self, _):
        runner.gmail.list_labels.side_effect = RefreshError("invalid_grant")
        runner.run_user(self.user)
        self.user.google.refresh_from_db()
        self.assertEqual(self.user.google.status, GoogleConnection.Status.RECONNECT_NEEDED)
        self.assertFalse(self.user.processed_emails.exists())



def formset_data(rows, initial=0):
    """POST data for the labels formset: rows of {field: value}, the first `initial` existing."""
    data = {
        "labels-TOTAL_FORMS": str(len(rows)), "labels-INITIAL_FORMS": str(initial),
        "labels-MIN_NUM_FORMS": "0", "labels-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(rows):
        for field, value in row.items():
            data[f"labels-{i}-{field}"] = value
    return data


@mock.patch.object(runner.google_connect, "credentials", return_value=None)
class LabelsTabTests(TestCase):
    def setUp(self):
        self.user = make_connected_user()
        self.client.force_login(self.user)
        patcher = mock.patch.object(
            runner.gmail, "list_labels",
            return_value=[{"name": "✈️ Travel"}, {"name": "✈️ Travel/Rome"}, {"name": "Hobbies"}],
        )
        self.list_labels = patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        return [(label.emoji, label.name) for label in self.user.labels.all()]

    def test_existing_user_starts_from_labels_md(self, _):
        response = self.client.get(reverse("labels"))
        auth = self.user.labels.get(name="Authorisations")
        self.assertEqual(auth.emoji, "🔐")
        self.assertEqual(auth.sublabels, "never")
        self.assertTrue(auth.include.startswith("Things you act on to get or grant access"))
        self.assertEqual(auth.exclude, "Not warnings about your account (Alerts).")
        finance = self.user.labels.get(name="Finance")
        self.assertTrue(finance.exclude.endswith("not health insurance (Medical)."))
        self.assertEqual(self.user.labels.get(name="Travel").cleanup, "after the trip is over")
        self.assertFalse(self.user.labels.filter(name="University").exists())
        self.assertEqual(self.user.labels.count(), len(labels.parse(labels.LABELS_FILE.read_text())))
        self.assertContains(response, "In Gmail but not described: Hobbies")
        self.assertContains(response, "Not in Gmail")

    def test_signup_gets_the_default_labels(self, _):
        self.client.logout()
        self.client.post(reverse("signup"), {
            "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
            "password1": "a-long-password", "password2": "a-long-password",
        })
        self.assertTrue(User.objects.get(email="ada@example.com").labels.filter(name="Travel").exists())

    def test_edit_add_and_remove(self, _):
        self.user.labels.all().delete()
        travel = Label.objects.create(user=self.user, emoji="✈️", name="Travel", include="Flights.")
        work = Label.objects.create(user=self.user, position=1, name="Work")
        data = formset_data([
            {"id": travel.pk, "emoji": "✈️", "name": "Travel", "sublabels": "one per trip", "include": "Flights and hotels.", "exclude": ""},
            {"id": work.pk, "emoji": "", "name": "Work", "sublabels": "", "include": "", "exclude": "", "DELETE": "on"},
            {"emoji": "", "name": "🧾 Receipts", "sublabels": "never", "include": "Receipts.", "exclude": "Not invoices."},
        ], initial=2)
        response = self.client.post(reverse("labels"), data, follow=True)
        self.assertContains(response, "Labels saved.")
        self.assertEqual(self.rows(), [("✈️", "Travel"), ("🧾", "Receipts")])
        self.assertEqual(self.user.labels.get(name="Travel").sublabels, "one per trip")

    def test_icon_picker_sets_the_emoji(self, _):
        self.user.labels.all().delete()
        travel = Label.objects.create(user=self.user, emoji="✈️", name="Travel")
        home = Label.objects.create(user=self.user, position=1, emoji="🏠", name="Home")
        data = formset_data([
            {"id": travel.pk, "emoji": " 🧑🏽‍💻 ", "name": "Travel"},  # any emoji, even multi-part ones
            {"id": home.pk, "emoji": "", "name": "Home"},  # an empty box clears it
        ], initial=2)
        self.client.post(reverse("labels"), data)
        self.assertEqual(self.rows(), [("🧑🏽‍💻", "Travel"), ("", "Home")])

    def test_page_shows_each_label_as_a_collapsed_row(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, emoji="✈️", name="Travel", include="Flights.")
        Label.objects.create(user=self.user, position=1, emoji="🦄", name="Work")
        response = self.client.get(reverse("labels"))
        self.assertContains(response, '<details class="label-details">', count=2)
        self.assertContains(response, 'value="🦄"')
        self.assertContains(response, "aria-haspopup=\"dialog\"", count=3)  # two rows and the new-row template
        self.assertContains(response, "vendor/emoji-picker-element/index.js")
        self.assertContains(response, "Reset to defaults")

    def test_rejects_duplicates_and_slashes(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, name="Mine")
        data = formset_data([
            {"name": "Travel"}, {"name": "✈️ travel"}, {"name": "Travel/Rome"},
        ])
        response = self.client.post(reverse("labels"), data)
        self.assertContains(response, "Another label has this name.")
        self.assertContains(response, "No / in label names")
        self.assertEqual(self.rows(), [("", "Mine")])

    def test_rejects_too_many_labels(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, name="Mine")
        names = [f"Label {chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(LABELS_MAX + 1)]
        response = self.client.post(reverse("labels"), formset_data([{"name": name} for name in names]))
        self.assertContains(response, f"You can have at most {LABELS_MAX} labels. Delete one to add another.")
        self.assertEqual(self.rows(), [("", "Mine")])

    def test_add_missing_gmail_labels(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, name="Travel")
        self.client.post(reverse("add_missing_labels"))
        self.assertEqual(self.rows(), [("", "Travel"), ("", "Hobbies")])

    def test_add_missing_stays_within_the_limit(self, _):
        self.list_labels.return_value = [{"name": "Hobbies"}, {"name": "Sport"}]
        self.user.labels.all().delete()
        Label.objects.bulk_create(Label(user=self.user, position=i, name=f"Mine {chr(65 + i)}") for i in range(LABELS_MAX - 1))
        response = self.client.post(reverse("add_missing_labels"), follow=True)
        self.assertContains(response, f"Added 1 label. Describe them below. 1 more didn&#x27;t fit: you can have at most {LABELS_MAX} labels.")
        self.assertEqual(self.user.labels.count(), LABELS_MAX)

    def test_shows_the_label_count(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, name="Travel")
        self.assertContains(self.client.get(reverse("labels")), f"1 of {LABELS_MAX} labels")
        Label.objects.bulk_create(Label(user=self.user, position=i + 1, name=f"Mine {chr(65 + i)}") for i in range(LABELS_MAX - 1))
        self.assertContains(self.client.get(reverse("labels")), f"{LABELS_MAX} of {LABELS_MAX} labels. Delete one to add another.")

    def test_reset_to_defaults(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, name="Mine")
        self.client.post(reverse("reset_labels"))
        self.assertFalse(self.user.labels.filter(name="Mine").exists())
        self.assertTrue(self.user.labels.filter(name="Coding").exists())

    def test_works_without_google(self, _):
        self.user.google.delete()
        response = self.client.get(reverse("labels"))
        self.assertContains(response, "Connect Google to check")
        self.list_labels.assert_not_called()


class NewDefaultLabelsMigrationTests(TestCase):
    migration = importlib.import_module("accounts.migrations.0009_new_default_labels")

    def test_adds_new_defaults_and_keeps_own_labels(self):
        user = make_connected_user()
        user.labels.all().delete()
        Label.objects.create(user=user, emoji="🎓", name="University", include="Courses.")
        Label.objects.create(user=user, position=1, emoji="🛠️", name="coding", include="My own.")
        untouched = make_connected_user("b@example.com")
        untouched.labels.all().delete()

        self.migration.add_new_labels(apps, None)

        rows = [(label.name, label.position, label.include) for label in user.labels.all()]
        self.assertEqual([name for name, _, _ in rows], ["University", "coding", "Work", "Personal"])
        self.assertEqual(rows[0][2], "Courses.")
        self.assertEqual(rows[1][2], "My own.")
        self.assertEqual(user.labels.get(name="Work").sublabels, "one per project or client")
        self.assertFalse(untouched.labels.exists())

    def test_stays_within_the_editor_limit(self):
        user = make_connected_user()
        user.labels.all().delete()
        Label.objects.bulk_create(Label(user=user, position=i, name=f"Mine {chr(65 + i)}") for i in range(LABELS_MAX - 1))

        self.migration.add_new_labels(apps, None)

        self.assertEqual(user.labels.count(), LABELS_MAX)
        self.assertEqual(user.labels.last().name, "Work")
        self.assertEqual(self.migration.LABELS_MAX, LABELS_MAX)

    def test_matches_labels_md(self):
        defaults = labels.parse(labels.LABELS_FILE.read_text())
        for label in self.migration.NEW_LABELS:
            self.assertEqual(defaults[labels.key(label["name"])], {
                field: label[field] for field in ("name", "emoji", "sublabels", "cleanup", "include", "exclude")
            })


class LabelsPerRunTests(TestCase):
    def test_cli_prompt_lines_are_unchanged(self):
        with run_context.use(run_context.RunContext(None)):
            self.assertEqual(
                labels.describe("🔐 Authorisations"),
                "🔐 Authorisations: Things you act on to get or grant access: sign-in codes, 2FA codes, "
                "password resets, account verification, invitations to join an account, team or project. "
                "Not warnings about your account (Alerts). Sub-labels: never.",
            )

    def test_runs_for_two_users_dont_share_labels(self):
        first, second = make_connected_user("a@example.com"), make_connected_user("b@example.com")
        first.labels.all().delete()
        Label.objects.create(user=first, name="Travel", sublabels="one per trip", include="Flights.", exclude="Not events.")
        with mock.patch.object(runner.google_connect, "credentials", return_value=None):
            first, second = runner.context(first), runner.context(second)
        with run_context.use(first):
            self.assertEqual(labels.describe("Travel"), "Travel: Flights. Not events. Sub-labels: one per trip.")
            self.assertFalse(labels.is_described("Medical"))
        with run_context.use(second):  # no labels yet: starts from labels.md
            self.assertTrue(labels.describe("Travel").startswith("Travel: Flights, trains, hotels"))


NOW = datetime.now(timezone.utc)
TODAY = NOW.date()
CLEANUP_MD = """## Travel
Sub-labels: one per trip
Cleanup: after the trip is over
Flights and hotels.

## Medical
Sub-labels: one per doctor
Cleanup: never
Doctors.
"""


def judgment(decision="delete", ended_days_ago=None, recheck_after=None):
    ended_on = TODAY - timedelta(days=ended_days_ago) if ended_days_ago is not None else None
    return sublabel_cleanup.Judgment(
        decision=decision, reason="Trip is over.", ended_on=ended_on, recheck_after=recheck_after
    )


class FakeGmail:
    """Labels as {id: (name, threads, [days ago of each email, newest first])}."""

    def __init__(self, labels):
        self.labels = labels
        self.deleted = []
        self.patcher = mock.patch.multiple(
            sublabel_cleanup.gmail,
            list_labels=lambda: [{"id": i, "name": name} for i, (name, _, _) in self.labels.items()],
            get_label=lambda i: {"id": i, "threadsTotal": self.labels[i][1]},
            # A message id is its label and age, so a new email gets a new id.
            list_label_ids=lambda i, limit=500: [f"{i}:{days}" for days in self.labels[i][2]][:limit],
            get_summary=self.summary,
            delete_label=self.deleted.append,
        )

    def summary(self, message_id):
        days_ago = int(message_id.split(":")[1])
        return {"id": message_id, "date": NOW - timedelta(days=days_ago), "sender": "s", "subject": "x", "snippet": ""}

    def __enter__(self):
        self.patcher.start()
        return self

    def __exit__(self, *exc):
        self.patcher.stop()


class SublabelCleanupTests(TestCase):
    def setUp(self):
        token = run_context._current.set(run_context.RunContext(None, labels=labels.parse(CLEANUP_MD)))
        self.addCleanup(run_context._current.reset, token)

    def test_never_deletes_anything_but_a_sub_label(self):
        with FakeGmail({}) as fake:
            for name in ("Travel", "Travel/Rome/Hotel"):
                with self.assertRaises(ValueError):
                    sublabel_cleanup.delete_sublabel("L1", name)
            self.assertEqual(fake.deleted, [])

    def test_only_sub_labels_under_described_parents_are_eligible(self):
        gmail_labels = [{"id": str(i), "name": name} for i, name in enumerate(
            ["Travel", "Travel/Rome", "Travel/Rome/Hotel", "Medical/Dr Weber", "Work/Launch"]
        )]
        self.assertEqual([l["name"] for l in sublabel_cleanup.eligible(gmail_labels)], ["Travel/Rome"])

    def test_candidates(self):
        def sub(threads, *days_ago):
            return sublabel_cleanup.Sublabel(
                "L1", "Travel/Rome", "Travel", threads, [],
                NOW - timedelta(days=days_ago[0]) if days_ago else None,
            )
        self.assertFalse(sublabel_cleanup.is_candidate(sub(1, 10), NOW))  # grace period
        self.assertTrue(sublabel_cleanup.is_candidate(sub(1, 20), NOW))
        self.assertFalse(sublabel_cleanup.is_candidate(sub(3, 30), NOW))
        self.assertTrue(sublabel_cleanup.is_candidate(sub(3, 50), NOW))
        self.assertTrue(sublabel_cleanup.is_candidate(sub(0), NOW))  # empty

    def test_grace_period_after_it_ended_beats_the_model(self):
        self.assertEqual(
            sublabel_cleanup.settle(judgment("delete", ended_days_ago=5), TODAY),
            ("keep", TODAY + timedelta(days=9)),
        )
        self.assertEqual(sublabel_cleanup.settle(judgment("delete", ended_days_ago=20), TODAY), ("delete", None))
        self.assertEqual(sublabel_cleanup.settle(judgment("keep"), TODAY), ("keep", TODAY + timedelta(days=30)))
        early = judgment("keep", ended_days_ago=10, recheck_after=TODAY + timedelta(days=1))
        self.assertEqual(sublabel_cleanup.settle(early, TODAY), ("keep", TODAY + timedelta(days=4)))

    def test_cleanup_hints_stay_out_of_the_classifier_prompt(self):
        self.assertEqual(labels.cleanup_hint("✈️ Travel"), "after the trip is over")
        self.assertNotIn("after the trip", labels.describe("Travel"))

    def test_dry_run_reports_and_apply_deletes(self):
        gmail_labels = {
            "T": ("Travel", 9, [1]),
            "R": ("Travel/Rome", 3, [60, 70]),      # candidate
            "L": ("Travel/Lisbon", 3, [5, 60]),     # recent email: never sent to the model
            "W": ("Medical/Dr Weber", 1, [90]),     # Cleanup: never
        }
        with FakeGmail(gmail_labels) as fake, mock.patch.object(
            sublabel_cleanup, "judge", return_value=judgment(ended_days_ago=60)
        ) as judge:
            dry = sublabel_cleanup.run(apply=False)
            self.assertEqual([(r.label_name, r.decision) for r in dry], [("Travel/Rome", "would_delete")])
            self.assertEqual(fake.deleted, [])
            applied = sublabel_cleanup.run(apply=True)
            self.assertEqual([(r.label_name, r.decision) for r in applied], [("Travel/Rome", "delete")])
            self.assertEqual(fake.deleted, ["R"])
            self.assertEqual([call.args[0].name for call in judge.call_args_list], ["Travel/Rome", "Travel/Rome"])

    def test_new_email_during_review_cancels_the_delete(self):
        fake = FakeGmail({"R": ("Travel/Rome", 3, [60])})

        def judge_while_an_email_arrives(sub, today):
            fake.labels["R"] = ("Travel/Rome", 4, [0, 60])
            return judgment(ended_days_ago=60)

        with fake, mock.patch.object(sublabel_cleanup, "judge", judge_while_an_email_arrives):
            reviews = sublabel_cleanup.run(apply=True)
        self.assertEqual(reviews[0].decision, "keep")
        self.assertEqual(fake.deleted, [])


@mock.patch.object(runner.google_connect, "credentials", return_value=None)
class CleanupRunnerTests(TestCase):
    def setUp(self):
        self.user = make_connected_user()
        Label.objects.create(user=self.user, name="Travel", sublabels="one per trip", cleanup="after the trip is over")
        self.fake = FakeGmail({"R": ("Travel/Rome", 3, [60]), "P": ("Travel/Paris", 3, [50])})
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__)

    def test_saves_reviews_and_skips_kept_ones_until_their_recheck(self, _):
        decisions = {"Travel/Rome": judgment("delete", ended_days_ago=60), "Travel/Paris": judgment("keep")}
        with mock.patch.object(sublabel_cleanup, "judge", lambda sub, today: decisions[sub.name]):
            cleanup_runner.run_user(self.user)
        self.user.google.refresh_from_db()
        self.assertEqual(self.user.google.last_cleanup_error, "")
        self.assertEqual(
            sorted(self.user.sublabel_reviews.values_list("label_name", "decision")),
            [("Travel/Paris", "keep"), ("Travel/Rome", "would_delete")],  # cleanup_enabled is off
        )
        self.assertEqual(cleanup_runner.last_result(self.user), {"removed": 0, "would_remove": 1})

        self.user.cleanup_enabled = True
        judged = []
        with mock.patch.object(sublabel_cleanup, "judge", lambda sub, today: judged.append(sub.name) or decisions[sub.name]):
            cleanup_runner.run_user(self.user)
        self.assertEqual(judged, ["Travel/Rome"])  # Paris waits for its recheck date
        self.assertEqual(self.fake.deleted, ["R"])
        self.assertEqual(cleanup_runner.last_result(self.user), {"removed": 1, "would_remove": 0})

    def test_due_monthly_after_connecting_or_the_last_cleanup(self, _):
        connection = self.user.google
        self.assertFalse(cleanup_runner.is_due(self.user))
        connection.connected_at = NOW - timedelta(days=31)
        self.assertTrue(cleanup_runner.is_due(self.user))
        connection.last_cleanup_at = NOW - timedelta(days=3)
        self.assertFalse(cleanup_runner.is_due(self.user))

    def test_button_requests_a_cleanup_and_dashboard_shows_the_count(self, _):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("dashboard")), "Not yet")
        self.client.post(reverse("cleanup_now"))
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.cleanup_requested_at)

        connection = self.user.google
        connection.last_cleanup_at = NOW
        connection.save()
        for name in ("Travel/Rome", "Travel/Paris"):
            SublabelReview.objects.create(
                user=self.user, label_id=name, label_name=name, decision="delete", reviewed_at=NOW
            )
        self.assertContains(self.client.get(reverse("dashboard")), "2 sub-labels removed")

    def test_button_needs_a_connection(self, _):
        user = User.objects.create_user("b@example.com", "a-long-password")
        self.client.force_login(user)
        self.client.post(reverse("cleanup_now"))
        user.refresh_from_db()
        self.assertIsNone(user.cleanup_requested_at)


class FakeClaude(GenericFakeChatModel):
    """A chat model that replays scripted messages and records how tools were bound."""

    bound: list = []

    def bind_tools(self, tools, **kwargs):
        self.bound.append(kwargs)
        return self


def tool_call(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call-{name}"}])


EMAIL = pipeline.Email(id="m1", thread_id="t1", sender="Bob <bob@example.com>", to="a@example.com", cc="", subject="Hi", body="Hello")
RESULT = SimpleNamespace(model_dump_json=lambda indent=None: "{}")


class LangChainAgentTests(SimpleTestCase):
    def test_tools_are_the_three_actions_with_argument_descriptions(self):
        self.assertEqual(
            [t.name for t in actions.TOOLS],
            ["apply_label", "block_time", "find_free_slots"],
        )
        for t in actions.TOOLS:
            for name, schema in t.args.items():
                self.assertTrue(schema.get("description"), f"{t.name}.{name} has no description")

    def test_act_runs_a_tool_in_the_users_context_one_at_a_time(self):
        seen = []

        def take_note(note: str) -> str:
            """Take a note about the email."""
            seen.append((note, run_context.current()))
            return "noted"

        fake = FakeClaude(messages=iter([tool_call("take_note", {"note": "Bob says hi"}), AIMessage(content="Noted.")]), bound=[])
        ctx = run_context.RunContext(None)
        with mock.patch.object(agent, "act_model", lambda: fake), mock.patch.object(
            actions, "TOOLS", [tool(take_note)]
        ), run_context.use(ctx), mock.patch("builtins.print") as printed:
            agent.act(EMAIL, RESULT, [])
        self.assertEqual(seen, [("Bob says hi", ctx)])
        self.assertTrue(fake.bound and all(kwargs["parallel_tool_calls"] is False for kwargs in fake.bound))
        printed.assert_called_with("[agent]", "Noted.")

    def test_act_stops_after_max_steps(self):
        calls = []

        def take_note(note: str) -> str:
            """Take a note about the email."""
            calls.append(note)
            return "noted"

        endless = (tool_call("take_note", {"note": str(n)}) for n in range(100))
        fake = FakeClaude(messages=endless, bound=[])
        with mock.patch.object(agent, "act_model", lambda: fake), mock.patch.object(
            actions, "TOOLS", [tool(take_note)]
        ), run_context.use(run_context.RunContext(None)), mock.patch("builtins.print"):
            agent.act(EMAIL, RESULT, [])
        self.assertEqual(len(calls), agent.MAX_STEPS)

    def test_ask_returns_the_parsed_result_and_raises_on_refusal(self):
        answer = {"raw": AIMessage(content="", response_metadata={"stop_reason": "end_turn"}), "parsed": "parsed", "parsing_error": None}
        with mock.patch.object(pipeline, "chat_model") as chat:
            structured = chat.return_value.with_structured_output.return_value
            structured.invoke.return_value = answer
            self.assertEqual(pipeline.ask("system", "user", pipeline.Reply, max_tokens=10), "parsed")
            chat.return_value.with_structured_output.assert_called_once_with(pipeline.Reply, method="json_schema", include_raw=True)

            answer["raw"] = AIMessage(content="", response_metadata={"stop_reason": "refusal"})
            with self.assertRaisesMessage(RuntimeError, "Model refused"):
                pipeline.ask("system", "user", pipeline.Reply, max_tokens=10)


class BlockTimeAgeTests(SimpleTestCase):
    """Old emails, as reached by rerunning the agent over the inbox, get no calendar block."""

    def block(self, days_old):
        received = datetime.now(timezone.utc) - timedelta(days=days_old)
        with mock.patch.object(actions.gmail, "received_at", return_value=received), \
                mock.patch.object(actions.gcal, "find_event_for_message", return_value=None), \
                mock.patch.object(actions.gcal, "reply_slot", return_value=None) as reply_slot, \
                mock.patch("builtins.print"):
            outcome = actions.block_time("m1", "Reply to Bob", 15, "notes", urgent=True)
        return outcome, reply_slot

    def test_email_older_than_five_days_is_not_blocked(self):
        outcome, reply_slot = self.block(days_old=6)
        self.assertEqual(outcome, "not blocked: the email is 6 days old, older than 5 days")
        reply_slot.assert_not_called()

    def test_recent_email_still_looks_for_a_slot(self):
        _, reply_slot = self.block(days_old=4)
        reply_slot.assert_called_once_with(15, urgent=True)


PUSH_SETTINGS = {"GMAIL_PUBSUB_TOPIC": "projects/p/topics/gmail", "GMAIL_PUSH_TOKEN": "secret"}


def push_body(address):
    data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": "123"}).encode()).decode()
    return json.dumps({"message": {"data": data, "messageId": "1"}, "subscription": "projects/p/subscriptions/s"})


@override_settings(**PUSH_SETTINGS)
class GmailPushWebhookTests(TestCase):
    def setUp(self):
        self.user = make_connected_user("ada@example.com")

    def post(self, body, token="secret"):
        return self.client.post(f"/gmail/push/?token={token}", body, content_type="application/json")

    def test_marks_the_connection_of_the_notified_address(self):
        self.assertEqual(self.post(push_body("Ada@Example.com")).status_code, 204)
        self.user.google.refresh_from_db()
        self.assertIsNotNone(self.user.google.push_received_at)

    def test_rejects_a_wrong_token(self):
        self.assertEqual(self.post(push_body("ada@example.com"), token="nope").status_code, 403)
        self.user.google.refresh_from_db()
        self.assertIsNone(self.user.google.push_received_at)

    def test_acknowledges_malformed_and_unknown_notifications(self):
        self.assertEqual(self.post("not json").status_code, 204)
        self.assertEqual(self.post(json.dumps({"message": {"data": "!!"}})).status_code, 204)
        self.assertEqual(self.post(push_body("someone@else.com")).status_code, 204)
        self.user.google.refresh_from_db()
        self.assertIsNone(self.user.google.push_received_at)

    def test_off_without_settings(self):
        with self.settings(GMAIL_PUBSUB_TOPIC="", GMAIL_PUSH_TOKEN=""):
            self.assertEqual(self.post(push_body("ada@example.com"), token="").status_code, 404)

    def test_dashboard_says_how_new_emails_are_handled(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("dashboard")), "Poll · every 5 minutes")
        GoogleConnection.objects.update(watch_expires_at=django_timezone.now() + timedelta(days=7))
        self.assertContains(self.client.get(reverse("dashboard")), "Push · within seconds")


@override_settings(**PUSH_SETTINGS)
class GmailPushSchedulerTests(TestCase):
    def setUp(self):
        self.user = make_connected_user()
        self.ran = []
        patcher = mock.patch.object(
            run_scheduler.runner, "run_user", lambda user, rerun_newest=0: self.ran.append((user.email, rerun_newest))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_agents(self, poll):
        self.ran.clear()
        run_scheduler.Command(stdout=StringIO()).run_agents(poll)
        return self.ran

    def watching(self, last_run_ago=timedelta(minutes=1)):
        GoogleConnection.objects.update(
            watch_expires_at=django_timezone.now() + timedelta(days=7), last_run_at=django_timezone.now() - last_run_ago
        )

    def test_push_runs_the_user_once(self):
        self.watching()
        GoogleConnection.objects.update(push_received_at=django_timezone.now())
        self.assertEqual(self.run_agents(poll=False), [("a@example.com", 0)])
        self.assertEqual(self.run_agents(poll=False), [])

    def test_watched_users_are_polled_only_as_a_fallback(self):
        self.watching()
        self.assertEqual(self.run_agents(poll=True), [])
        self.watching(last_run_ago=push.FALLBACK_POLL)
        self.assertEqual(self.run_agents(poll=True), [("a@example.com", 0)])

    def test_unwatched_users_are_polled_every_time(self):
        GoogleConnection.objects.update(last_run_at=django_timezone.now())
        self.assertEqual(self.run_agents(poll=True), [("a@example.com", 0)])
        with self.settings(GMAIL_PUBSUB_TOPIC="", GMAIL_PUSH_TOKEN=""):
            self.watching()
            self.assertEqual(self.run_agents(poll=True), [("a@example.com", 0)])

    def test_push_is_ignored_while_the_agent_is_off(self):
        User.objects.update(agent_enabled=False)
        GoogleConnection.objects.update(push_received_at=django_timezone.now())
        self.assertEqual(self.run_agents(poll=True), [])

class RunnerKeepsPushTests(TestCase):
    def test_a_run_does_not_clear_a_push_that_arrives_during_it(self):
        user = make_connected_user()
        with mock.patch.object(runner, "_process_emails", lambda *a: push.mark_pushed("a@example.com")), \
                mock.patch.object(runner.google_connect, "credentials", return_value=None):
            runner.run_user(User.objects.select_related("google").get(pk=user.pk))
        self.assertIsNotNone(GoogleConnection.objects.get().push_received_at)



@override_settings(**PUSH_SETTINGS)
@mock.patch.object(runner.google_connect, "credentials", return_value=None)
class GmailWatchRenewalTests(TestCase):
    def setUp(self):
        self.user = make_connected_user()

    def test_starts_and_renews_daily(self, _):
        expiration = datetime(2030, 1, 8, tzinfo=timezone.utc)
        with mock.patch.object(push.gmail, "watch_inbox", return_value={
            "historyId": "1", "expiration": str(int(expiration.timestamp() * 1000)),
        }) as watch:
            push.renew_watches()
            push.renew_watches()  # not due again yet
        watch.assert_called_once_with("projects/p/topics/gmail")
        connection = GoogleConnection.objects.get()
        self.assertEqual(connection.watch_expires_at, expiration)
        self.assertAlmostEqual(connection.watch_renew_at - django_timezone.now(), push.RENEW_EVERY, delta=timedelta(minutes=1))

    def test_failure_is_shown_and_retried_in_an_hour(self, _):
        GoogleConnection.objects.update(watch_expires_at=django_timezone.now() + timedelta(days=6))
        with mock.patch.object(push.gmail, "watch_inbox", side_effect=RuntimeError("topic not found")):
            push.renew_watches()
        connection = GoogleConnection.objects.get()
        self.assertIn("topic not found", connection.watch_error)
        self.assertIsNone(connection.watch_expires_at)  # back to polling every 5 minutes
        self.assertAlmostEqual(connection.watch_renew_at - django_timezone.now(), push.RETRY_AFTER, delta=timedelta(minutes=1))

    def test_nothing_without_settings(self, _):
        with self.settings(GMAIL_PUBSUB_TOPIC=""), mock.patch.object(push.gmail, "watch_inbox") as watch:
            push.renew_watches()
        watch.assert_not_called()

    def test_disconnect_stops_the_watch(self, _):
        GoogleConnection.objects.update(watch_expires_at=django_timezone.now() + timedelta(days=7))
        self.client.force_login(self.user)
        with mock.patch.object(push.gmail, "stop_watch") as stop, mock.patch.object(google_connect, "revoke"):
            self.client.post(reverse("google_disconnect"))
        stop.assert_called_once()
        self.assertFalse(GoogleConnection.objects.exists())


@mock.patch.object(runner.google_connect, "credentials", return_value=None)
class ReconnectReminderTests(TestCase):
    def setUp(self):
        self.user = make_connected_user()
        self.user.first_name = "Ada"
        self.user.save()
        patcher = mock.patch.object(reminder.gmail, "insert_into_inbox", return_value="r1")
        self.insert = patcher.start()
        self.addCleanup(patcher.stop)

    def authorized(self, ago):
        GoogleConnection.objects.update(authorized_at=django_timezone.now() - ago)

    def test_not_sent_before_day_six(self, _):
        self.authorized(timedelta(days=5, hours=23))
        reminder.send_due()
        self.insert.assert_not_called()

    def test_sent_once_on_day_six_and_skipped_by_the_agent(self, _):
        self.authorized(timedelta(days=6))
        reminder.send_due()
        reminder.send_due()  # already sent
        self.insert.assert_called_once()
        subject, body = self.insert.call_args.args
        self.assertEqual(subject, reminder.SUBJECT)
        self.assertIn("Hi Ada,", body)
        self.assertIn("http://localhost:8000/google/connect/", body)
        self.assertIsNotNone(GoogleConnection.objects.get().reconnect_reminder_sent_at)
        self.assertEqual(self.user.processed_emails.get(message_id="r1").status, ProcessedEmail.Status.SKIPPED)

    def test_reconnecting_during_the_reminder_keeps_the_new_grants_reminder(self, _):
        self.authorized(timedelta(days=6))

        def reconnect_meanwhile(subject, body):
            GoogleConnection.objects.update(authorized_at=django_timezone.now(), reconnect_reminder_sent_at=None)
            return "r1"

        self.insert.side_effect = reconnect_meanwhile
        reminder.send_due()
        self.assertIsNone(GoogleConnection.objects.get().reconnect_reminder_sent_at)

    def test_reconnecting_during_a_failed_reminder_stays_connected(self, _):
        self.authorized(timedelta(days=6))

        def reconnect_then_fail(subject, body):
            GoogleConnection.objects.update(authorized_at=django_timezone.now())
            raise RefreshError("invalid_grant")

        self.insert.side_effect = reconnect_then_fail
        reminder.send_due()
        self.assertEqual(GoogleConnection.objects.get().status, GoogleConnection.Status.CONNECTED)

    def test_not_sent_once_google_already_disconnected(self, _):
        self.authorized(timedelta(days=8))
        GoogleConnection.objects.update(status=GoogleConnection.Status.RECONNECT_NEEDED)
        reminder.send_due()
        self.insert.assert_not_called()

    def test_expired_access_shows_the_banner_instead(self, _):
        self.authorized(timedelta(days=6))
        self.insert.side_effect = RefreshError("invalid_grant")
        reminder.send_due()
        connection = GoogleConnection.objects.get()
        self.assertEqual(connection.status, GoogleConnection.Status.RECONNECT_NEEDED)
        self.assertIsNone(connection.reconnect_reminder_sent_at)

    def test_other_failures_retry_on_the_next_poll(self, _):
        self.authorized(timedelta(days=6))
        self.insert.side_effect = RuntimeError("Gmail is down")
        reminder.send_due()
        self.assertIsNone(GoogleConnection.objects.get().reconnect_reminder_sent_at)
        self.insert.side_effect, self.insert.return_value = None, "r2"
        reminder.send_due()
        self.assertIsNotNone(GoogleConnection.objects.get().reconnect_reminder_sent_at)

    def test_link_uses_the_public_host(self, _):
        with self.settings(APP_HOST="agent.example.com"):
            self.assertEqual(reminder.reconnect_url(), "https://agent.example.com/google/connect/")

    def test_reconnecting_starts_a_new_seven_days(self, _):
        self.authorized(timedelta(days=6))
        reminder.send_due()
        self.client.force_login(self.user)
        session = self.client.session
        session["google_oauth"] = {"state": "s", "code_verifier": "v"}
        session.save()
        creds = SimpleNamespace(refresh_token="new-refresh")
        with mock.patch.object(google_connect, "exchange_code", return_value=creds), \
                mock.patch.object(google_connect, "has_all_scopes", return_value=True), \
                mock.patch.object(google_connect, "account_email", return_value="a@example.com"):
            self.client.get(reverse("google_callback"), {"state": "s", "code": "c"})
        connection = GoogleConnection.objects.get()
        self.assertIsNone(connection.reconnect_reminder_sent_at)
        self.assertAlmostEqual(connection.authorized_at, django_timezone.now(), delta=timedelta(minutes=1))

    def test_dashboard_offers_reconnect_after_the_reminder(self, _):
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(reverse("dashboard")), "Reconnect Google")
        GoogleConnection.objects.update(reconnect_reminder_sent_at=django_timezone.now())
        self.assertContains(self.client.get(reverse("dashboard")), "Reconnect Google")


class GmailInsertTests(SimpleTestCase):
    def test_puts_an_unread_email_to_self_in_the_inbox_without_sending(self):
        service = mock.Mock()
        service.users().messages().insert().execute.return_value = {"id": "m9"}
        with run_context.use(run_context.RunContext(None)), \
                mock.patch("triage.gmail._service", return_value=service), \
                mock.patch("triage.gmail.my_address", return_value="a@example.com"):
            from triage import gmail
            self.assertEqual(gmail.insert_into_inbox("Hello", "Body text"), "m9")
        body = service.users().messages().insert.call_args.kwargs["body"]
        self.assertEqual(body["labelIds"], ["INBOX", "UNREAD"])
        raw = base64.urlsafe_b64decode(body["raw"]).decode()
        self.assertIn("To: a@example.com", raw)
        self.assertIn("From: Inbox Agent <a@example.com>", raw)
        self.assertIn("Subject: Hello", raw)
        service.users().messages().send.assert_not_called()


class DryRunTests(SimpleTestCase):
    """Eval mode: the agent runs as usual, but nothing in Gmail or Calendar changes."""

    def setUp(self):
        self.ctx = run_context.RunContext(None, dry_run=True)
        token = run_context._current.set(self.ctx)
        self.addCleanup(run_context._current.reset, token)
        # Any call to the Google clients would be a real read or write; reads are faked below.
        self.gmail_api, self.calendar_api = mock.Mock(), mock.Mock()
        for patcher in (
            mock.patch.object(actions.gmail, "_service", return_value=self.gmail_api),
            mock.patch.object(actions.gcal, "_service", return_value=self.calendar_api),
            mock.patch.multiple(
                actions.gmail,
                list_labels=mock.Mock(return_value=[{"id": "L1", "name": "Work", "color": {"backgroundColor": "#fff"}}]),
                get_label_ids=mock.Mock(return_value=[]),
                received_at=mock.Mock(return_value=datetime.now(timezone.utc)),
                thread_has_draft=mock.Mock(return_value=False),
                my_address=mock.Mock(return_value="me@example.com"),
                get_reply_headers=mock.Mock(return_value={
                    "thread_id": "t1", "from": "ada@example.com", "subject": "Launch", "message-id": "<m1@x>",
                }),
            ),
            mock.patch.multiple(
                actions.gcal,
                find_event_for_message=mock.Mock(return_value=None),
                reply_slot=mock.Mock(return_value=((datetime(2026, 11, 23, 10, tzinfo=timezone.utc),
                                                    datetime(2026, 11, 23, 10, 15, tzinfo=timezone.utc)), None)),
                format_slot=mock.Mock(return_value="Mon 23 Nov 10:00–10:15"),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_actions_change_nothing_and_record_what_they_would_do(self):
        with mock.patch("builtins.print"):
            actions.apply_label("m1", "Work", "Launch")
            actions.block_time("m1", "Reply to Ada: Launch", 15, "From: Ada", urgent=False)
            actions.draft_reply("m1", "Thanks, Ada!")

        self.gmail_api.assert_not_called()
        self.calendar_api.assert_not_called()
        self.assertEqual([w["action"] for w in self.ctx.would_do],
                         ["create_label", "add_labels", "create_event", "create_reply_draft"])
        self.assertEqual(self.ctx.would_do[1]["labels"], ["Work", "Work/Launch"])
        self.assertEqual(self.ctx.would_do[2]["start"], "2026-11-23T10:00:00+00:00")
        self.assertEqual(self.ctx.would_do[3]["body"], "Thanks, Ada!")

    def test_writes_happen_outside_a_dry_run(self):
        self.ctx.dry_run = False
        actions.gmail.add_labels("m1", ["L1"])
        self.gmail_api.users().messages().modify.assert_called_once()
        self.assertEqual(self.ctx.would_do, [])

    def test_cli_saves_one_line_per_email(self):
        email = {"id": "m1", "thread_id": "t1", "sender": "Ada <ada@example.com>", "to": "me", "cc": "",
                 "subject": "Launch", "body": "Can we meet?"}
        result = SimpleNamespace(model_dump=lambda: {"urgency": "not_urgent"})

        def fake_run(email, label_names):
            actions.gmail.add_labels(email.id, ["L1"])
            return result

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(pipeline, "EVAL_RUNS_DIR", Path(tmp)), \
                mock.patch.object(pipeline.gmail, "list_newest", return_value=[email]), \
                mock.patch.object(pipeline, "run", side_effect=fake_run), \
                mock.patch("sys.argv", ["pipeline", "1", "--dry-run"]), \
                mock.patch("builtins.print"):
            self.ctx.dry_run = False  # the flag turns it on
            pipeline.main()
            [log] = Path(tmp).glob("*.jsonl")
            [line] = log.read_text().splitlines()

        record = json.loads(line)
        self.assertEqual(record["email"]["subject"], "Launch")
        self.assertNotIn("body", record["email"])
        self.assertEqual(record["classification"], {"urgency": "not_urgent"})
        self.assertEqual(record["would_do"], [{"action": "add_labels", "message_id": "m1", "label_ids": ["L1"], "labels": ["Work"]}])
        self.gmail_api.assert_not_called()



class EvalExportTests(SimpleTestCase):
    """Adding emails: pull new ones with variety, pre-fill with the agent, append as to-do."""

    def setUp(self):
        # Newest first: 7 GitHub emails, one from the user, then four others.
        senders = ["GitHub <notifications@github.com>"] * 7 + [
            "Me <me@example.com>", "Ada <ada@example.com>", "Ada <ada@example.com>", "Ada <ada@example.com>",
            "Bob <bob@lists.example.org>",
        ]
        self.subjects = [f"Push {i}" for i in range(7)] + ["note", "Lunch", "Lunch 2", "Trip", "News"]
        self.messages = {f"m{i}": sender for i, sender in enumerate(senders)}
        gmail_api = mock.Mock()
        for patcher in (
            mock.patch.object(eval_export.gmail, "_service", return_value=gmail_api),
            mock.patch.multiple(
                eval_export.gmail,
                my_address=mock.Mock(return_value="me@example.com"),
                list_labels=mock.Mock(return_value=[{"id": "L1", "name": "Work"}]),
                search_ids=mock.Mock(side_effect=lambda query: iter(self.messages)),
                get_summary=lambda i: {"id": i, "sender": self.messages[i], "subject": self.subjects[int(i[1:])],
                                       "date": datetime(2026, 11, 20, tzinfo=timezone.utc)},
                get_email=lambda i: {"id": i, "thread_id": f"t{i}", "sender": self.messages[i], "to": "me",
                                     "cc": "", "subject": self.subjects[int(i[1:])], "body": "b"},
            ),
            mock.patch("builtins.print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.gmail_api = gmail_api

    def test_caps_skip_own_and_near_duplicates(self):
        emails, seen = eval_export.pick(100, [])
        # GitHub capped at 5 (and its numbered subjects are near-duplicates: 1 kept), own email skipped,
        # Ada's "Lunch 2" is a near-duplicate of "Lunch".
        self.assertEqual([e["id"] for e in emails], ["m0", "m8", "m10", "m11"])
        self.assertEqual(seen["notifications@github.com"], 7)
        self.assertEqual(emails[1]["sender_emails"], 3)
        self.gmail_api.assert_not_called()  # nothing beyond the faked reads

    def test_only_new_emails_and_rules_count_the_dataset_too(self):
        dataset = [{"id": "m0", "sender": "GitHub <notifications@github.com>", "subject": "Push 0"},
                   {"id": "x", "sender": "Ada <ada@example.com>", "subject": "Hello"},
                   {"id": "y", "sender": "Ada <ada@example.com>", "subject": "Bye"}]
        emails, _ = eval_export.pick(100, dataset)
        self.assertEqual([e["id"] for e in emails], ["m11"])  # Ada already has 2; GitHub's subject is taken: only Bob
        self.assertEqual(len(eval_export.pick(1, [])[0]), 1)

    def test_prefill_uses_the_agents_live_decisions_without_sub_labels(self):
        result = SimpleNamespace(labels=[SimpleNamespace(label="Work", sublabel_needed=True, sublabel="Launch"),
                                         SimpleNamespace(label="Team", sublabel_needed=False, sublabel=None)],
                                 urgency="schedule_time", needs_reply=True)
        with mock.patch("triage.pipeline.classify", return_value=result) as classify, \
                mock.patch("triage.labels.allows_sublabel", return_value=True):
            guess = eval_export.agent_guess({"id": "m1", "thread_id": "t", "sender": "s", "to": "", "cc": "",
                                             "subject": "s", "body": "b", "date": "d"}, ["Work", "Team"],
                                            run_context.RunContext(None))
        self.assertEqual(guess, {"labels": "Work; Team", "urgency": "schedule_time", "needs_reply": "yes"})
        self.assertEqual(len(classify.call_args.args), 2)  # the live classify model, no override

    def test_new_emails_are_appended_as_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {"DATA_DIR": Path(tmp), "EMAILS_FILE": Path(tmp) / "emails.jsonl", "ANSWERS_FILE": Path(tmp) / "a.csv",
                     "LABELS_FILE": Path(tmp) / "labels.json"}
            with mock.patch.multiple(eval_export, **files), mock.patch.multiple(eval_review, ANSWERS_FILE=files["ANSWERS_FILE"]):
                old = [{"id": "old", "account": "me", "date": "d", "sender": "s", "subject": "s", "labels": "Work",
                        "urgency": "not_urgent", "needs_reply": "no", "checked": "", "sender_emails": "1", "notes": ""}]
                eval_review.write_rows(old)
                # Reviewed on the page while the export was pulling and pre-filling:
                eval_review.write_rows([{**old[0], "checked": "yes"}])
                new = [{"id": "m1", "thread_id": "t", "sender": "s", "to": "", "cc": "", "subject": "s", "body": "b",
                        "date": "2026-11-20T00:00:00", "account": "me", "sender_emails": 2}]
                eval_export.append(new, {"m1": {"labels": "Team", "urgency": "instant", "needs_reply": "yes"}})
                eval_export.append(new, {})  # the same email twice is added once
                rows = eval_review.read_rows()
                saved = files["EMAILS_FILE"].read_text().splitlines()
        self.assertEqual([(r["id"], r["checked"]) for r in rows], [("old", "yes"), ("m1", "")])  # the review survived
        self.assertEqual(len(saved), 1)
        self.assertEqual((rows[1]["labels"], rows[1]["urgency"]), ("Team", "instant"))
        self.assertEqual(set(json.loads(saved[0])), {"id", "thread_id", "sender", "to", "cc", "subject", "body", "date"})


class GmailSearchTests(SimpleTestCase):
    def test_search_follows_pages(self):
        service = mock.Mock()
        service.users().messages().list().execute.side_effect = [
            {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
            {"messages": [{"id": "c"}]},
        ]
        with run_context.use(run_context.RunContext(None)), mock.patch("triage.gmail._service", return_value=service):
            from triage import gmail
            self.assertEqual(list(gmail.search_ids("q")), ["a", "b", "c"])
        self.assertEqual(service.users().messages().list.call_args.kwargs["pageToken"], "p2")




class EvalReviewTests(SimpleTestCase):
    """The local review page's save: checked answers land in answers.csv, bad ones are refused."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.answers = Path(tmp.name) / "answers.csv"
        self.answers.write_text(
            "id,date,sender,subject,labels,urgency,needs_reply,mine\n"  # an older file, plus a hand-made column
            "m1,2026-11-20,Ada <ada@x.com>,Launch,📰 Newsletters,not_urgent,no,keep me\n"
        )
        labels_file = Path(tmp.name) / "labels.json"
        labels_file.write_text(json.dumps(["💼 Job Applications", "💼 Job Applications/Globex Application"]))
        patcher = mock.patch.multiple(eval_review, ANSWERS_FILE=self.answers, LABELS_FILE=labels_file,
                                      DESCRIPTIONS_FILE=Path(tmp.name) / "none.md")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saves_and_marks_checked(self):
        eval_review.save({"id": "m1", "labels": ["💼 Job Applications", "🔔 Alerts"],
                          "urgency": "schedule_time", "needs_reply": "yes", "notes": " tricky "})
        [row] = eval_review.read_rows()
        self.assertEqual(row["labels"], "💼 Job Applications; 🔔 Alerts")
        self.assertEqual((row["urgency"], row["needs_reply"], row["checked"], row["notes"]),
                         ("schedule_time", "yes", "yes", "tricky"))
        self.assertEqual(row["mine"], "keep me")

    def test_done_can_be_undone(self):
        answer = {"id": "m1", "labels": [], "urgency": "not_urgent", "needs_reply": "no"}
        eval_review.save(answer)
        eval_review.save({**answer, "done": False})
        self.assertEqual(eval_review.read_rows()[0]["checked"], "")

    def test_no_label_is_an_answer(self):
        eval_review.save({"id": "m1", "labels": [], "urgency": "not_urgent", "needs_reply": "no"})
        self.assertEqual(eval_review.read_rows()[0]["labels"], "")

    def test_refuses_unknown_labels_and_missing_answers(self):
        good = {"id": "m1", "labels": [], "urgency": "not_urgent", "needs_reply": "no"}
        for bad in ({"labels": ["Made Up"]}, {"labels": ["🔔 Alerts/Sub-labels are not answers"]}, {"urgency": ""},
                    {"needs_reply": "maybe"}, {"id": "nope"}):
            with self.assertRaises(ValueError):
                eval_review.save({**good, **bad})
        self.assertEqual(eval_review.read_rows()[0]["checked"], "")

    def test_page_file_exists(self):
        self.assertTrue(eval_review.PAGE.exists(), eval_review.PAGE)

    def test_label_options_merge_gmail_labels_md_and_answers(self):
        options = eval_review.label_options(eval_review.read_rows())
        self.assertIn("🔔 Alerts", options["top"])  # from labels.md
        self.assertIn("💼 Job Applications", options["top"])  # a Gmail sub-label adds only its parent
        self.assertNotIn("💼 Job Applications/Globex Application", options["top"])



class ScoringTests(SimpleTestCase):
    ROWS = [
        {"id": "a", "sender": "LinkedIn <jobs@linkedin.com>", "labels": "Jobs", "urgency": "not_urgent",
         "needs_reply": "no", "sender_emails": "30"},
        {"id": "b", "sender": "LinkedIn <jobs@linkedin.com>", "labels": "Jobs", "urgency": "not_urgent",
         "needs_reply": "no", "sender_emails": "30"},
        {"id": "c", "sender": "Ada <ada@x.com>", "labels": "", "urgency": "instant", "needs_reply": "yes", "sender_emails": "1"},
    ]

    def test_weights_share_a_senders_emails_among_its_rows(self):
        self.assertEqual(eval_scoring.weights(self.ROWS), {"a": 15.0, "b": 15.0, "c": 1.0})

    def test_a_later_export_doesnt_change_a_senders_share(self):
        rows = [{"id": "a", "sender": "x@x.com", "sender_emails": "100"}, {"id": "b", "sender": "x@x.com", "sender_emails": "1"}]
        self.assertEqual(eval_scoring.weights(rows), {"a": 50.0, "b": 50.0})  # still 100 emails in total

    def test_grade_scores_labels_without_their_sub_labels(self):
        guess = {"labels": "Jobs/Google", "urgency": "not_urgent", "needs_reply": "no"}
        self.assertTrue(eval_scoring.grade(self.ROWS[0], guess)["all"])
        self.assertFalse(eval_scoring.grade(self.ROWS[0], None)["all"])

    def test_share_can_be_weighted(self):
        marks = {"a": {"all": True}, "b": {"all": True}, "c": {"all": False}}
        self.assertAlmostEqual(eval_scoring.share(self.ROWS, marks, "all"), 200 / 3)
        self.assertAlmostEqual(eval_scoring.share(self.ROWS, marks, "all", eval_scoring.weights(self.ROWS)), 3000 / 31)


class ModelChoiceTests(SimpleTestCase):
    def test_haiku_classifies_and_sonnet_drafts(self):
        email = pipeline.Email(id="m1", thread_id="t1", sender="a", to="b", cc="", subject="s", body="b")
        with run_context.use(run_context.RunContext(None)), mock.patch.object(pipeline, "ask") as ask:
            pipeline.classify(email, ["Work"])
            pipeline.write_reply(email, SimpleNamespace(model_dump_json=lambda indent: "{}"), [])
        self.assertEqual(ask.call_args_list[0].kwargs["model"], "claude-haiku-5-5")
        self.assertEqual(ask.call_args_list[1].kwargs["model"], agent.MODEL)
        self.assertEqual(agent.MODEL, "claude-sonnet-5-5")




class CallLogTests(SimpleTestCase):
    def test_step_is_told_apart_by_the_system_prompt(self):
        self.assertEqual(eval_calls.step_of(pipeline.SYSTEM_PROMPT + "\nmore"), "classify")
        self.assertEqual(eval_calls.step_of(agent.ACT_PROMPT), "agent loop")
        with run_context.use(run_context.RunContext(None)):
            self.assertEqual(eval_calls.step_of(pipeline.load_skill("write-reply")), "draft reply")
        self.assertEqual(eval_calls.step_of("something else"), "other")

    def test_records_every_model_call_with_tokens_and_cost(self):
        reply = AIMessage(content="ok", usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                          response_metadata={"model_name": "claude-sonnet-5-5"})
        model = GenericFakeChatModel(messages=iter([reply, reply]))
        with eval_calls.recording() as log:
            model.invoke([("system", agent.ACT_PROMPT), ("human", "hi")])
            model.invoke([("system", agent.ACT_PROMPT), ("human", "again")])
        self.assertEqual([c["step"] for c in log.calls], ["agent loop", "agent loop"])
        self.assertEqual(log.calls[0]["input_tokens"], 1000)
        self.assertAlmostEqual(log.calls[0]["dollars"], 0.003)



class VolumeTests(SimpleTestCase):
    def test_month_starts_cover_full_months_then_this_one(self):
        now = datetime(2026, 11, 22, 15, tzinfo=timezone.utc)
        starts = eval_volume.month_starts(now, 2)
        self.assertEqual([s.strftime("%Y-%m-%d") for s in starts], ["2026-09-01", "2026-10-01", "2026-11-01"])

    def test_count_uses_epoch_bounds_and_skips_own_mail(self):
        start, end = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)
        with mock.patch.object(eval_volume.gmail, "search_ids", return_value=iter(["1", "2", "3"])) as search:
            self.assertEqual(eval_volume.count(start, end), 3)
        query = search.call_args.args[0]
        self.assertIn(f"after:{int(start.timestamp())}", query)
        self.assertIn("-from:me", query)

    def test_a_failed_count_is_unknown_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.multiple(eval_volume, RUNS_DIR=Path(tmp), VOLUME_FILE=Path(tmp) / "volume.json"), \
                mock.patch.object(eval_volume, "count_months", side_effect=RuntimeError("Gmail is down")), \
                mock.patch("builtins.print"):
            self.assertIsNone(eval_volume.monthly_emails())

    def test_counted_again_only_when_missing_or_a_month_old(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.multiple(eval_volume, RUNS_DIR=Path(tmp), VOLUME_FILE=Path(tmp) / "volume.json"), \
                mock.patch.object(eval_volume, "count_months", return_value=[("Oct", 500), ("Nov", 700)]) as counted, \
                mock.patch("builtins.print"):
            self.assertEqual(eval_volume.monthly_emails(), 600)
            self.assertEqual(eval_volume.monthly_emails(), 600)
            self.assertEqual(counted.call_count, 1)  # the second call used the saved count
            old = json.loads((Path(tmp) / "volume.json").read_text())
            old["counted_at"] = "2020-01-01T00:00:00"
            (Path(tmp) / "volume.json").write_text(json.dumps(old))
            eval_volume.monthly_emails()
            self.assertEqual(counted.call_count, 2)


class BenchTests(SimpleTestCase):
    ROWS = [
        {"id": "a", "subject": "News", "sender": "N <n@x.com>", "labels": "📰 Newsletters", "urgency": "not_urgent",
         "needs_reply": "no", "checked": "yes", "sender_emails": "30"},
        {"id": "b", "subject": "Hi", "sender": "Ada <ada@x.com>", "labels": "💼 Job Applications", "urgency": "schedule_time",
         "needs_reply": "yes", "checked": "yes", "sender_emails": "1"},
    ]
    EMAILS = {i: {"id": i, "thread_id": "t", "sender": "s", "to": "me", "cc": "", "subject": "s", "body": "b", "date": "x"}
              for i in ("a", "b")}

    def test_system_defaults_to_live_models_and_swaps_them_back(self):
        live = (pipeline.CLASSIFY_MODEL, pipeline.DRAFT_MODEL, agent.MODEL, pipeline.act)
        self.assertEqual(eval_bench.system(), {"classify": live[0], "draft": live[1], "agent": live[2],
                                               "loop_runs": "only for replies and calendar time",
                                               "emails_dated": "as if just received"})
        models = eval_bench.system(classify="sonnet", loop="none")
        with eval_bench.use_system(models):
            self.assertEqual(pipeline.CLASSIFY_MODEL, "claude-sonnet-5-5")
            self.assertIsNone(pipeline.act(None, None, []))  # the loop is skipped
        self.assertEqual((pipeline.CLASSIFY_MODEL, pipeline.DRAFT_MODEL, agent.MODEL, pipeline.act), live)

    def fake_run(self, email, names):
        """Like pipeline.run: one classify call and one loop call, a draft for b."""
        reply = AIMessage(content="ok", usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
                          response_metadata={"model_name": "claude-sonnet-5-5"})
        model = GenericFakeChatModel(messages=iter([reply, reply]))
        model.invoke([("system", pipeline.SYSTEM_PROMPT), ("human", "x")])
        model.invoke([("system", agent.ACT_PROMPT), ("human", "x")])
        if email.id == "b":
            run_context.current().would_do.append({"action": "create_reply_draft", "to": "ada@x.com", "cc": [], "body": "Hi Ada"})
        run_context.current().would_do.append({"action": "add_labels", "labels": ["x"]})
        return SimpleNamespace(labels=[SimpleNamespace(label="📰 Newsletters", sublabel_needed=False, sublabel=None)],
                               urgency="not_urgent", needs_reply=email.id == "b", minutes_needed=5)

    def test_run_one_records_decisions_draft_and_every_call(self):
        with mock.patch.object(eval_bench, "live_context", side_effect=lambda: run_context.RunContext(None, dry_run=True)), \
                mock.patch.object(eval_bench.pipeline, "run", side_effect=self.fake_run):
            eval_bench._local.__dict__.clear()
            recs = [eval_bench.run_one(row, self.EMAILS[row["id"]], ["📰 Newsletters"]) for row in self.ROWS]
        self.assertEqual([c["step"] for c in recs[0]["calls"]], ["classify", "agent loop"])
        self.assertAlmostEqual(recs[0]["dollars"], 2 * 0.003)
        self.assertTrue(recs[0]["correct"]["all"])
        self.assertIsNone(recs[0]["draft"])
        self.assertEqual(recs[1]["draft"]["body"], "Hi Ada")
        self.assertEqual([a["action"] for a in recs[1]["actions"]], ["add_labels"])
        self.assertFalse(recs[1]["correct"]["labels"])

        summary = eval_bench.summarize("current", eval_bench.system(), recs, self.ROWS, 600)
        self.assertEqual((summary["all_right"], summary["replies_caught"], summary["drafts"]), (50, 100, 1))
        self.assertAlmostEqual(summary["dollars_agent_loop"], 0.003)
        self.assertAlmostEqual(summary["dollars_per_month"], 600 * 0.006)
        self.assertIn("about $3.60 a month", eval_bench.cost_breakdown(summary))
        self.assertAlmostEqual(summary["emails_per_dollar"], 1 / 0.006)
        self.assertIn("$1 handles about 167 emails", eval_bench.cost_breakdown(summary))
        old_run = {k: v for k, v in summary.items() if k != "emails_per_dollar"}
        self.assertIn("167", eval_bench.table([old_run]))  # computed for runs saved before the metric

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(eval_bench, "BENCH_DIR", Path(tmp)), \
                mock.patch.object(eval_bench, "LEADERBOARD", Path(tmp) / "leaderboard.csv"):
            for stamp, name in (("20261123-000001", "current"), ("20261123-000002", "current"), ("20261123-000003", "no-loop")):
                eval_bench.save({**summary, "name": name, "run": stamp}, recs, stamp)
            self.assertEqual([s["run"] for s in eval_bench.load_runs()], ["20261123-000001", "20261123-000002", "20261123-000003"])
            self.assertEqual(len(list(csv.DictReader((Path(tmp) / "leaderboard.csv").open()))), 3)
            self.assertEqual(eval_bench.find_run("current"), "20261123-000002-current")  # newest of that name
            with self.assertRaises(SystemExit):
                eval_bench.find_run("nope")


class SkipIdleLoopTests(SimpleTestCase):
    EMAIL = pipeline.Email(id="m1", thread_id="t", sender="s", to="t", cc="", subject="s", body="b")

    def run_with(self, urgency, needs_reply, skip=True):
        result = SimpleNamespace(urgency=urgency, needs_reply=needs_reply, model_dump_json=lambda indent: "{}")
        with mock.patch.object(pipeline, "classify", return_value=result), \
                mock.patch.object(pipeline, "run_required", return_value=[]), \
                mock.patch.object(pipeline, "act") as act, \
                mock.patch.object(pipeline, "SKIP_IDLE_LOOP", skip), mock.patch("builtins.print"):
            pipeline.run(self.EMAIL, ["Work"])
        return act.called

    def test_loop_only_runs_when_a_reply_or_calendar_time_is_needed(self):
        self.assertFalse(self.run_with("not_urgent", False))  # newsletter, receipt
        self.assertTrue(self.run_with("not_urgent", True))
        self.assertTrue(self.run_with("schedule_time", False))
        self.assertTrue(self.run_with("instant", False))

    def test_old_behaviour_runs_it_on_every_email(self):
        self.assertTrue(self.run_with("not_urgent", False, skip=False))

    def test_bench_can_measure_both(self):
        self.assertEqual(eval_bench.system()["loop_runs"], "only for replies and calendar time")
        always = eval_bench.system(loop_always=True)
        with eval_bench.use_system(always):
            self.assertFalse(pipeline.SKIP_IDLE_LOOP)
        self.assertTrue(pipeline.SKIP_IDLE_LOOP)


class AsIfNewTests(SimpleTestCase):
    """Evals treat old test emails as just received, so the calendar step runs as live."""

    def block(self, as_if_new):
        slot = (datetime(2026, 11, 23, 10, tzinfo=timezone.utc), datetime(2026, 11, 23, 10, 15, tzinfo=timezone.utc))
        with run_context.use(run_context.RunContext(None, dry_run=True, as_if_new=as_if_new)), \
                mock.patch.object(actions.gmail, "received_at", return_value=datetime.now(timezone.utc) - timedelta(days=12)), \
                mock.patch.object(actions.gmail, "message_url", return_value="u"), \
                mock.patch.object(actions.gcal, "find_event_for_message",  # a block from an earlier live run
                                  side_effect=lambda message_id, real=True: {"status": "confirmed"} if real else None), \
                mock.patch.object(actions.gcal, "reply_slot", return_value=(slot, None)), \
                mock.patch.object(actions.gcal, "format_slot", return_value="Mon 10:00"), \
                mock.patch("builtins.print"):
            return actions.block_time("m1", "Reply", 15, "notes")

    def test_live_refuses_old_emails(self):
        self.assertIn("12 days old", self.block(as_if_new=False))

    def test_eval_blocks_them_and_ignores_earlier_blocks(self):
        self.assertEqual(self.block(as_if_new=True), "blocked Mon 10:00")

    def test_eval_contexts_use_it(self):
        with mock.patch.object(eval_calls.google_auth, "credentials", return_value=None):
            self.assertTrue(eval_calls.live_context().as_if_new)
        self.assertEqual(eval_bench.system()["emails_dated"], "as if just received")


class BenchSafetyTests(SimpleTestCase):
    def test_a_step_no_run_measured_makes_the_estimate_a_lower_bound(self):
        no_loop = {"run": "r1", "folder": "r1-no-loop", "emails": 1, "errors": 0, "total_dollars": 0.001,
                   "model_classify": "claude-haiku-5-5", "model_draft": "claude-sonnet-5-5", "model_agent": "none",
                   "model_loop_runs": "never", "dollars_per_email": 0.001}
        record = {"calls": [{"step": "classify", "input_tokens": 1000, "output_tokens": 100}]}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "r1-no-loop").mkdir()
            (Path(tmp) / "r1-no-loop" / "results.jsonl").write_text(json.dumps(record))
            with mock.patch.object(eval_bench, "BENCH_DIR", Path(tmp)), \
                    mock.patch.object(eval_bench, "load_runs", return_value=[no_loop]):
                text = eval_bench.estimate(eval_bench.system(), [{"needs_reply": "yes", "urgency": "instant"}])
        self.assertTrue(text.startswith("at least $"), text)
        self.assertIn("agent loop", text)

    def test_estimate_ignores_runs_that_failed(self):
        models = eval_bench.system()
        failed = {"run": "r1", "emails": 175, "errors": 175, "total_dollars": 0, "dollars_per_email": 0,
                  **{f"model_{k}": v for k, v in models.items()}}
        with mock.patch.object(eval_bench, "load_runs", return_value=[failed]), \
                mock.patch.object(eval_bench, "RUNS_DIR", Path(tempfile.mkdtemp())):
            self.assertNotIn("from run r1", eval_bench.estimate(models, [{"needs_reply": "no", "urgency": "not_urgent"}]))

    def test_yes_variants_start_and_a_fully_failed_run_is_not_saved(self):
        failing = lambda row, email, names: {"error": "401 invalid x-api-key", "dollars": 0}  # noqa: E731
        with mock.patch.object(eval_bench, "run_one", side_effect=failing), \
                mock.patch.object(eval_bench, "load_emails", return_value={"a": {}}), \
                mock.patch.object(eval_bench, "label_names", return_value=[]), \
                mock.patch.object(eval_bench.review, "read_rows", return_value=[{"id": "a", "checked": "yes", "needs_reply": "no", "urgency": "not_urgent"}]), \
                mock.patch.object(eval_bench, "save") as save, \
                mock.patch("builtins.input", return_value="yy"), mock.patch("builtins.print"), \
                mock.patch("sys.argv", ["bench"]), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as stop:
                eval_bench.main()
        self.assertIn("invalid x-api-key", str(stop.exception))
        save.assert_not_called()



class CredentialTests(SimpleTestCase):
    def anthropic_client(self, env):
        from triage import llm
        with mock.patch.dict("os.environ", env, clear=False):
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                if name not in env:
                    os.environ.pop(name, None)
            return llm.chat_model("claude-haiku-5-5", 10)._client

    def test_api_key_is_used_as_usual(self):
        client = self.anthropic_client({"ANTHROPIC_API_KEY": "sk-ant-api-test"})
        self.assertEqual(client.api_key, "sk-ant-api-test")

    def test_bearer_token_goes_in_the_authorization_header(self):
        client = self.anthropic_client({"ANTHROPIC_AUTH_TOKEN": "sk-ant-usr-test"})
        self.assertEqual(client._custom_headers.get("Authorization"), "Bearer sk-ant-usr-test")
        self.assertFalse(client.api_key)


    def test_token_wins_over_a_stale_exported_key(self):
        client = self.anthropic_client({"ANTHROPIC_API_KEY": "sk-ant-api-old", "ANTHROPIC_AUTH_TOKEN": "sk-ant-usr-new"})
        self.assertEqual(client._custom_headers.get("Authorization"), "Bearer sk-ant-usr-new")
        self.assertFalse(client.api_key)

    def test_a_bearer_token_in_the_api_key_variable_is_sent_as_one(self):
        client = self.anthropic_client({"ANTHROPIC_API_KEY": "sk-ant-usr-x"})
        self.assertEqual(client._custom_headers.get("Authorization"), "Bearer sk-ant-usr-x")



class BenchReviewTests(SimpleTestCase):
    """The prod flag and the two-run comparison page's data."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        live = eval_bench.system()
        def record(i, labels, draft=None):
            return json.dumps({"id": i, "subject": f"s{i}", "sender": "x", "expected": {}, "error": None,
                               "predicted": {"labels": labels, "urgency": "not_urgent", "needs_reply": "no", "minutes_needed": 5},
                               "correct": {}, "draft": draft, "actions": [], "calls": [], "dollars": 0.001, "seconds": 1})
        for folder, models, labels in (("20261123-000001-old", {**live, "loop_runs": "every email"}, "News"),
                                       ("20261123-000002-current", live, "Jobs")):
            (self.dir / folder).mkdir()
            (self.dir / folder / "summary.json").write_text(json.dumps({
                "name": folder.split("-", 2)[2], "run": folder[:15], "emails": 1, "errors": 0,
                **{f"model_{k}": v for k, v in models.items()}, "dollars_per_email_real_inbox": 0.002}))
            (self.dir / folder / "results.jsonl").write_text(record("a", labels))
        answers = [{"id": "a", "sender": "x <x@x.com>", "sender_emails": "1", "labels": "Jobs", "urgency": "not_urgent",
                    "needs_reply": "no", "notes": "", "date": "d", "account": "me"}]
        for patcher in (
            mock.patch.multiple(eval_bench, BENCH_DIR=self.dir, PROD_FILE=self.dir / "prod.json"),
            mock.patch.object(eval_bench_review, "BENCH_DIR", self.dir),
            mock.patch.object(eval_bench_review, "EMAILS_FILE", self.dir / "none.jsonl"),
            mock.patch.object(eval_bench_review.review, "read_rows", return_value=answers),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_prod_flag_by_name_and_tags(self):
        self.assertIsNone(eval_bench.prod_run())
        self.assertEqual(eval_bench.set_prod("old"), "20261123-000001-old")
        tags = {s["name"]: eval_bench.tags(s) for s in eval_bench.load_runs()}
        self.assertEqual(tags, {"old": "prod", "current": "live code"})
        self.assertIn("prod", eval_bench.table(eval_bench.load_runs()))
        with self.assertRaises(SystemExit):
            eval_bench.set_prod("nope")

    def test_compare_grades_both_runs_against_your_answers_now(self):
        emails = eval_bench_review.compare("20261123-000001-old", "20261123-000002-current")["emails"]
        self.assertEqual(len(emails), 1)
        self.assertEqual(emails[0]["answer"]["labels"], "Jobs")
        self.assertFalse(emails[0]["a"]["correct"]["labels"])
        self.assertTrue(emails[0]["b"]["correct"]["labels"])

    def test_scores_use_your_answers_now_like_the_rows(self):
        scores = {r["name"]: r["all_right"] for r in eval_bench_review.runs()}
        self.assertEqual(scores, {"old": 0, "current": 100})  # the answer is "Jobs"

    def test_only_saved_runs_can_be_read(self):
        with self.assertRaises(ValueError):
            eval_bench_review.results("../../eval_data")

    def test_runs_list_newest_first_and_page_exists(self):
        self.assertEqual([r["name"] for r in eval_bench_review.runs()], ["current", "old"])
        self.assertTrue(eval_bench_review.PAGE.exists())



class DryRunMemoryTests(SimpleTestCase):
    """A dry run sees its own simulated writes, as a live run would see the real ones."""

    def setUp(self):
        self.ctx = run_context.RunContext(None, dry_run=True)
        token = run_context._current.set(self.ctx)
        self.addCleanup(run_context._current.reset, token)
        api = mock.Mock()
        api.users().labels().list().execute.return_value = {"labels": [{"id": "L1", "name": "Work", "type": "user"}]}
        api.users().messages().get().execute.return_value = {"labelIds": ["INBOX"]}
        api.users().drafts().list().execute.return_value = {"drafts": []}
        calendar = mock.Mock()
        calendar.events().list().execute.return_value = {"items": []}
        for patcher in (mock.patch("triage.gmail._service", return_value=api),
                        mock.patch("triage.gcal._service", return_value=calendar)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_labels_and_drafts(self):
        from triage import gmail
        label_id = gmail.create_label("Work/Launch")
        gmail.add_labels("m1", ["L1", label_id])
        self.assertIn("Work/Launch", [label["name"] for label in gmail.list_labels()])
        self.assertEqual(gmail.get_label_ids("m1"), ["INBOX", "L1", label_id])
        self.assertFalse(gmail.thread_has_draft("t1"))
        gmail.create_reply_draft({"thread_id": "t1", "subject": "Hi"}, "ada@x.com", [], "Hello")
        self.assertTrue(gmail.thread_has_draft("t1"))  # a second email in the thread gets no paid draft

    def test_calendar_blocks(self):
        from triage import gcal
        start, end = datetime(2026, 11, 24, 10, tzinfo=timezone.utc), datetime(2026, 11, 24, 10, 15, tzinfo=timezone.utc)
        gcal.create_event("Reply", (start, end), "notes", "m1", "url")
        self.assertEqual(len(gcal._events(start - timedelta(hours=1), end + timedelta(hours=1))), 1)  # next block avoids it
        self.assertEqual(len(gcal._events(end + timedelta(hours=1), end + timedelta(hours=2))), 0)
        self.assertIsNotNone(gcal.find_event_for_message("m1", real=False))
        self.assertIsNone(gcal.find_event_for_message("m2", real=False))

    def test_nothing_is_remembered_outside_a_dry_run(self):
        self.ctx.dry_run = False
        self.ctx.would_do.append({"action": "create_reply_draft", "thread_id": "t1"})
        self.assertEqual(run_context.simulated("create_reply_draft"), [])



class PriceTests(SimpleTestCase):
    def test_unpriced_models_are_refused_before_spending_and_never_cost_zero(self):
        eval_prices.require_prices(["claude-haiku-5-5", "none"])
        with self.assertRaises(SystemExit):
            eval_prices.require_prices(["claude-haiku-5-5", "claude-new-model"])
        with self.assertRaises(KeyError):
            eval_prices.dollars("claude-new-model", 1000, 100)
        with mock.patch("sys.argv", ["bench", "--classify", "claude-new-model", "--yes"]), \
                mock.patch.object(eval_bench, "run_one") as run_one, self.assertRaises(SystemExit):
            eval_bench.main()
        run_one.assert_not_called()


class LocalPageGuardTests(SimpleTestCase):
    def test_only_this_server_as_host_and_origin(self):
        from evals.localhost import is_local_request
        self.assertTrue(is_local_request({"Host": "127.0.0.1:8765"}, 8765))
        self.assertTrue(is_local_request({"Host": "localhost:8765"}, 8765))
        self.assertFalse(is_local_request({"Host": "evil.example:8765"}, 8765))  # DNS rebinding
        self.assertFalse(is_local_request({}, 8765))
        self.assertTrue(is_local_request({"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}, 8765, write=True))
        self.assertFalse(is_local_request({"Host": "127.0.0.1:8765", "Origin": "http://evil.example"}, 8765, write=True))
