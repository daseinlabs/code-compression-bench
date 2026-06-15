# code-compression-bench — developer entrypoints.
# Every arm runs the SAME model (OPENAI_BASE_URL / OPENAI_API_KEY / MODEL from .env).
# Copy .env.example -> .env and fill in before running.

PY ?= python3
# baseline (A0) is the measured control savings are computed against — always include it.
# bear is no-op (sales-gated) so it's omitted; the runner also skips any unready arm.
ARMS ?= baseline,dasein,woz,edgee,rtk,headroom,compresr
TASKS ?= tasks_bloated50.json
WORKERS ?= 8

.PHONY: help arms smoke bench report selfhost-up selfhost-down

help:
	@echo "Targets:"
	@echo "  arms     - list registered arms and their readiness (env/proxy checks)"
	@echo "  smoke    - one task x all ready arms; gates before a full run"
	@echo "  bench    - full run: TASKS x ARMS, writes runs/ + ledger"
	@echo "  report   - build leaderboard + figures from the ledger into results/"
	@echo "  selfhost-up / selfhost-down - bring the local proxy stack up/down"

# List arms and whether each is ready (env keys present, proxy reachable).
arms:
	$(PY) -m bench.runner --list-arms

# Smoke gate: a single task across every ready arm. Fast fail before scaling.
smoke:
	$(PY) -m bench.runner --tasks $(TASKS) --arms $(ARMS) --limit 1 --workers $(WORKERS)

# Full benchmark run.
bench:
	$(PY) -m bench.runner --tasks $(TASKS) --arms $(ARMS) --workers $(WORKERS)

# Build the public leaderboard, figures, and README assets from the ledger.
report:
	$(PY) -m bench.report --out results/

# Self-hosted proxy arms (edgee / rtk / headroom).
selfhost-up:
	docker compose -f selfhost/docker-compose.yml up -d

selfhost-down:
	docker compose -f selfhost/docker-compose.yml down
