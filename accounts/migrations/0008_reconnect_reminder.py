from django.db import migrations, models
from django.db.models import F
from django.utils import timezone


def authorized_at_from_connected_at(apps, schema_editor):
    """Best guess for existing connections: access was granted when they connected."""
    apps.get_model("accounts", "GoogleConnection").objects.update(authorized_at=F("connected_at"))


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0007_urgent_after_hours"),
    ]

    operations = [
        migrations.AddField(
            model_name="googleconnection",
            name="authorized_at",
            field=models.DateTimeField(default=timezone.now),
        ),
        migrations.AddField(
            model_name="googleconnection",
            name="reconnect_reminder_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="processedemail",
            name="status",
            field=models.CharField(
                choices=[("ok", "Ok"), ("error", "Error"), ("skipped", "Skipped")], default="ok", max_length=10
            ),
        ),
        migrations.RunPython(authorized_at_from_connected_at, migrations.RunPython.noop),
    ]
