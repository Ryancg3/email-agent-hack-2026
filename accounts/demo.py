"""Run the web app locally with a fake, in-memory Gmail: `make demo`.

For trying UI changes without a Google account. Nothing touches a real inbox,
and the demo has its own database (demo.db), so app.db is left alone.

    make demo
    open http://localhost:8765/login/    log in with DEMO_EMAIL / DEMO_PASSWORD below

Use a normal browser (Safari, Chrome). Conductor's built-in browser drops the
CSRF cookie, so every form there fails with "CSRF cookie not set".

The fake Gmail starts with every default label except Work, Personal and Coding,
plus an undescribed "Hobbies". To make a label "by hand in Gmail", open
http://localhost:8765/fake-gmail/add/?name=🎸%20Music and reload the Labels tab.
The fake Gmail resets on restart; the demo database doesn't (delete demo.db to start over).
"""

import os
from pathlib import Path

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password"
PORT = "8765"

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ["DATABASE_PATH"] = str(Path(__file__).resolve().parent.parent / "demo.db")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.http import HttpResponse  # noqa: E402
from django.urls import path  # noqa: E402

from accounts import google_connect  # noqa: E402
from accounts.models import GoogleConnection, User, user_labels  # noqa: E402
from config import urls  # noqa: E402
from triage import gmail  # noqa: E402

FAKE_GMAIL = [
    {"id": f"Label_{i}", "name": name, "type": "user"}
    for i, name in enumerate([
        "🔐 Authorisations", "🔔 Alerts", "✈️ Travel", "🛍️ Shopping", "📰 Newsletters",
        "📅 Events", "💶 Finance", "🩺 Medical", "Hobbies",
    ])
]


def fake_create_label(name: str, color: dict | None = None) -> str:
    label = {"id": f"Label_{len(FAKE_GMAIL)}", "name": name, "type": "user"}
    FAKE_GMAIL.append(label)
    print(f"[fake gmail] created {name}")
    return label["id"]


def add_by_hand(request):
    name = request.GET.get("name", "").strip()
    if not name:
        return HttpResponse("Pass ?name=…", status=400)
    fake_create_label(name)
    # Plain text: the name comes from the URL, so as HTML it could inject a script.
    return HttpResponse(f"Added {name} to the fake Gmail. Reload the Labels tab.", content_type="text/plain")


def main() -> None:
    gmail.list_labels = lambda: list(FAKE_GMAIL)
    gmail.create_label = fake_create_label
    google_connect.credentials = lambda connection: None
    urls.urlpatterns.insert(0, path("fake-gmail/add/", add_by_hand))

    call_command("migrate", verbosity=0)
    user = User.objects.filter(email=DEMO_EMAIL).first()
    if not user:
        user = User.objects.create_user(DEMO_EMAIL, DEMO_PASSWORD, first_name="Demo")
        GoogleConnection.objects.create(
            user=user, google_email=DEMO_EMAIL, refresh_token=google_connect.encrypt("fake")
        )
    user_labels(user)
    print(f"\nDemo: http://localhost:{PORT}/login/  ({DEMO_EMAIL} / {DEMO_PASSWORD})")
    print("Open it in a normal browser, not Conductor's.\n", flush=True)
    # --noreload: the reloader's child process wouldn't have the fakes.
    call_command("runserver", PORT, "--noreload")


if __name__ == "__main__":
    main()
