"""Where reply blocks land in the calendar, around meetings."""

from datetime import datetime, time
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from triage import gcal
from triage import run_context

TZ = ZoneInfo("Europe/Berlin")
# A Monday.
DAY = (2026, 11, 23)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(*DAY, hour, minute, tzinfo=TZ)


def event(start, end, title="Meeting", **extra):
    return {"summary": title, "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()}, **extra}


class ReplySlotTests(SimpleTestCase):
    """The calendar is faked: the events Google lists for the queried range."""

    def place(self, now, meetings, minutes=15, urgent=False, email_time=(), after_hours=False,
              extra_events=(), until=datetime(2026, 11, 28, tzinfo=TZ)):
        events = [
            *(event(s, e) for s, e in meetings),
            *(event(s, e, "Email time") for s, e in email_time),
            *extra_events,
        ]

        def listed(start, end):
            # Like Google: events that overlap the range.
            return [e for e in events
                    if gcal._any_slot(e)[1] > start and gcal._any_slot(e)[0] < end]

        ctx = run_context.RunContext(creds=None, work_start=time(9), work_end=time(18),
                                     urgent_after_hours=after_hours)
        with run_context.use(ctx), \
                mock.patch.object(gcal, "_timezone", return_value=TZ), \
                mock.patch.object(gcal, "_window", return_value=(now, until)), \
                mock.patch.object(gcal, "_events", side_effect=listed):
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

    def test_urgent_after_hours_waits_for_the_next_working_day_by_default(self):
        slot = self.place(at(20), [], urgent=True)
        self.assertEqual(slot[0], datetime(2026, 11, 24, 9, tzinfo=TZ))

    def test_urgent_after_hours_setting_blocks_the_evening(self):
        slot = self.place(at(20), [(at(19, 30), at(20, 30))], urgent=True, after_hours=True)
        self.assertEqual(slot, (at(20, 35), at(20, 50)))

    def test_after_hours_setting_leaves_normal_replies_in_working_hours(self):
        slot = self.place(at(20), [], after_hours=True)
        self.assertEqual(slot[0], datetime(2026, 11, 24, 9, tzinfo=TZ))

    def test_meeting_that_just_ended_keeps_its_buffer(self):
        # The run's window starts at 10:30, after the meeting ended at 10:29.
        slot = self.place(at(10, 30), [(at(10), at(10, 29))], urgent=True)
        self.assertEqual(slot[0], at(10, 35))

    def test_after_hours_gap_runs_across_midnight(self):
        night = datetime(2026, 11, 24, tzinfo=TZ)
        meeting = (night.replace(minute=40), night.replace(hour=2))
        slot = self.place(at(23, 45), [meeting], minutes=30, urgent=True, after_hours=True)
        self.assertEqual(slot, (at(23, 45), night.replace(minute=15)))

    def test_meeting_under_email_time_keeps_its_buffers(self):
        both = [(at(11), at(12))]
        slot = self.place(at(10, 30), both, minutes=25, urgent=True, email_time=both)
        self.assertEqual(slot, (at(12, 5), at(12, 30)))

    def test_declined_and_free_events_are_not_meetings(self):
        declined = event(at(10), at(11), attendees=[{"self": True, "responseStatus": "declined"}])
        free = event(at(10), at(11), transparency="transparent")
        slot = self.place(at(10), [], urgent=True, extra_events=[declined, free])
        self.assertEqual(slot[0], at(10))

    def test_busy_all_day_event_blocks_the_day(self):
        conference = {"summary": "Conference", "start": {"date": "2026-11-23"}, "end": {"date": "2026-11-24"}}
        slot = self.place(at(10), [], urgent=True, extra_events=[conference])
        self.assertEqual(slot[0], datetime(2026, 11, 24, 9, tzinfo=TZ))

    def test_free_all_day_event_does_not(self):
        holiday = {"summary": "Holiday", "start": {"date": "2026-11-23"}, "end": {"date": "2026-11-24"},
                   "transparency": "transparent"}
        slot = self.place(at(10), [], urgent=True, extra_events=[holiday])
        self.assertEqual(slot[0], at(10))

    def test_free_email_time_is_not_blocked(self):
        free = event(at(10), at(12), "Email time", transparency="transparent")
        slot = self.place(at(10), [], urgent=True, extra_events=[free])
        self.assertEqual(slot[0], at(10))
