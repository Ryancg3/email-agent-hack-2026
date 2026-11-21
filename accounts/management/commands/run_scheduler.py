import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from accounts import cleanup_runner, push, runner
from accounts.models import GoogleConnection, User

INTERVAL = timedelta(minutes=5)
POLL_SECONDS = 10


class Command(BaseCommand):
    help = (
        "Run the agent for every enabled user every 5 minutes, and on 'Run now' "
        "(which reprocesses the user's newest emails). With Gmail push set up, run it "
        "seconds after an email arrives, and poll those users only every 30 minutes. "
        "Run sub-label cleanup once a month, and on 'Clean up sub-labels'."
    )

    def handle(self, *args, **options):
        next_scheduled = timezone.now()
        while True:
            poll = timezone.now() >= next_scheduled
            if poll:
                next_scheduled = timezone.now() + INTERVAL
            self.run_agents(poll)
            push.renew_watches()
            self.clean_up_sublabels()
            time.sleep(POLL_SECONDS)

    def run_agents(self, poll: bool):
        """Run users who clicked "Run now" or got a Gmail push, and with poll, the
        enabled users due for a poll."""
        requested = Q(run_requested_at__isnull=False) | Q(
            agent_enabled=True, google__push_received_at__isnull=False
        )
        if poll:
            requested |= Q(agent_enabled=True) & push.needs_poll(timezone.now())

        users = User.objects.filter(
            requested, google__status=GoogleConnection.Status.CONNECTED
        ).select_related("google")
        for user in users:
            # Clear first, so a click or push during this run triggers another one.
            User.objects.filter(pk=user.pk).update(run_requested_at=None)
            GoogleConnection.objects.filter(pk=user.google.pk).update(push_received_at=None)
            self.stdout.write(f"{timezone.now():%H:%M:%S} running for {user.email}")
            rerun = runner.RUN_NOW_NEWEST if user.run_requested_at else 0
            runner.run_user(user, rerun_newest=rerun)

    def clean_up_sublabels(self):
        users = User.objects.filter(google__status=GoogleConnection.Status.CONNECTED).filter(
            Q(cleanup_requested_at__isnull=False) | Q(agent_enabled=True)
        ).select_related("google")
        for user in users:
            if user.cleanup_requested_at is None and not cleanup_runner.is_due(user):
                continue
            User.objects.filter(pk=user.pk).update(cleanup_requested_at=None)
            self.stdout.write(f"{timezone.now():%H:%M:%S} cleaning up sub-labels for {user.email}")
            cleanup_runner.run_user(user)
