"""Benchmark runs side by side: scores, cost, and every email's labels, sub-labels,
urgency, reply decision and draft (SHY-12). Makes no model calls.

Usage:
    python -m evals.bench_compare               # the newest run of each system name
    python -m evals.bench_compare current no-loop   # by name (newest of each) or run folder

Writes eval_runs/bench/compare.html and opens it.
"""

import html
import json
import sys
import webbrowser
from pathlib import Path

from .bench import BENCH_DIR, STEPS, with_derived
from .export import EMAILS_FILE


def esc(text) -> str:
    return html.escape("" if text is None else str(text))


def pick_runs(wanted: list[str]) -> list[Path]:
    """Run folders, oldest first: the newest of each name, or the ones asked for."""
    folders = sorted(p.parent for p in BENCH_DIR.glob("*/summary.json"))
    newest = {}
    for folder in folders:
        newest[json.loads((folder / "summary.json").read_text())["name"]] = folder
    if not wanted:
        return sorted(newest.values())
    picked = []
    for want in wanted:
        match = newest.get(want) or next((f for f in folders if f.name == want), None)
        if match is None:
            raise SystemExit(f"No benchmark run called {want}. Saved: {', '.join(f.name for f in folders) or 'none'}")
        picked.append(match)
    return picked


def pct(value) -> str:
    return "–" if value is None else f"{value:.0f}%"


def summary_table(summaries: list[dict]) -> str:
    rows = [
        ("Tags", lambda s: esc(s.get("tags") or "–")),
        ("Models", lambda s: f"classify {s['model_classify']}<br>draft {s['model_draft']}<br>agent loop {s['model_agent']}"
                             f"<br>loop runs {s.get('model_loop_runs', 'every email')}"
                             f"<br>emails dated {s.get('model_emails_dated', 'as received')}"),
        ("Emails", lambda s: s["emails"]),
        ("All right", lambda s: pct(s["all_right"])),
        ("All right, real inbox", lambda s: pct(s["all_right_real_inbox"])),
        ("Labels", lambda s: pct(s["labels"])),
        ("Urgency", lambda s: pct(s["urgency"])),
        ("Urgent caught", lambda s: pct(s["urgent_caught"])),
        ("Needs reply", lambda s: pct(s["reply"])),
        ("Replies caught", lambda s: pct(s["replies_caught"])),
        ("Drafts written", lambda s: s["drafts"]),
        *[(f"$ {step} / email", lambda s, k="dollars_" + step.replace(" ", "_"): f"{s[k]:.5f}") for step in STEPS],
        ("$ / email (test set)", lambda s: f"{s['dollars_per_email']:.5f}"),
        ("$ / email (real inbox)", lambda s: f"{s['dollars_per_email_real_inbox']:.5f}"),
        ("Tokens / email", lambda s: f"{s['input_tokens_per_email']:,.0f} in + {s['output_tokens_per_email']:,.0f} out"),
        ("Emails per $1", lambda s: "–" if not s.get("emails_per_dollar") else f"{s['emails_per_dollar']:,.0f}"),
        ("$ / month", lambda s: "–" if s["dollars_per_month"] is None
         else f"{s['dollars_per_month']:.2f} ({s['monthly_emails']:,} emails)"),
        ("Seconds / email", lambda s: f"{s['seconds_per_email']:.1f}"),
        ("Errors", lambda s: s["errors"]),
    ]
    head = "".join(f"<th>{esc(s['name'])}<div class='muted small'>{esc(s['run'])}</div></th>" for s in summaries)
    body = "".join(
        f"<tr><td class='muted'>{title}</td>" + "".join(f"<td>{value(s)}</td>" for s in summaries) + "</tr>"
        for title, value in rows
    )
    return f"<table class='summary'><thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table>"


def result_cell(rec: dict | None) -> str:
    if rec is None:
        return "<td class='muted'>not in this run</td>"
    if rec["predicted"] is None:
        return f"<td class='wrong'>error: {esc(rec['error'])}</td>"
    p, ok = rec["predicted"], rec["correct"]
    mark = lambda good: "ok" if good else "bad"  # noqa: E731
    labels = "<br>".join(
        f"{esc(name.split('/')[0])}" + (f" <span class='sub'>/ {esc(name.split('/', 1)[1])}</span>" if "/" in name else "")
        for name in p["labels"].split("; ") if name
    ) or "<span class='muted'>no label</span>"
    draft = ""
    if rec["draft"]:
        draft = f"<details><summary>Draft</summary><div class='text'>{esc(rec['draft']['body'])}</div></details>"
    elif p["needs_reply"] == "yes":
        draft = "<div class='muted small'>no draft (thread already had one)</div>"
    return (f"<td><div class='{mark(ok['labels'])}'>{labels}</div>"
            f"<div class='{mark(ok['urgency'])} small'>{esc(p['urgency'])}</div>"
            f"<div class='{mark(ok['reply'])} small'>reply: {esc(p['needs_reply'])}</div>{draft}"
            f"<div class='muted small'>${rec['dollars']:.4f} · {rec['seconds']}s</div></td>")


def email_rows(runs: list[list[dict]], bodies: dict) -> str:
    by_run = [{r["id"]: r for r in records} for records in runs]
    seen, rows = set(), []
    for records in runs:
        for rec in records:
            if rec["id"] in seen:
                continue
            seen.add(rec["id"])
            recs = [run.get(rec["id"]) for run in by_run]
            outputs = {json.dumps([r["predicted"], (r["draft"] or {}).get("body")], sort_keys=True) for r in recs if r}
            wrong = any(r and not r["correct"]["all"] for r in recs)
            e = rec["expected"]
            body = bodies.get(rec["id"])
            rows.append(
                f"<tr data-differs='{int(len(outputs) > 1)}' data-wrong='{int(wrong)}'>"
                f"<td><div>{esc(rec['subject'] or '(no subject)')}</div><div class='muted small'>{esc(rec['sender'])}</div>"
                f"<div class='small answer'>Yours: {esc(e['labels'] or 'no label')} · {esc(e['urgency'])} · reply {esc(e['needs_reply'])}</div>"
                + (f"<details><summary class='small'>Email</summary><div class='text'>{esc(body)}</div></details>" if body else "")
                + "</td>" + "".join(result_cell(r) for r in recs) + "</tr>"
            )
    return "".join(rows)


def page(folders: list[Path]) -> str:
    summaries = [with_derived({**json.loads((f / "summary.json").read_text()), "folder": f.name}) for f in folders]
    runs = [[json.loads(line) for line in (f / "results.jsonl").read_text().splitlines()] for f in folders]
    bodies = {}
    if EMAILS_FILE.exists():
        bodies = {e["id"]: e.get("body", "") for e in map(json.loads, EMAILS_FILE.read_text().splitlines())}
    head = "".join(f"<th>{esc(s['name'])}</th>" for s in summaries)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Benchmark comparison</title>
<style>
  :root {{ --bg:#f6f7f9; --panel:#fff; --text:#1d2129; --muted:#667085; --line:#e4e7ec; --ok:#157a4a; --bad:#c4320a; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --bg:#111318; --panel:#1a1d24; --text:#e6e8ec; --muted:#98a2b3; --line:#2c313c; --ok:#4ade80; --bad:#f87171; }} }}
  body {{ margin:0; padding:24px; font:14px/1.45 system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text); }}
  h1 {{ font-size:18px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
  table {{ border-collapse:collapse; width:100%; background:var(--panel); table-layout:fixed; }}
  table.summary {{ width:auto; min-width:50%; table-layout:auto; }}
  th, td {{ border:1px solid var(--line); padding:7px 10px; vertical-align:top; text-align:left; }}
  th {{ background:var(--panel); font-size:13px; }}
  .emails th {{ position:sticky; top:0; }}
  .muted {{ color:var(--muted); }} .small {{ font-size:12px; }} .sub {{ color:var(--muted); }}
  .ok {{ color:var(--ok); }} .bad {{ color:var(--bad); font-weight:600; }} .wrong {{ color:var(--bad); }}
  .answer {{ margin-top:4px; }}
  .text {{ white-space:pre-wrap; word-break:break-word; font-size:12px; max-height:260px; overflow:auto; margin-top:4px; }}
  label {{ margin-right:16px; }}
</style></head><body>
<h1>Benchmark comparison</h1>
<p class="muted">Green = matches your answer, red = differs. Sub-labels and drafts have no right answer: judge them by eye.</p>
{summary_table(summaries)}
<h2>Every email</h2>
<p><label><input type="radio" name="f" value="all" checked> All</label>
<label><input type="radio" name="f" value="differs"> Systems disagree</label>
<label><input type="radio" name="f" value="wrong"> Any system wrong</label></p>
<table class="emails"><thead><tr><th style="width:26%">Email and your answer</th>{head}</tr></thead>
<tbody>{email_rows(runs, bodies)}</tbody></table>
<script>
document.querySelectorAll("input[name=f]").forEach(radio => radio.onchange = () => {{
  document.querySelectorAll("tbody tr[data-differs]").forEach(row => {{
    const f = radio.value;
    row.hidden = (f === "differs" && row.dataset.differs !== "1") || (f === "wrong" && row.dataset.wrong !== "1");
  }});
}});
</script>
</body></html>
"""


def write_page(wanted: list[str]) -> Path:
    folders = pick_runs(wanted)
    if not folders:
        raise SystemExit("No benchmarks yet. Run make bench first.")
    path = BENCH_DIR / "compare.html"
    path.write_text(page(folders))
    return path


def main() -> None:
    path = write_page(sys.argv[1:])
    print(f"Benchmark comparison: {path}")
    webbrowser.open(path.as_uri())


if __name__ == "__main__":
    main()
