"""Each model's sub-labels and reply drafts side by side, to judge by eye (SHY-12).

Sub-labels and drafts are free text, so there is no right answer to score them
against. This page puts the models next to each other instead.

Usage:
    python -m evals.compare           # the newest predictions of each model
    python -m evals.compare RUN       # one run, e.g. 20261123-001427

Writes eval_runs/compare-<RUN or latest>.html and opens it. Makes no model calls.
"""

import html
import json
import sys
import webbrowser
from pathlib import Path

from .export import EMAILS_FILE
from .prices import dollars

RUNS_DIR = Path(__file__).parent.parent / "eval_runs"
# Column order; models not listed here follow alphabetically.
MODEL_ORDER = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
STAMP_LENGTH = len("20261123-001427")


def prediction_files(stamp: str | None = None) -> dict[str, Path]:
    """{model: predictions file} for one run, or the newest file of each model."""
    files = {}
    for path in sorted(RUNS_DIR.glob("2*-*.jsonl")):  # names start with the time, so they sort by it
        run, model = path.stem[:STAMP_LENGTH], path.stem[STAMP_LENGTH + 1:]
        if stamp is None or run == stamp:
            files[model] = path
    order = {model: i for i, model in enumerate(MODEL_ORDER)}
    return dict(sorted(files.items(), key=lambda item: (order.get(item[0], len(order)), item[0])))


def sublabels(labels: str | None) -> list[tuple[str, str]]:
    """[(parent, sub-label)] of the labels that carry one."""
    return [tuple(name.strip().split("/", 1)) for name in (labels or "").split(";") if "/" in name]


def esc(text) -> str:
    return html.escape(str(text))


def sublabel_cell(record: dict | None) -> str:
    if record is None:
        return '<td class="muted">not in this run</td>'
    pairs = sublabels((record.get("predicted") or {}).get("labels"))
    if not pairs:
        return '<td class="muted">—</td>'
    return "<td>" + "".join(f'<div><span class="muted">{esc(p)} /</span> {esc(s)}</div>' for p, s in pairs) + "</td>"


def draft_cell(record: dict | None) -> str:
    if record is None:
        return '<td class="muted">not in this run</td>'
    predicted = record.get("predicted") or {}
    reply = predicted.get("draft")
    if not reply:
        return '<td class="muted">no draft</td>'
    if reply.get("error"):
        return f'<td class="muted">error: {esc(reply["error"])}</td>'
    cost = dollars(reply["model"], reply["input_tokens"], reply["output_tokens"])
    notes = [f'{esc(reply["model"])} · {reply["input_tokens"]:,} in / {reply["output_tokens"]:,} out · ${cost:.4f}']
    if predicted.get("needs_reply") == "no":
        notes.append("its classifier said no reply needed, so live it would not draft this")
    if reply.get("reply_all"):
        notes.append(f'reply all: {esc(reply.get("reply_all_reason") or "no reason given")}')
    return (f'<td><div class="draft">{esc(reply["body"])}</div>'
            + "".join(f'<div class="muted small">{n}</div>' for n in notes) + "</td>")


def page(files: dict[str, Path], title: str) -> str:
    runs = {model: [json.loads(line) for line in path.read_text().splitlines()] for model, path in files.items()}
    by_model = {model: {r["id"]: r for r in records} for model, records in runs.items()}
    # Every email any of the runs saw, so a short run doesn't hide the others.
    emails = list({r["id"]: r for records in runs.values() for r in records}.values())
    bodies = {}
    if EMAILS_FILE.exists():
        bodies = {e["id"]: e.get("body", "") for e in map(json.loads, EMAILS_FILE.read_text().splitlines())}
    heads = "".join(f"<th>{esc(model)}</th>" for model in files)

    def email_cell(email: dict, with_body: bool) -> str:
        body = ""
        if with_body and bodies.get(email["id"]):
            body = f'<details><summary class="muted small">Email</summary><div class="draft">{esc(bodies[email["id"]])}</div></details>'
        return (f'<td class="email"><div>{esc(email["subject"] or "(no subject)")}</div>'
                f'<div class="muted small">{esc(email["sender"])}</div>'
                f'<div class="muted small">Your labels: {esc(email["expected"]["labels"] or "none")}</div>{body}</td>')

    sub_rows, draft_rows = [], []
    for email in emails:
        records = {model: by_model[model].get(email["id"]) for model in files}
        if any(sublabels(((r or {}).get("predicted") or {}).get("labels")) for r in records.values()):
            sub_rows.append("<tr>" + email_cell(email, False) + "".join(sublabel_cell(r) for r in records.values()) + "</tr>")
        if any(((r or {}).get("predicted") or {}).get("draft") for r in records.values()):
            draft_rows.append("<tr>" + email_cell(email, True) + "".join(draft_cell(r) for r in records.values()) + "</tr>")

    def section(name: str, rows: list[str], empty: str) -> str:
        if not rows:
            return f"<h2>{name}</h2><p>{empty}</p>"
        return (f"<h2>{name} <span class='muted small'>{len(rows)} emails</span></h2>"
                f"<table><thead><tr><th>Email</th>{heads}</tr></thead><tbody>{''.join(rows)}</tbody></table>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Compare · {esc(title)}</title>
<style>
  :root {{ --bg:#f6f7f9; --panel:#fff; --text:#1d2129; --muted:#667085; --line:#e4e7ec; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#111318; --panel:#1a1d24; --text:#e6e8ec; --muted:#98a2b3; --line:#2c313c; }} }}
  body {{ margin:0; padding:24px; font:14px/1.45 system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); }}
  h1 {{ font-size:18px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
  p {{ color:var(--muted); margin:0 0 12px; }}
  nav a {{ color:inherit; margin-right:16px; }}
  table {{ border-collapse:collapse; width:100%; background:var(--panel); table-layout:fixed; }}
  th, td {{ border:1px solid var(--line); padding:8px 10px; vertical-align:top; text-align:left; }}
  th {{ position:sticky; top:0; background:var(--panel); font-size:13px; }}
  .email {{ width:24%; }}
  .muted {{ color:var(--muted); }} .small {{ font-size:12px; margin-top:4px; }}
  .draft {{ white-space:pre-wrap; word-break:break-word; font-size:13px; }}
  details .draft {{ max-height:240px; overflow:auto; margin-top:4px; }}
</style></head><body>
<h1>Sub-labels and drafts, model by model</h1>
<p>{esc(title)} · no right answer here: judge by eye</p>
<nav><a href="#sublabels">Sub-labels</a><a href="#drafts">Drafts</a></nav>
<div id="sublabels">{section("Sub-labels", sub_rows, "No model gave a sub-label.")}</div>
<div id="drafts">{section("Reply drafts", draft_rows, "No drafts in these runs. Drafts are made for emails you marked as needing a reply.")}</div>
</body></html>
"""


def write_page(stamp: str | None = None) -> Path:
    files = prediction_files(stamp)
    if not files:
        raise SystemExit(f"No predictions in {RUNS_DIR}{f' for run {stamp}' if stamp else ''}. Run make eval first.")
    title = f"run {stamp}" if stamp else "newest run of each model: " + ", ".join(p.stem[:STAMP_LENGTH] for p in files.values())
    path = RUNS_DIR / f"compare-{stamp or 'latest'}.html"
    path.write_text(page(files, title))
    return path


def main() -> None:
    path = write_page(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"Sub-labels and drafts side by side: {path}")
    webbrowser.open(path.as_uri())


if __name__ == "__main__":
    main()
