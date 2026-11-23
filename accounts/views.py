from datetime import UTC, datetime, time, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db import DatabaseError, connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from oauthlib.oauth2 import OAuth2Error

from triage import gmail
from triage import labels
from triage import run_context

from . import cleanup_runner, google_connect, label_sync, push, runner
from .forms import LABELS_MAX, LabelFormSet, SettingsForm, SignupForm
from .models import GoogleConnection, Label, ProcessedEmail, seed_labels, user_labels


def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


@csrf_exempt
@require_POST
def gmail_push(request):
    """Pub/Sub push endpoint: Gmail says a watched inbox changed."""
    if not push.token_ok(request.GET.get("token", "")):
        return HttpResponse(status=403 if push.enabled() else 404)
    # Always acknowledge: Pub/Sub retries anything but a 2xx for days.
    if address := push.notified_address(request.body):
        push.mark_pushed(address)
    return HttpResponse(status=204)


def home(request):
    return render(request, "accounts/landing.html")


def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    if settings.SIGNUP_CLOSED:
        messages.error(request, "Sign-up is closed on this server.")
        return redirect("login")
    form = SignupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        seed_labels(user)
        login(request, user)
        return redirect("dashboard")
    return render(request, "accounts/signup.html", {"form": form})


@login_required
def dashboard(request):
    return _render_dashboard(request, SettingsForm(instance=request.user))


def _render_dashboard(request, form):
    connection = GoogleConnection.objects.filter(user=request.user).first()
    # Users do not have a saved time zone yet, so use an explicit UTC day and
    # label it as UTC in the dashboard rather than implying a local boundary.
    today_start = datetime.combine(timezone.now().date(), time.min, tzinfo=UTC)
    handled_today = request.user.processed_emails.filter(
        status=ProcessedEmail.Status.OK,
        processed_at__gte=today_start,
        processed_at__lt=today_start + timedelta(days=1),
    ).count()
    failed = request.user.processed_emails.filter(
        status=ProcessedEmail.Status.ERROR, processed_at__gte=timezone.now() - timedelta(hours=24)
    )
    return render(request, "accounts/dashboard.html", {
        "connection": connection,
        "form": form,
        "handled_today": handled_today,
        "failed_count": failed.count(),
        "latest_failure": failed.first(),
        "cleanup": cleanup_runner.last_result(request.user) if connection else None,
        "push_on": bool(
            push.enabled() and connection and connection.watch_expires_at
            and connection.watch_expires_at > timezone.now()
        ),
    })


@login_required
@require_POST
def save_settings(request):
    form = SettingsForm(request.POST, instance=request.user)
    if not form.is_valid():
        return _render_dashboard(request, form)
    form.save()
    messages.success(request, "Settings saved.")
    return redirect("dashboard")


@login_required
@require_POST
def run_now(request):
    if not GoogleConnection.objects.filter(
        user=request.user, status=GoogleConnection.Status.CONNECTED
    ).exists():
        messages.error(request, "Connect Google first.")
        return redirect("dashboard")
    request.user.run_requested_at = timezone.now()
    request.user.save(update_fields=["run_requested_at"])
    messages.success(
        request, f"Run requested. It rechecks your {runner.RUN_NOW_NEWEST} newest emails within a few seconds."
    )
    return redirect("dashboard")


@login_required
@require_POST
def cleanup_now(request):
    if not GoogleConnection.objects.filter(
        user=request.user, status=GoogleConnection.Status.CONNECTED
    ).exists():
        messages.error(request, "Connect Google first.")
        return redirect("dashboard")
    request.user.cleanup_requested_at = timezone.now()
    request.user.save(update_fields=["cleanup_requested_at"])
    messages.success(request, "Cleanup requested. It starts within a few seconds.")
    return redirect("dashboard")


@login_required
def labels_page(request):
    user_labels(request.user)
    if request.method == "POST":
        formset = LabelFormSet(request.POST, queryset=request.user.labels.all(), prefix="labels")
        if formset.is_valid():
            added = _save_labels(request.user, formset)
            messages.success(request, "Labels saved. The next run uses them.")
            _report_push(request, *label_sync.push_to_gmail(request.user, added))
            return redirect("labels")
        found, problem = label_sync.gmail_labels(request.user)
    else:
        # Import before building the formset, so new rows show up right away.
        found, problem = label_sync.gmail_labels(request.user)
        if found is not None and (imported := label_sync.import_from_gmail(request.user, found)):
            messages.success(request, f"New in Gmail, added below: {', '.join(imported)}. Describe them so the agent knows what goes there.")
        formset = LabelFormSet(queryset=request.user.labels.all(), prefix="labels")

    gmail_names = None if found is None else [label["name"] for label in found]
    missing = []
    if gmail_names is not None:
        in_gmail = {labels.key(name): name for name in gmail_names if "/" not in name}
        described = {labels.key(label.name) for label in request.user.labels.all()}
        missing = [name for k, name in in_gmail.items() if k not in described]
        for form in formset:
            form.not_in_gmail = bool(form.instance.pk) and labels.key(form.initial.get("name", "")) not in in_gmail
    return render(request, "accounts/labels.html", {
        "formset": formset, "missing": missing, "problem": problem,
        "not_in_gmail": [form.initial.get("name", "") for form in formset if getattr(form, "not_in_gmail", False)],
        "label_count": request.user.labels.count(), "labels_max": LABELS_MAX,
    })


def _save_labels(user, formset) -> list[Label]:
    """Saves the formset and returns the rows it added."""
    rows = formset.save(commit=False)
    for label in formset.deleted_objects:
        label.delete()
    position = max(user.labels.values_list("position", flat=True), default=-1) + 1
    added = []
    for label in rows:
        if label.pk is None:
            label.user, label.position = user, position
            position += 1
            added.append(label)
        label.save()
    return added


def _report_push(request, created: list[str], problems: list[str]) -> None:
    if created:
        messages.success(request, f"Created in Gmail: {', '.join(created)}.")
    for problem in problems:
        messages.error(request, problem)


@login_required
@require_POST
def push_missing_labels(request):
    """Creates every label of the user's that Gmail doesn't have yet."""
    created, problems = label_sync.push_to_gmail(request.user, user_labels(request.user))
    if not created and not problems:
        messages.success(request, "All your labels are already in Gmail.")
    _report_push(request, created, problems)
    return redirect("labels")


@login_required
@require_POST
def add_missing_labels(request):
    """Adds an empty row for each Gmail top-level label the user hasn't described."""
    gmail_names, problem = label_sync.gmail_label_names(request.user)
    if gmail_names is None:
        messages.error(request, problem)
        return redirect("labels")
    labels_now = user_labels(request.user)
    described = {labels.key(label.name) for label in labels_now}
    position = max((label.position for label in labels_now), default=-1) + 1
    added, skipped, too_long = [], 0, []
    for name in gmail_names:
        if "/" in name or labels.key(name) in described:
            continue
        described.add(labels.key(name))
        emoji, plain = labels.split_emoji(name)
        if not label_sync.fits(emoji, plain):
            too_long.append(name)
            continue
        if len(labels_now) + len(added) >= LABELS_MAX:
            skipped += 1
            continue
        added.append(Label(user=request.user, position=position + len(added), emoji=emoji, name=plain))
    Label.objects.bulk_create(added)
    if too_long:
        max_length = Label._meta.get_field("name").max_length
        messages.error(request, f"Too long for a label name ({max_length} characters at most), so not added: "
                                f"{', '.join(too_long)}. Shorten them in Gmail first.")
    if not added and too_long and not skipped:
        return redirect("labels")
    if not added and skipped:
        messages.error(request, f"You can have at most {LABELS_MAX} labels. Delete one to add another.")
        return redirect("labels")
    message = f"Added {len(added)} label{'s' if len(added) != 1 else ''}. Describe them below."
    if skipped:
        message += f" {skipped} more didn't fit: you can have at most {LABELS_MAX} labels."
    messages.success(request, message)
    return redirect("labels")


@login_required
@require_POST
def reset_labels(request):
    request.user.labels.all().delete()
    seed_labels(request.user)
    messages.success(request, "Labels reset to the defaults.")
    return redirect("labels")


def _callback_url(request) -> str:
    return request.build_absolute_uri(reverse("google_callback"))


@login_required
def google_connect_start(request):
    if problem := google_connect.setup_problem():
        messages.error(request, problem)
        return redirect("dashboard")
    url, state, code_verifier = google_connect.authorization_url(_callback_url(request))
    request.session["google_oauth"] = {"state": state, "code_verifier": code_verifier}
    return redirect(url)


@login_required
def google_callback(request):
    pending = request.session.pop("google_oauth", None)
    if not pending or request.GET.get("state") != pending["state"]:
        messages.error(request, "The Google sign-in expired. Please try again.")
        return redirect("dashboard")
    if "code" not in request.GET:
        messages.error(request, "Google access was not granted.")
        return redirect("dashboard")

    try:
        creds = google_connect.exchange_code(
            _callback_url(request), request.GET["code"], pending["code_verifier"]
        )
    except OAuth2Error as e:
        messages.error(request, f"Google sign-in failed: {e.description or e.error}")
        return redirect("dashboard")
    if not google_connect.has_all_scopes(creds):
        messages.error(request, "Please tick all the Gmail and Calendar boxes on Google's screen.")
        return redirect("dashboard")
    if not creds.refresh_token:
        messages.error(request, "Google didn't return a long-lived token. Please try again.")
        return redirect("dashboard")

    google_email = google_connect.account_email(creds)
    defaults = {
        "google_email": google_email,
        "refresh_token": google_connect.encrypt(creds.refresh_token),
        "status": GoogleConnection.Status.CONNECTED,
        "last_error": "",
        "authorized_at": timezone.now(),  # Google's 7 days start again
        "reconnect_reminder_sent_at": None,
        "watch_renew_at": None,  # watch the (possibly different) account right away
    }
    previous = GoogleConnection.objects.filter(user=request.user).first()
    if previous and previous.google_email.lower() != google_email.lower():
        # A different account: start its cutoff now, so its existing inbox is left alone.
        # Reconnecting the same account keeps the cutoff, so emails that arrived while
        # it was disconnected are still processed.
        defaults["connected_at"] = timezone.now()
        defaults["seen_label_ids"] = None  # its labels are a new starting point
    connection, _ = GoogleConnection.objects.update_or_create(user=request.user, defaults=defaults)
    messages.success(request, f"Connected {connection.google_email}.")
    return redirect("dashboard")


@login_required
@require_POST
def google_disconnect(request):
    if connection := GoogleConnection.objects.filter(user=request.user).first():
        push.stop(connection)
        google_connect.revoke(connection)
        connection.delete()
        messages.success(request, "Google disconnected.")
    return redirect("dashboard")
