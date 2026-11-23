from django.core.management.base import BaseCommand

from accounts import label_sync
from accounts.models import GoogleConnection, User, user_labels


class Command(BaseCommand):
    help = (
        "Create each user's labels in Gmail where Gmail doesn't have them yet, "
        "so the agent can use them. Only adds; never renames or deletes."
    )

    def add_arguments(self, parser):
        parser.add_argument("email", nargs="?", help="Only this user (default: every connected user)")

    def handle(self, *args, email=None, **options):
        users = User.objects.filter(google__status=GoogleConnection.Status.CONNECTED)
        if email:
            users = users.filter(email__iexact=email)
            if not users:
                self.stderr.write(f"No connected user with email {email}.")
                return
        for user in users:
            created, problems = label_sync.push_to_gmail(user, user_labels(user))
            self.stdout.write(f"{user.email}: created {', '.join(created) if created else 'nothing'}")
            for problem in problems:
                self.stderr.write(f"{user.email}: {problem}")
