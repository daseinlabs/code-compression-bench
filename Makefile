# code-compression-bench — developer entrypoints.
# The fixed agent is headless Claude Code (Python Claude Agent SDK). Every arm
# runs the SAME Claude Code scaffold against the SAME model (MODEL from .env);
# only the compression layer (the "arm") differs. ANTHROPIC_API_KEY authenticates
# the model; the per-run usage gateway is internal (no config needed).
# Copy .env.example -> .env and fill in before running.

PY ?= python3
# baseline (A0) is the measured control savings are computed against — always include it.
# bear is no-op (sales-gated) so it's omitted; the runner also skips any unready arm.
ARMS ?= baseline,dasein,woz,edgee,rtk,headroom,compresr
TASKS ?= tasks_bloated50.json
WORKERS ?= 8
# Where each task repo is checked out at base_commit (the agent's cwd per instance).
REPO_ROOT ?= $(HOME)/task_repos

.PHONY: help arms prepare smoke bench report selfhost-up selfhost-down

help:
	@echo "Targets:"
	@echo "  arms     - list registered arms and their readiness (env/proxy checks)"
	@echo "  prepare  - check out each task repo at base_commit under REPO_ROOT (run once before smoke)"
	@echo "  smoke    - one task x all ready arms; gates before a full run"
	@echo "  bench    - full run: TASKS x ARMS, writes runs/ + ledger"
	@echo "  report   - build leaderboard + figures from the ledger into results/"
	@echo "  selfhost-up / selfhost-down - bring the local proxy stack up/down"

# List arms and whether each is ready (env keys present, proxy reachable).
arms:
	$(PY) -m bench.cc_runner --list-arms

# Provision per-instance repo checkouts (agent cwd). Idempotent; run once before smoke/bench.
prepare:
	$(PY) -m bench.prepare_repos --tasks $(TASKS) --repo-root $(REPO_ROOT)

# Smoke gate: a single task across every ready arm. Fast fail before scaling.
smoke:
	$(PY) -m bench.cc_runner --tasks $(TASKS) --arms $(ARMS) --limit 1 --workers $(WORKERS) --repo-root $(REPO_ROOT)

# Full benchmark run.
bench:
	$(PY) -m bench.cc_runner --tasks $(TASKS) --arms $(ARMS) --workers $(WORKERS) --repo-root $(REPO_ROOT)

# Build the public leaderboard, figures, and README assets from the ledger.
report:
	$(PY) -m bench.report --out results/

# Self-hosted proxy arms (edgee / rtk / headroom).
selfhost-up:
	docker compose -f selfhost/docker-compose.yml up -d

selfhost-down:
	docker compose -f selfhost/docker-compose.yml down
