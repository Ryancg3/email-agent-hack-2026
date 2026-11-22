"""Google Calendar access: free slots in the primary calendar and booking them.

Usage:
    python -m triage.gcal [DAYS]    # print free slots for the next DAYS days (default 5)
"""

import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import run_context

WORKDAYS = range(0, 5)  # Monday to Friday; working hours come from the run context

# Events with these words in the title count as email time to group reply blocks with.
GROUP_KEYWORDS = ("reply", "replies", "email", "inbox")
# Group with existing email time only if that delays the block by at most this much.
MAX_GROUP_DELAY = timedelta(days=1)
# Reply blocks start 5 to 10 minutes after a meeting ends (5 minutes, then rounded up
# to the next 5-minute mark), and end at least this long before the next meeting.
AFTER_MEETING = timedelta(minutes=5)
BEFORE_MEETING = timedelta(minutes=10)

Slot = tuple[datetime, datetime]


def _service():
    return run_context.current().service("calendar", "v3")


def _timezone() -> ZoneInfo:
    return run_context.current().cached(
        "calendar:timezone",
        lambda: ZoneInfo(_service().calendars().get(calendarId="primary").execute()["timeZone"]),
    )


def _busy(start: datetime, end: datetime) -> list[Slot]:
    response = _service().freebusy().query(body={
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": "primary"}],
    }).execute()
    periods = response["calendars"]["primary"]["busy"]
    return sorted(
        (datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"]))
        for p in periods
    )


def _window(days: int) -> Slot:
    """From now (rounded up to the quarter hour) to the end of the last day."""
    tz = _timezone()
    now = datetime.now(tz).replace(second=0, microsecond=0)
    now += timedelta(minutes=-now.minute % 15)
    return now, datetime.combine(now.date() + timedelta(days=days), time.min, tz)


def _round_up_5(t: datetime) -> datetime:
    t = t.replace(second=0, microsecond=0) + timedelta(minutes=1 if t.second or t.microsecond else 0)
    return t + timedelta(minutes=-t.minute % 5)


def _is_email_time(event: dict) -> bool:
    """The agent's own reply blocks and the user's events with a GROUP_KEYWORDS
    word in the title."""
    return (
        "gmailMessageId" in event.get("extendedProperties", {}).get("private", {})
        or any(word in event.get("summary", "").lower() for word in GROUP_KEYWORDS)
    )


def _blocks_time(event: dict) -> bool:
    """Whether the event makes the user busy: not marked free, not declined."""
    return not (
        event.get("transparency") == "transparent"
        or any(a.get("self") and a.get("responseStatus") == "declined"
               for a in event.get("attendees", []))
    )


def _events(start: datetime, end: datetime) -> list[dict]:
    """Events in the range, all-day ones included."""
    return _service().events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime", maxResults=2500,
    ).execute()["items"]


def _any_slot(event: dict) -> Slot:
    """An event's start and end; an all-day event runs from midnight to
    midnight in the calendar's time zone."""
    if "dateTime" in event["start"]:
        return event_slot(event)
    tz = _timezone()
    return tuple(datetime.combine(date.fromisoformat(event[edge]["date"]), time.min, tz)
                 for edge in ("start", "end"))


def _calendar(start: datetime, end: datetime) -> tuple[list[Slot], list[Slot]]:
    """The meetings and the email time in the range, both only events that
    make the user busy. Meetings include busy all-day events, like a vacation.
    Starts a little earlier, so a meeting that just ended still keeps its
    AFTER_MEETING buffer."""
    events = [e for e in _events(start - AFTER_MEETING - timedelta(minutes=5), end) if _blocks_time(e)]
    timed_email = [e for e in events if "dateTime" in e["start"] and _is_email_time(e)]
    meetings = [_any_slot(e) for e in events if e not in timed_email]
    return meetings, [event_slot(e) for e in timed_email]


def _around_meetings(meetings: list[Slot], email_time: list[Slot]) -> list[Slot]:
    """What a reply block must stay clear of: each meeting widened by
    BEFORE_MEETING before it and AFTER_MEETING after it (rounded up to the next
    5-minute mark), and email time as it is, so reply blocks can sit right next
    to it. A meeting that overlaps email time keeps its buffers."""
    padded = [(start - BEFORE_MEETING, _round_up_5(end + AFTER_MEETING)) for start, end in meetings]
    return sorted(padded + email_time)


def free_slots(days: int = 5, min_minutes: int = 30) -> list[Slot]:
    """Free slots of at least min_minutes within working hours, from now on,
    from the calendar's free/busy times (for proposing meeting times)."""
    now, end = _window(days)
    return _working_gaps(days, _busy(now, end), timedelta(minutes=min_minutes))


def reply_gaps(days: int, min_minutes: int, any_hour: bool = False) -> tuple[list[Slot], list[Slot]]:
    """Free time for a reply block, and the email time in the window.

    The gaps keep clear of meetings: they start 5 to 10 minutes after one ends
    and stop before the next one starts, so a reply block never lands right
    before a meeting. Back-to-back meetings leave no gap between them, so the
    block goes after the last one.

    Within working hours, or with any_hour around the clock, every day.
    """
    now, end = _window(days)
    meetings, email_time = _calendar(now, end)
    busy = _around_meetings(meetings, email_time)
    min_length = timedelta(minutes=min_minutes)
    if any_hour:
        return _gaps(now, end, busy, min_length), email_time
    return _working_gaps(days, busy, min_length), email_time


def _working_gaps(days: int, busy: list[Slot], min_length: timedelta) -> list[Slot]:
    ctx = run_context.current()
    tz = _timezone()
    now, _ = _window(days)
    gaps = []
    for offset in range(days):
        day = now.date() + timedelta(days=offset)
        if day.weekday() in WORKDAYS:
            start = max(datetime.combine(day, ctx.work_start, tz), now)
            gaps += _gaps(start, datetime.combine(day, ctx.work_end, tz), busy, min_length)
    return gaps


def _gaps(start: datetime, end: datetime, busy: list[Slot], min_length: timedelta) -> list[Slot]:
    """The free stretches of at least min_length between start and end."""
    cursor, gaps = start, []
    for busy_start, busy_end in sorted(busy):
        if busy_end <= cursor or busy_start >= end:
            continue
        if busy_start - cursor >= min_length:
            gaps.append((cursor, busy_start))
        cursor = max(cursor, busy_end)
    if end - cursor >= min_length:
        gaps.append((cursor, end))
    return gaps


def _pick_slot(slots: list[Slot], anchors: list[Slot], minutes: int) -> tuple[Slot, Slot | None] | None:
    """A slot of the given length, right before or after an anchor if one is
    free soon enough, else at the start of the first free slot. Returns the
    slot and the anchor it is next to (or None)."""
    if not slots:
        return None
    length = timedelta(minutes=minutes)
    candidates = []
    for start, end in slots:
        for anchor in anchors:
            if start == anchor[1]:
                candidates.append(((start, start + length), anchor))
            if end == anchor[0]:
                candidates.append(((end - length, end), anchor))
    first = slots[0][0]
    if candidates:
        slot, anchor = min(candidates)
        if slot[0] - first <= MAX_GROUP_DELAY:
            return slot, anchor
    return (first, first + length), None


def reply_slot(minutes: int, days: int = 5, urgent: bool = False) -> tuple[Slot, Slot | None] | None:
    """Where to block reply time, always clear of meetings (see reply_gaps).

    Urgent replies take the first free time, which is within the hour unless
    meetings or the end of the working day are in the way; then it is the first
    free time after them. With the user's urgent_after_hours setting, urgent
    replies ignore working hours. Other replies go next to existing email time when
    that is free within MAX_GROUP_DELAY, else the first free time.

    Returns the slot and the email-time event it is next to (or None), or
    None if there is no free slot."""
    any_hour = urgent and run_context.current().urgent_after_hours
    slots, email_time = reply_gaps(days, minutes, any_hour)
    return _pick_slot(slots, [] if urgent else email_time, minutes)


def find_event_for_message(message_id: str) -> dict | None:
    """The event created for this Gmail message, past or future, including
    ones the user deleted (status "cancelled")."""
    events = _service().events().list(
        calendarId="primary", privateExtendedProperty=f"gmailMessageId={message_id}",
        showDeleted=True, maxResults=1,
    ).execute()["items"]
    return events[0] if events else None


def event_slot(event: dict) -> Slot:
    return (datetime.fromisoformat(event["start"]["dateTime"]),
            datetime.fromisoformat(event["end"]["dateTime"]))


def create_event(title: str, slot: Slot, description: str, message_id: str, source_url: str) -> str:
    """Create an event in the primary calendar, tagged with the Gmail message
    it is for, and return its link."""
    start, end = slot
    event = _service().events().insert(calendarId="primary", body={
        "summary": title,
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "source": {"title": "Open email in Gmail", "url": source_url},
        "extendedProperties": {"private": {"gmailMessageId": message_id}},
    }).execute()
    return event["htmlLink"]


def format_slot(slot: Slot) -> str:
    start, end = (t.astimezone(_timezone()) for t in slot)
    return f"{start:%a %d %b %H:%M}–{end:%H:%M}"


if __name__ == "__main__":
    for slot in free_slots(int(sys.argv[1]) if len(sys.argv) > 1 else 5):
        print(format_slot(slot))
