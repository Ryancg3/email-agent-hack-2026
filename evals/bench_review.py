"""Local page to compare two benchmark runs, email by email (SHY-12).

Pick two saved runs; for each email it shows the email and your answer, then
what each run decided: labels and sub-labels, urgency, reply, the draft, the
calendar block and other would-be actions, and every Claude call with its cost.
One run can be flagged as production from the page.

Usage:
    python -m evals.bench_review [--port 8766]   # then open http://127.0.0.1:8766

Listens on 127.0.0.1 only. Reads eval_runs/bench/ on each load, so new runs show
up after a reload. Makes no model calls.
"""

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import review
from .localhost import is_local_request
from .bench import BENCH_DIR, prod_run, run_folders, set_prod, with_derived
from .export import EMAILS_FILE
from .run import grade

PAGE = Path(__file__).parent / "bench_review.html"


def runs() -> list[dict]:
    """Every saved run, newest first, with the fields the run picker shows."""
    out = []
    for folder in reversed(run_folders()):
        s = with_derived({**json.loads((BENCH_DIR / folder / "summary.json").read_text()), "folder": folder})
        out.append({k: s.get(k) for k in (
            "folder", "name", "run", "tags", "emails", "all_right", "all_right_real_inbox", "labels", "urgency",
            "urgent_caught", "replies_caught", "drafts", "dollars_per_email_real_inbox", "emails_per_dollar",
            "dollars_per_month", "errors", "model_classify", "model_draft", "model_agent", "model_loop_runs",
            "model_emails_dated")})
    return out


def results(folder: str) -> dict[str, dict]:
    """One run's records by email id, graded against your answers as they are now."""
    if folder not in run_folders():  # only names from the list: no paths from the browser
        raise ValueError(f"no run {folder}")
    answers = {r["id"]: r for r in review.read_rows()}
    records = {}
    for line in (BENCH_DIR / folder / "results.jsonl").read_text().splitlines():
        rec = json.loads(line)
        if rec["id"] in answers:
            rec["correct"] = grade(answers[rec["id"]], rec["predicted"])
        records[rec["id"]] = rec
    return records


def compare(a: str, b: str) -> dict:
    """Every email either run saw, with your answer and both runs' records."""
    first, second = results(a), results(b)
    answers = {r["id"]: r for r in review.read_rows()}
    bodies = {}
    if EMAILS_FILE.exists():
        bodies = {e["id"]: e for e in map(json.loads, EMAILS_FILE.read_text().splitlines())}
    emails = []
    for email_id in dict.fromkeys([*first, *second]):
        row = answers.get(email_id, {})
        any_rec = first.get(email_id) or second[email_id]
        saved = bodies.get(email_id, {})
        emails.append({
            "id": email_id,
            "subject": any_rec["subject"], "sender": any_rec["sender"],
            "to": saved.get("to", ""), "cc": saved.get("cc", ""), "date": row.get("date", ""),
            "account": row.get("account", ""), "body": saved.get("body", ""),
            "answer": {k: row.get(k, "") for k in ("labels", "urgency", "needs_reply", "notes")},
            "a": first.get(email_id), "b": second.get(email_id),
        })
    return {"emails": emails}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json")

    def _refuse_foreign(self, write: bool = False) -> bool:
        """Answer 403 to requests from other sites (see evals/localhost.py)."""
        if is_local_request(self.headers, self.server.server_address[1], write):
            return False
        self._json(403, {"error": "only this page on 127.0.0.1 may use this server"})
        return True

    def do_GET(self):
        if self._refuse_foreign():
            return
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/runs":
                self._json(200, {"runs": runs(), "prod": prod_run()})
            elif url.path == "/api/compare":
                query = parse_qs(url.query)
                self._json(200, compare(query["a"][0], query["b"][0]))
            else:
                self._json(404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            self._json(400, {"error": str(e)})

    def do_POST(self):
        if self._refuse_foreign(write=True):
            return
        if self.path != "/api/prod":
            return self._json(404, {"error": "not found"})
        try:
            folder = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))["run"]
            if folder not in run_folders():
                raise ValueError(f"no run {folder}")
            self._json(200, {"prod": set_prod(folder)})
        except (ValueError, KeyError) as e:
            self._json(400, {"error": str(e)})

    def log_message(self, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two benchmark runs in the browser.")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()
    if not run_folders():
        raise SystemExit("No benchmark runs yet. Run make bench first.")
    url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Benchmark comparison page: {url}  (Ctrl+C stops it)")
    if not args.no_open:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
