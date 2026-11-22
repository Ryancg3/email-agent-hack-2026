"""Gmail push notifications: run the agent seconds after an email arrives.

Gmail posts to a Google Cloud Pub/Sub topic whenever a watched inbox changes, and
Pub/Sub forwards that to our webhook. The webhook only marks the connection; the
scheduler then runs the agent as usual, which finds the new emails the same way a
poll does (newest inbox ids not yet in ProcessedEmail). So a missed or duplicate
notification costs nothing, and polling stays on as a slower fallback.
"""

import base64
import binascii
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from triage import gmail
from triage import run_context

from . import google_connect, runner
from .models import GoogleConnection

log = logging.getLogger(__name__)

# Google asks for watch to be renewed at least every 7 days and recommends daily.
RENEW_EVERY = timedelta(days=1)
RETRY_AFTER = timedelta(hours=1)
# Users with a working watch are still polled this often, in case a notification is lost.
FALLBACK_POLL = timedelta(minutes=30)


def enabled() -> bool:
    return bool(settings.GMAIL_PUBSUB_TOPIC and settings.GMAIL_PUSH_TOKEN)


def token_ok(token: str) -> bool:
    return enabled() and hmac.compare_digest(token.encode(), settings.GMAIL_PUSH_TOKEN.encode())


def notified_address(body: bytes) -> str | None:
    """The Gmail address in a Pub/Sub push request, or None if it isn't one.
    The body is {"message": {"data": base64 of {"emailAddress", "historyId"}}, ...}."""
    try:
        data = json.loads(body)["message"]["data"]
        return json.loads(base64.b64decode(data))["emailAddress"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None


def mark_pushed(address: str) -> int:
    """Flag the connections for this address so the scheduler runs them next."""
    return GoogleConnection.objects.filter(google_email__iexact=address).update(push_received_at=timezone.now())


def needs_poll(now: datetime) -> Q:
    """Users the 5-minute poll should still run: everyone without push, and users
    with push only when FALLBACK_POLL has passed since their last run."""
    if not enabled():
        return Q()
    return (
        Q(google__watch_expires_at__isnull=True)
        | Q(google__watch_expires_at__lte=now)
        | Q(google__last_run_at__isnull=True)
        | Q(google__last_run_at__lte=now - FALLBACK_POLL)
    )


def renew_watches() -> None:
    """Start or renew the Gmail watch of every enabled, connected user that is due."""
    if not enabled():
        return
    now = timezone.now()
    due = GoogleConnection.objects.filter(
        Q(watch_renew_at__isnull=True) | Q(watch_renew_at__lte=now),
        status=GoogleConnection.Status.CONNECTED, user__agent_enabled=True,
    ).select_related("user")

    for connection in due:
        try:
            with run_context.use(runner.context(connection.user)):
                result = gmail.watch_inbox(settings.GMAIL_PUBSUB_TOPIC)
            connection.watch_expires_at = datetime.fromtimestamp(int(result["expiration"]) / 1000, dt_timezone.utc)
            connection.watch_renew_at = now + RENEW_EVERY
            connection.watch_error = ""
        except Exception as e:
            log.warning("gmail watch failed for %s: %s", connection.google_email, e)
            # Assume push is down, so the 5-minute poll takes over and the dashboard says so.
            connection.watch_expires_at = None
            connection.watch_renew_at = now + RETRY_AFTER
            connection.watch_error = f"Instant processing is off, polling instead: {e}"
        connection.save(update_fields=["watch_expires_at", "watch_renew_at", "watch_error"])


def stop(connection: GoogleConnection) -> None:
    """Best effort: stop Gmail's notifications before a disconnect."""
    if not connection.watch_expires_at:
        return
    try:
        with run_context.use(run_context.RunContext(google_connect.credentials(connection))):
            gmail.stop_watch()
    except Exception as e:
        log.warning("gmail stop failed for %s: %s", connection.google_email, e)
