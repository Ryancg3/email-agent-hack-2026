"""Local page to check the eval answers (SHY-12): pick the labels, urgency and
needs_reply for each email, saved straight to eval_data/answers.csv.

Usage:
    python -m evals.review [--port 8765]   # then open http://127.0.0.1:8765

Listens on 127.0.0.1 only, so nothing outside this Mac can reach it. It reads
whatever rows answers.csv holds when the page loads, so it keeps working when
the set grows, and keeps any extra columns added by hand.
"""

import argparse
import csv
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from triage import labels

from .export import ANSWER_COLUMNS, ANSWERS_FILE, DESCRIPTIONS_FILE, EMAILS_FILE, LABELS_FILE

PAGE = Path(__file__).parent / "review.html"
URGENCIES = ["not_urgent", "schedule_time", "instant"]
REPLIES = ["yes", "no"]
_lock = threading.Lock()


def read_rows() -> list[dict]:
    with ANSWERS_FILE.open(newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        for column in ANSWER_COLUMNS:
            row[column] = row.get(column) or ""  # older files lack newer columns
    return rows


def write_rows(rows: list[dict]) -> None:
    """Rewrite answers.csv in one step, so a crash never leaves half a file."""
    extra = [c for c in (rows[0] if rows else {}) if c not in ANSWER_COLUMNS and c]
    tmp = ANSWERS_FILE.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        writer = csv.DictWriter(f, ANSWER_COLUMNS + extra, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(ANSWERS_FILE)


def label_options(rows: list[dict]) -> dict:
    """The labels to pick from: the Gmail labels saved at export, the label
    descriptions, and those the answers already use. Top-level only: answers have
    no sub-labels, since the models' sub-labels are compared by eye (make eval-compare)."""
    names = set(json.loads(LABELS_FILE.read_text())) if LABELS_FILE.exists() else set()
    descriptions = DESCRIPTIONS_FILE if DESCRIPTIONS_FILE.exists() else labels.LABELS_FILE
    names |= {f"{e['emoji']} {e['name']}".strip() for e in labels.parse(descriptions.read_text()).values()}
    names |= {name for row in rows for name in row["labels"].split("; ") if name}
    return {"top": sorted({name.split("/")[0] for name in names}, key=labels.key)}


def save(answer: dict) -> dict:
    """Store one answer. labels is a list of top-level labels like ["Work", "Team"]; done
    (default true) marks the row checked, false marks it to do again."""
    with _lock:
        rows = read_rows()
        row = next((r for r in rows if r["id"] == answer.get("id")), None)
        if row is None:
            raise ValueError("no such email")
        top = set(label_options(rows)["top"])
        chosen = [name.strip() for name in answer.get("labels", []) if name.strip()]
        for name in chosen:
            if name not in top:
                raise ValueError(f"not a label: {name}")
        if answer.get("urgency") not in URGENCIES or answer.get("needs_reply") not in REPLIES:
            raise ValueError("pick an urgency and whether it needs a reply")
        row.update(
            labels="; ".join(chosen), urgency=answer["urgency"], needs_reply=answer["needs_reply"],
            notes=answer.get("notes", "").strip(), checked="yes" if answer.get("done", True) else "",
        )
        write_rows(rows)
        return row


def data() -> dict:
    rows = read_rows()
    bodies = {}
    for line in EMAILS_FILE.read_text().splitlines():
        email = json.loads(line)
        bodies[email["id"]] = {k: email.get(k, "") for k in ("to", "cc", "body")}
    return {"rows": rows, "emails": bodies, "options": label_options(rows),
            "urgencies": URGENCIES}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json")

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/data":
            self._json(200, data())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/save":
            return self._json(404, {"error": "not found"})
        try:
            answer = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            self._json(200, save(answer))
        except ValueError as e:
            self._json(400, {"error": str(e)})

    def log_message(self, *args):  # quiet: one line per save is enough
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Check the eval answers in the browser.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()
    if not ANSWERS_FILE.exists():
        raise SystemExit("No eval_data/answers.csv yet. Run make eval-export first.")
    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Review page: {url}  (Ctrl+C stops it)")
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
