RUN := uv run --env-file .env
N ?= 5

.DEFAULT_GOAL := help
.PHONY: help setup install migrate superuser dev web scheduler test agent cleanup slots eval-export eval-prefill eval-review eval eval-compare eval-trace eval-monthly bench bench-compare bench-list check-key

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

setup: install .env migrate ## First-time setup: install, create .env, create the database

install: ## Install dependencies into .venv
	uv venv --allow-existing
	uv pip install -r requirements.txt

# Created on first use from .env.example, with generated secret keys and the
# ANTHROPIC_API_KEY from your shell if it is set. Never overwritten.
define MAKE_ENV
import os, secrets
from cryptography.fernet import Fernet
text = open(".env.example").read()
values = {
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "DJANGO_SECRET_KEY": secrets.token_urlsafe(50),
    "TOKEN_KEY": Fernet.generate_key().decode(),
}
for key, value in values.items():
    text = text.replace(f"\n{key}=\n", f"\n{key}={value}\n")
open(".env", "w").write(text)
print("Created .env with generated secret keys.")
if not values["ANTHROPIC_API_KEY"]:
    print("Add your ANTHROPIC_API_KEY to .env before the agent runs.")
endef
export MAKE_ENV

.env:
	@uv run python -c "$$MAKE_ENV"

migrate: .env ## Create or update the database
	$(RUN) python manage.py migrate

superuser: .env ## Create an admin account for /admin/
	$(RUN) python manage.py createsuperuser

dev: migrate ## Run the web server and the scheduler together (Ctrl+C stops both)
	$(MAKE) -j2 web scheduler

web: .env ## Run the web server on http://localhost:8000
	$(RUN) python manage.py runserver

scheduler: .env ## Run the agent for all users every 5 minutes, and on "Run now"
	$(RUN) python manage.py run_scheduler

test: .env ## Run the tests
	$(RUN) python manage.py test accounts

agent: .env ## CLI: process the newest N emails of the token.json account (make agent N=3; DRY_RUN=1 changes nothing)
	$(RUN) python -m triage.pipeline $(N) $(if $(DRY_RUN),--dry-run)

cleanup: .env ## CLI: sub-label cleanup for the token.json account, dry run (make cleanup APPLY=1 deletes)
	$(RUN) python -m triage.sublabel_cleanup $(if $(APPLY),--apply)

slots: .env ## CLI: show free calendar slots of the token.json account
	$(RUN) python -m triage.gcal

eval-export: .env ## Eval: save your newest emails once as the test set, near-duplicates dropped (make eval-export N=300)
	$(RUN) python -m evals.export $(if $(filter command line,$(origin N)),$(N),300) $(if $(FORCE),--force)

eval-prefill: .env ## Eval: guess urgency and needs_reply (and missing labels) for you to correct
	$(RUN) python -m evals.prefill

eval-review: .env ## Eval: check the answers in a local page that saves to eval_data/answers.csv
	$(RUN) python -m evals.review

eval: .env ## Eval: score models on the checked answers and draft replies (make eval MODEL=haiku LIMIT=20 [DRAFT_MODEL=sonnet] [NO_DRAFTS=1]; no MODEL = all)
	$(RUN) python -m evals.run $(if $(MODEL),--model $(MODEL)) $(if $(LIMIT),--limit $(LIMIT)) $(if $(DRAFT_MODEL),--draft-model $(DRAFT_MODEL)) $(if $(NO_DRAFTS),--no-drafts)

eval-compare: ## Eval: each model's sub-labels and reply drafts side by side, to judge by eye (no model calls)
	uv run python -m evals.compare $(RUN_ID)

eval-trace: .env ## Eval: tokens and cost of one email through the whole pipeline, dry run (make eval-trace ID="<gmail id> ...")
	$(RUN) python -m evals.trace $(ID)

eval-monthly: .env ## Eval: emails per month from Gmail and the estimated monthly cost (no model calls)
	$(RUN) python -m evals.monthly $(if $(MONTHS),--months $(MONTHS))

bench: .env ## Benchmark: whole loop on the test set, accuracy + cost; asks before spending (make bench NAME=x [CLASSIFY=haiku DRAFT=sonnet AGENT=sonnet|none LOOP_ALWAYS=1 LIMIT=20])
	$(RUN) python -m evals.bench $(if $(NAME),--name $(NAME)) $(if $(CLASSIFY),--classify $(CLASSIFY)) $(if $(DRAFT),--draft $(DRAFT)) $(if $(AGENT),--agent $(AGENT)) $(if $(LOOP_ALWAYS),--loop-always) $(if $(LIMIT),--limit $(LIMIT))

bench-compare: ## Benchmark: runs side by side in the browser: scores, cost, labels, sub-labels, drafts (make bench-compare RUNS="current no-loop")
	uv run python -m evals.bench_compare $(RUNS)

bench-list: ## Benchmark: every saved run as a table
	uv run python -m evals.bench --list

check-key: .env ## Check which Anthropic credential is used and that it works (free)
	$(RUN) python -m triage.llm
