RUN := uv run --env-file .env
N ?= 5

.DEFAULT_GOAL := help
.PHONY: help setup install migrate superuser dev web scheduler test agent cleanup slots check-key eval-export eval-review bench bench-review labels demo

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

demo: .env ## Web app with a fake Gmail on http://localhost:8765 (demo@example.com / demo-password)
	$(RUN) python -m accounts.demo

labels: .env ## Create users' missing labels in Gmail (make labels EMAIL=you@x.com for one user)
	$(RUN) python manage.py push_labels $(EMAIL)

test: .env ## Run the tests
	$(RUN) python manage.py test accounts

agent: .env ## CLI: process the newest N emails of the token.json account (make agent N=3; DRY_RUN=1 changes nothing)
	$(RUN) python -m triage.pipeline $(N) $(if $(DRY_RUN),--dry-run)

cleanup: .env ## CLI: sub-label cleanup for the token.json account, dry run (make cleanup APPLY=1 deletes)
	$(RUN) python -m triage.sublabel_cleanup $(if $(APPLY),--apply)

slots: .env ## CLI: show free calendar slots of the token.json account
	$(RUN) python -m triage.gcal

eval-export: .env ## Eval: add N new emails to the dataset, pre-filled by the agent, then review them (make eval-export N=50)
	$(RUN) python -m evals.export $(if $(filter command line,$(origin N)),$(N),50)

eval-review: .env ## Eval: reopen the review page to check or fix answers (http://127.0.0.1:8765)
	$(RUN) python -m evals.review

bench: .env ## Eval: run the whole system on the reviewed emails: accuracy + cost; asks before spending (make bench NAME=x [CLASSIFY= DRAFT= AGENT=none LOOP_ALWAYS=1 LIMIT=20])
	$(RUN) python -m evals.bench $(if $(NAME),--name $(NAME)) $(if $(CLASSIFY),--classify $(CLASSIFY)) $(if $(DRAFT),--draft $(DRAFT)) $(if $(AGENT),--agent $(AGENT)) $(if $(LOOP_ALWAYS),--loop-always) $(if $(LIMIT),--limit $(LIMIT))

check-key: .env ## Check which Anthropic credential is used and that it works (free)
	$(RUN) python -m triage.llm

bench-review: ## Eval: page to compare two saved benchmark runs and flag prod (http://127.0.0.1:8766, no model calls)
	uv run python -m evals.bench_review
