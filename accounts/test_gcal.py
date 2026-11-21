"""Where reply blocks land in the calendar, around meetings."""

from datetime import datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

import gcal
import run_context

TZ = ZoneInfo("Europe/Berlin")
# A Monday.
DAY = (2026, 11, 23)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(*DAY, hour, minute, tzinfo=TZ)


class ReplySlotTests(SimpleTestCase):
    """The calendar is faked: busy periods as freebusy gives them (in UTC, like
    Google), and the email-time events the agent groups with."""

    def place(self, now, meetings, minutes=15, urgent=False, email_time=()):
        email_time = list(email_time)
        busy = sorted(
            (s.astimezone(ZoneInfo("UTC")), e.astimezone(ZoneInfo("UTC")))
            for s, e in [*meetings, *email_time]
        )
        ctx = run_context.RunContext(creds=None, work_start=time(9), work_end=time(18))
        window = (now, datetime(2026, 11, 28, tzinfo=TZ))
        with run_context.use(ctx), \
                mock.patch.object(gcal, "_timezone", return_value=TZ), \
                mock.patch.object(gcal, "_window", return_value=window), \
                mock.patch.object(gcal, "_busy", return_value=busy), \
                mock.patch.object(gcal, "_email_blocks", return_value=email_time):
            slot, _ = gcal.reply_slot(minutes, urgent=urgent)
        return slot

    def test_urgent_goes_right_after_the_current_meeting(self):
        slot = self.place(at(10, 15), [(at(10), at(11))], urgent=True)
        self.assertEqual(slot, (at(11, 5), at(11, 20)))

    def test_odd_meeting_end_rounds_to_5_to_10_minutes_after(self):
        slot = self.place(at(10, 15), [(at(10), at(10, 52))], urgent=True)
        self.assertEqual(slot[0], at(11, 0))

    def test_not_squeezed_in_right_before_a_meeting(self):
        # 10:00 to 10:20 is free, but a 15 minute block would end 5 minutes before 10:20.
        slot = self.place(at(10), [(at(10, 20), at(11))], urgent=True)
        self.assertEqual(slot, (at(11, 5), at(11, 20)))

    def test_fits_before_a_meeting_with_room_to_spare(self):
        slot = self.place(at(10), [(at(10, 30), at(11))], urgent=True)
        self.assertEqual(slot, (at(10), at(10, 15)))

    def test_back_to_back_meetings_go_after_the_last_one(self):
        meetings = [(at(10), at(11)), (at(11), at(12)), (at(12, 10), at(13))]
        slot = self.place(at(10, 30), meetings, urgent=True)
        self.assertEqual(slot, (at(13, 5), at(13, 20)))

    def test_urgent_is_not_grouped_with_later_email_time(self):
        email_time = [(at(15), at(15, 30))]
        slot = self.place(at(10, 15), [(at(10), at(11))], urgent=True, email_time=email_time)
        self.assertEqual(slot, (at(11, 5), at(11, 20)))

    def test_not_urgent_still_groups_with_email_time(self):
        email_time = [(at(15), at(15, 30))]
        slot = self.place(at(10, 15), [(at(10), at(11))], email_time=email_time)
        self.assertEqual(slot, (at(14, 45), at(15)))

    def test_email_time_right_after_a_meeting_is_still_an_anchor(self):
        # The meeting and the email block meet at 11:00; the block stays next to the email time.
        email_time = [(at(11), at(11, 30))]
        slot = self.place(at(10, 15), [(at(10), at(11))], email_time=email_time)
        self.assertEqual(slot, (at(11, 30), at(11, 45)))

    def test_meeting_buffer_does_not_apply_to_email_time(self):
        # Free from 11:30; nothing pushes an urgent block away from earlier email time.
        email_time = [(at(11), at(11, 30))]
        slot = self.place(at(11), [], urgent=True, email_time=email_time)
        self.assertEqual(slot, (at(11, 30), at(11, 45)))
