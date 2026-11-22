import base64
import json
from datetime import datetime, time, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone as django_timezone
from google.auth.exceptions import RefreshError
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

import actions
import agent
import labels
import run_context
import sublabel_cleanup

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
        with mock.patch.object(runner.agent, "run", lambda email, labels: seen.append(email.id) or result):
            runner.run_user(self.user)
        self.assertEqual(seen, ["m2", "m3"])
        self.assertEqual(self.user.processed_emails.filter(status="ok").count(), 3)
        self.assertEqual(self.user.processed_emails.get(message_id="m3").label_list, ["Work/Launch", "Team"])
        self.assertEqual(self.user.google.last_error, "")

    def test_run_now_reruns_the_newest_emails_even_if_done(self, _):
        ProcessedEmail.objects.create(user=self.user, message_id="m1", subject="old")
        seen = []
        result = SimpleNamespace(labels=[], urgency="not_urgent")
        with mock.patch.object(runner.agent, "run", lambda email, labels: seen.append(email.id) or result):
            runner.run_user(self.user, rerun_newest=3)
        runner.gmail.list_inbox_ids.assert_called_once_with(3)  # no connected_at cutoff
        self.assertEqual(seen, ["m1", "m2", "m3"])
        self.assertEqual(self.user.processed_emails.get(message_id="m1").subject, "subject m1")

    def test_run_now_skips_the_agents_own_reminder(self, _):
        ProcessedEmail.objects.create(user=self.user, message_id="m2", status=ProcessedEmail.Status.SKIPPED)
        seen = []
        result = SimpleNamespace(labels=[], urgency="not_urgent")
        with mock.patch.object(runner.agent, "run", lambda email, labels: seen.append(email.id) or result):
            runner.run_user(self.user, rerun_newest=3)
        self.assertEqual(seen, ["m1", "m3"])
        self.assertEqual(self.user.processed_emails.get(message_id="m2").status, ProcessedEmail.Status.SKIPPED)

    def test_reply_skill_uses_the_users_name(self, _):
        self.user.first_name, self.user.last_name = "Ada", "Lovelace"
        skills = []
        with mock.patch.object(runner.agent, "run", lambda email, labels: skills.append(runner.agent.load_skill("write-reply"))):
            runner.run_user(self.user)
        self.assertIn('Sign off with "Ada" for casual emails, "Ada Lovelace" for formal ones.', skills[0])

    def test_classifier_sees_the_users_label_descriptions(self, _):
        Label.objects.create(user=self.user, name="Work", sublabels="never", include="Projects at the office.")
        menus = []
        with mock.patch.object(runner.agent, "run", lambda email, names: menus.append(agent.label_menu(names)[1])):
            runner.run_user(self.user)
        self.assertEqual(menus[0], "- Work: Projects at the office. Sub-labels: never.")

    def test_failed_email_is_recorded_and_not_retried(self, _):
        with mock.patch.object(runner.agent, "run", side_effect=ValueError("bad email")):
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
            return_value=[{"name": "✈️ Travel"}, {"name": "✈️ Travel/Rome"}, {"name": "Work"}],
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
        self.assertEqual(self.user.labels.get(name="University").cleanup, "after the course's exam period; never the thesis")
        self.assertEqual(self.user.labels.count(), len(labels.parse(labels.LABELS_FILE.read_text())))
        self.assertContains(response, "In Gmail but not described: Work")
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
        self.assertContains(response, f"Please submit at most {LABELS_MAX} forms.")
        self.assertEqual(self.rows(), [("", "Mine")])

    def test_add_missing_gmail_labels(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, name="Travel")
        self.client.post(reverse("add_missing_labels"))
        self.assertEqual(self.rows(), [("", "Travel"), ("", "Work")])

    def test_reset_to_defaults(self, _):
        self.user.labels.all().delete()
        Label.objects.create(user=self.user, name="Mine")
        self.client.post(reverse("reset_labels"))
        self.assertFalse(self.user.labels.filter(name="Mine").exists())
        self.assertTrue(self.user.labels.filter(name="University").exists())

    def test_works_without_google(self, _):
        self.user.google.delete()
        response = self.client.get(reverse("labels"))
        self.assertContains(response, "Connect Google to check")
        self.list_labels.assert_not_called()


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
            self.assertFalse(labels.is_described("University"))
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


EMAIL = agent.Email(id="m1", thread_id="t1", sender="Bob <bob@example.com>", to="a@example.com", cc="", subject="Hi", body="Hello")
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
        with mock.patch.object(agent, "ChatAnthropic") as chat:
            structured = chat.return_value.with_structured_output.return_value
            structured.invoke.return_value = answer
            self.assertEqual(agent.ask("system", "user", agent.Reply, max_tokens=10), "parsed")
            chat.return_value.with_structured_output.assert_called_once_with(agent.Reply, method="json_schema", include_raw=True)

            answer["raw"] = AIMessage(content="", response_metadata={"stop_reason": "refusal"})
            with self.assertRaisesMessage(RuntimeError, "Model refused"):
                agent.ask("system", "user", agent.Reply, max_tokens=10)


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
        with mock.patch("gmail._service", return_value=service), \
                mock.patch("gmail.my_address", return_value="a@example.com"):
            import gmail
            self.assertEqual(gmail.insert_into_inbox("Hello", "Body text"), "m9")
        body = service.users().messages().insert.call_args.kwargs["body"]
        self.assertEqual(body["labelIds"], ["INBOX", "UNREAD"])
        raw = base64.urlsafe_b64decode(body["raw"]).decode()
        self.assertIn("To: a@example.com", raw)
        self.assertIn("From: Inbox Agent <a@example.com>", raw)
        self.assertIn("Subject: Hello", raw)
        service.users().messages().send.assert_not_called()
