"""How many emails you receive a month, to scale a benchmark's cost to a month (SHY-12).

make bench refreshes it when the saved count is missing or over a month old. It
reads only message ids from Gmail: free, and changes nothing. The count is an upper
bound: the live agent handles only emails that land in the inbox.
"""

import json
from datetime import datetime, timedelta

from triage import gmail

from .export import QUERY
from .scoring import RUNS_DIR

# The agent's own reminders are sent from the account to itself; it skips them.
VOLUME_QUERY = QUERY + " -from:me"
VOLUME_FILE = RUNS_DIR / "volume.json"
MONTHS = 6
MAX_AGE = timedelta(days=30)


def month_starts(now: datetime, months: int) -> list[datetime]:
    """The first moment of each of the last `months` full months, then of this month."""
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    starts = [first]
    for _ in range(months):
        starts.insert(0, (starts[0] - timedelta(days=1)).replace(day=1))
    return starts


def count(start: datetime, end: datetime) -> int:
    # Epoch seconds: plain dates in a Gmail search are read in Pacific time.
    query = f"{VOLUME_QUERY} after:{int(start.timestamp())} before:{int(end.timestamp())}"
    return sum(1 for _ in gmail.search_ids(query, page_size=500))


def count_months(months: int = MONTHS, now: datetime | None = None) -> list[tuple[str, int]]:
    """[(month, emails)] for the last full months."""
    starts = month_starts(now or datetime.now().astimezone(), months)
    return [(f"{s:%b %Y}", count(s, e)) for s, e in zip(starts, starts[1:])]


def monthly_emails(refresh: bool = True) -> int | None:
    """Average emails a month, counted again if the saved count is missing or old."""
    saved = json.loads(VOLUME_FILE.read_text()) if VOLUME_FILE.exists() else None
    fresh = saved and "counted_at" in saved and datetime.now() - datetime.fromisoformat(saved["counted_at"]) < MAX_AGE
    if fresh or not refresh:
        return saved["average"] if saved else None
    print("Counting your emails per month in Gmail (free, read-only)...")
    try:
        months = count_months()
    except Exception as e:  # optional: never lose a paid benchmark over it
        print(f"Couldn't count emails per month ({e}); monthly cost shown as unknown.")
        return saved["average"] if saved else None
    average = round(sum(n for _, n in months) / len(months))
    RUNS_DIR.mkdir(exist_ok=True)
    VOLUME_FILE.write_text(json.dumps({"months": months, "average": average,
                                       "counted_at": datetime.now().isoformat(timespec="seconds")}))
    return average
