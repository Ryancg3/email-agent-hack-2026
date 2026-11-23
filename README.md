# Email agent

An email agent that works inside Gmail. For each new email it adds labels, takes the
action that matches the email's urgency, drafts a reply when someone is waiting for
one, and lets Claude take any extra steps that would help. Replies are only ever saved
as drafts, never sent.

## How it works

![How the email agent works](docs/agent-flow.png)

1. **Trigger.** The web scheduler runs every 5 minutes for each enabled user, and
   when someone clicks "Run now". With [Gmail push](docs/gmail-push.md) set up, it
   runs within seconds of an email arriving instead, and polls only as a fallback. The CLI processes the newest N emails of the
   `token.json` account.
2. **Classify** (Claude). Picks 0 to 3 labels from your Gmail labels, using the
   label descriptions each user edits on the web app's Labels tab (new users start
   from [triage/labels.md](triage/labels.md), which the CLI uses). Also returns the urgency, the minutes
   needed to reply, and whether a reply is needed.
3. **Required actions** (code). Applies the labels, then runs the action for the
   urgency: `not_urgent` needs nothing more, `schedule_time` blocks
   calendar time next to other email time (usually within a day), and `instant`
   blocks the first free time, within the hour unless meetings are in the way.
   Reply blocks start 5 to 10 minutes after a meeting ends and never sit right
   before the next one, so after back-to-back meetings they come after the last one.
   Reply time stays within working hours, unless the user turns on "Super-urgent
   emails after hours" in Settings, which lets `instant` blocks go in the evening and at weekends.
4. **Draft reply** (Claude). Runs only when a reply is needed. Follows
   [triage/skills/write-reply/SKILL.md](triage/skills/write-reply/SKILL.md) and saves a Gmail draft.
5. **Extra actions** (Claude). Only for emails that need a reply or calendar time; newsletters,
   receipts and alerts skip it. A tool loop of at most 5 steps that can call the
   tools in [triage/actions.py](triage/actions.py) for anything beyond the required minimum.

See [docs/system-design.md](docs/system-design.md) for the infrastructure, its
trade-offs and where it breaks first. The first agent outline and the old feature
specs are in [docs/archive/](docs/archive/); they are no longer updated.

## Repo layout

| Folder | What's in it |
| --- | --- |
| `triage/` | The email triage itself (no Django): the fixed pipeline (`pipeline.py`), the agent step (`agent.py`), actions, Gmail and Calendar wrappers, labels, skills |
| `accounts/` | The Django app: users, models, views, templates, the scheduler and the per-user runner |
| `config/` | Django settings and URLs |
| `deploy/` | Container entrypoint (`Dockerfile`, `railway.json` and `render.yaml` stay at the root for the hosts) |
| `docs/` | System design, tech stack, hosting and Gmail push guides |

## Setup

Requires [uv](https://docs.astral.sh/uv/).

```bash
make setup
```

This installs the dependencies, creates `.env` from `.env.example` and sets up the
database. Then add your `ANTHROPIC_API_KEY` to `.env`. For the web app, also add
the Google OAuth client values.

## Usage

```bash
make dev        # web app on http://localhost:8000, plus the scheduler
make agent N=3  # CLI: process the newest 3 emails (needs credentials.json)
make agent N=50 DRY_RUN=1  # eval mode: changes nothing, saves the would-be actions to eval_runs/
make test       # run the tests
```

Run `make help` to see every command.

### Benchmark

Measures a whole system (classify, labels, calendar, draft, agent loop) on your
175 checked emails: accuracy against your answers, cost per email and per month.
Runs in dry run, so Gmail and Calendar never change. It asks before spending.

```bash
make bench                              # the current system, saved as "current"
make bench NAME=no-loop AGENT=none      # another system: skip the agent loop
make bench NAME=loop-always LOOP_ALWAYS=1  # the old setup: agent loop on every email
make bench NAME=all-sonnet CLASSIFY=sonnet
make bench-compare                      # newest run of each system, side by side
make bench-list                         # every run as a table
```

### Eval test set

Built from your own emails; everything stays in `eval_data/` (gitignored).

```bash
make eval-export   # pull 300 emails once, drop near-duplicates (max 2 per sender)
make eval-prefill  # the agent guesses labels, urgency and needs_reply for you to correct
make eval-review   # local page on http://127.0.0.1:8765 to check each answer
make eval          # score Haiku, Sonnet and Opus on your checked answers and draft replies (MODEL=haiku for one)
make eval-compare  # each model's sub-labels and drafts side by side, to judge by eye (no model calls)
make eval-trace    # tokens and cost of one email through the whole pipeline, in dry run
make eval-monthly  # emails per month and the estimated monthly cost (no model calls)
```

## Hosting

To keep the agent running when your laptop is off, deploy the Docker image to
Railway (about $5 a month, [docs/deploy-railway.md](docs/deploy-railway.md)) or Render
(about $7.25 a month, [docs/deploy-render.md](docs/deploy-render.md)).
