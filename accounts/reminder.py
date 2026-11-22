"""Reminds a user to reconnect Google before Google signs the agent out.

While the Google app is in Testing mode, Google expires its refresh tokens 7 days
after it grants access. On day 6 the token still works, so the agent puts a reminder
in the user's own inbox through that same connection. It inserts the email rather
than sending it, so nothing leaves the mailbox. One reminder per grant: reconnecting
starts a new 7 days and allows the next one.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from google.auth.exceptions import RefreshError

from triage import gmail
from triage import run_context

from . import runner
from .models import GoogleConnection, ProcessedEmail

log = logging.getLogger(__name__)

TOKEN_LIFETIME = timedelta(days=7)
REMIND_AFTER = timedelta(days=6)
SUBJECT = "Reconnect Google to keep Inbox Agent running"


def send_due() -> None:
    """Send the reminder to every connected user whose access is 6 days old."""
    now = timezone.now()
    due = GoogleConnection.objects.filter(
        status=GoogleConnection.Status.CONNECTED,
        reconnect_reminder_sent_at__isnull=True,
        authorized_at__lte=now - REMIND_AFTER,
    ).select_related("user")

    for connection in due:
        # Writes below only apply to the grant read here: if the user reconnects while
        # Gmail is busy, the new grant keeps its own status and its own day 6 reminder.
        same_grant = GoogleConnection.objects.filter(pk=connection.pk, authorized_at=connection.authorized_at)
        try:
            with run_context.use(runner.context(connection.user)):
                message_id = gmail.insert_into_inbox(SUBJECT, body(connection))
        except RefreshError:
            # Google signed the agent out early: the dashboard banner takes over.
            same_grant.update(
                status=GoogleConnection.Status.RECONNECT_NEEDED,
                last_error="Google access expired or was revoked. Reconnect Google.",
            )
            continue
        except Exception as e:
            log.warning("reconnect reminder failed for %s, retrying next poll: %s", connection.google_email, e)
            continue
        # So the agent never labels it or blocks time for it, not even on "Run now".
        ProcessedEmail.objects.update_or_create(
            user=connection.user, message_id=message_id,
            defaults={"subject": SUBJECT, "status": ProcessedEmail.Status.SKIPPED},
        )
        same_grant.update(reconnect_reminder_sent_at=now)
        print(f"[reminder] put a reconnect reminder in {connection.google_email}'s inbox")


def body(connection: GoogleConnection) -> str:
    user = connection.user
    expires = connection.authorized_at + TOKEN_LIFETIME
    return (
        f"Hi {user.first_name or user.email.split('@')[0]},\n\n"
        f"Google will sign Inbox Agent out of {connection.google_email} around "
        f"{expires:%A %d %B, %H:%M} UTC. After that the agent stops labeling your email "
        "until you reconnect.\n\n"
        "Reconnect now to keep it running for another 7 days:\n"
        f"{reconnect_url()}\n\n"
        "This happens every 7 days while the Google app is in Testing mode.\n\n"
        "Inbox Agent\n"
    )


def reconnect_url() -> str:
    base = f"https://{settings.APP_HOST}" if settings.APP_HOST else "http://localhost:8000"
    return base + reverse("google_connect")
