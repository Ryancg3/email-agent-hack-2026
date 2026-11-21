"""Google Calendar access: free slots in the primary calendar and booking them.

Usage:
    python gcal.py [DAYS]    # print free slots for the next DAYS days (default 5)
"""

import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import run_context

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


def _around_meetings(busy: list[Slot], email_blocks: list[Slot]) -> list[Slot]:
    """The busy periods widened by the meeting buffers: AFTER_MEETING after a
    meeting ends and BEFORE_MEETING before one starts. Email time (the agent's
    reply blocks and the user's own email events) is not a meeting, so an edge
    it shares with email time stays as it is, and reply blocks can still sit
    right next to it."""
    starts = {start for start, _ in email_blocks}
    ends = {end for _, end in email_blocks}
    return [
        (start if start in starts else start - BEFORE_MEETING,
         end if end in ends else _round_up_5(end + AFTER_MEETING))
        for start, end in busy
    ]


def free_slots(days: int = 5, min_minutes: int = 30, email_blocks: list[Slot] | None = None) -> list[Slot]:
    """Free slots of at least min_minutes within working hours, from now on.

    With email_blocks (the email time in the window), the slots keep clear of
    meetings: they start a few minutes after one ends and stop before the next
    one starts, so a reply block never lands right before a meeting. Back-to-back
    meetings leave no slot between them, so the block goes after the last one.
    """
    ctx = run_context.current()
    hours = (ctx.work_start, ctx.work_end)
    tz = _timezone()
    now, end = _window(days)
    today = now.date()
    busy = _busy(now, end)
    if email_blocks is not None:
        busy = sorted(_around_meetings(busy, email_blocks))

    slots = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        if day.weekday() in WORKDAYS:
            slots += _gaps(day, hours, tz, now, busy, timedelta(minutes=min_minutes))
    return slots


def _gaps(
    day: date, hours: tuple[time, time], tz: ZoneInfo, now: datetime,
    busy: list[Slot], min_length: timedelta,
) -> list[Slot]:
    cursor = max(datetime.combine(day, hours[0], tz), now)
    day_end = datetime.combine(day, hours[1], tz)
    gaps = []
    for busy_start, busy_end in busy:
        if busy_end <= cursor or busy_start >= day_end:
            continue
        if busy_start - cursor >= min_length:
            gaps.append((cursor, busy_start))
        cursor = max(cursor, busy_end)
    if day_end - cursor >= min_length:
        gaps.append((cursor, day_end))
    return gaps


def _email_blocks(start: datetime, end: datetime) -> list[Slot]:
    """Timed events in the range that are email time: the agent's own reply
    blocks or the user's events with a GROUP_KEYWORDS word in the title."""
    events = _service().events().list(
        calendarId="primary", timeMin=start.isoformat(), timeMax=end.isoformat(),
        singleEvents=True, orderBy="startTime",
    ).execute()["items"]
    return [
        event_slot(e) for e in events
        if "dateTime" in e["start"] and (
            "gmailMessageId" in e.get("extendedProperties", {}).get("private", {})
            or any(word in e.get("summary", "").lower() for word in GROUP_KEYWORDS)
        )
    ]


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
    """Where to block reply time, always clear of meetings (see free_slots).

    Urgent replies take the first free time, which is within the hour unless
    meetings or the end of the working day are in the way; then it is the first
    free time after them. Other replies go next to existing email time when
    that is free within MAX_GROUP_DELAY, else the first free time.

    Returns the slot and the email-time event it is next to (or None), or
    None if there is no free slot."""
    anchors = _email_blocks(*_window(days))
    slots = free_slots(days, minutes, email_blocks=anchors)
    return _pick_slot(slots, [] if urgent else anchors, minutes)


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
