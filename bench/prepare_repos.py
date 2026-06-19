#!/usr/bin/env python3
"""Provision per-instance repo checkouts for the bench agent.

The fixed agent (headless Claude Code) edits a working copy of each SWE-bench
task's repository at its ``base_commit`` and we capture its work as
``git diff``. The runner's cwd is ``AC_REPO_ROOT/<instance_id>`` but the runner
does NOT create that checkout — this script does, reproducibly:

  * resolve each instance's ``repo`` + ``base_commit`` from the SWE-bench
    Verified dataset (by id),
  * clone each distinct repo ONCE into a cache,
  * add a clean ``git worktree`` at ``base_commit`` for every instance under
    ``AC_REPO_ROOT/<instance_id>``.

Worktrees share the cache's object store (space-cheap) but each has its own
index + working tree, so parallel agents editing different instances of the
same repo never collide, and ``git -C <dir> add -A && git diff --cached`` in the
runner yields exactly that instance's patch.

Usage (on the runner box):
    AC_REPO_ROOT=~/task_repos python -m bench.prepare_repos
    # or: python -m bench.prepare_repos --tasks tasks_bloated50.json --repo-root ~/task_repos
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_DATASET = os.environ.get("AC_SWEBENCH_DATASET_HF", "princeton-nlp/SWE-bench_Verified")
DEFAULT_SPLIT = os.environ.get("AC_SWEBENCH_SPLIT", "test")


def _sh(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True)


def _load_ids(tasks_path: str) -> list[str]:
    data = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        ids = data.get("instances") or data.get("instance_ids") or data.get("ids")
    else:
        ids = data
    if not ids:
        raise SystemExit(f"no instance ids found in {tasks_path}")
    return [str(x) for x in ids]


def _resolve_instances(ids: list[str], dataset: str, split: str) -> dict[str, dict]:
    """id -> {repo, base_commit} from the SWE-bench dataset."""
    try:
        from datasets import load_dataset  # lazy: heavy import
    except ImportError:
        raise SystemExit("pip install datasets  (needed to resolve repo/base_commit)")
    ds = load_dataset(dataset, split=split)
    want = set(ids)
    out: dict[str, dict] = {}
    for row in ds:
        iid = row.get("instance_id")
        if iid in want:
            out[iid] = {"repo": row["repo"], "base_commit": row["base_commit"]}
    missing = want - set(out)
    if missing:
        raise SystemExit(f"{len(missing)} ids not found in {dataset}:{split}: {sorted(missing)[:5]}...")
    return out


def _ensure_cache_clone(repo: str, cache: Path) -> Path:
    cdir = cache / repo.replace("/", "__")
    if not (cdir / ".git").exists():
        cdir.parent.mkdir(parents=True, exist_ok=True)
        print(f"  clone {repo} -> {cdir}")
        _sh(["git", "clone", "-q", f"https://github.com/{repo}.git", str(cdir)])
    return cdir


def _ensure_commit(cdir: Path, base: str) -> None:
    if _sh(["git", "cat-file", "-e", f"{base}^{{commit}}"], cwd=str(cdir), check=False).returncode != 0:
        # base commit not in the default clone (shallow/old) — fetch it explicitly.
        _sh(["git", "fetch", "-q", "origin", base], cwd=str(cdir), check=False)


def _provision_worktree(cdir: Path, dest: Path, base: str) -> None:
    if dest.exists():
        # idempotent: drop and re-add so the tree is clean at base.
        _sh(["git", "worktree", "remove", "--force", str(dest)], cwd=str(cdir), check=False)
        if dest.exists():  # not a registered worktree (stale dir) — nuke it
            _sh(["rm", "-rf", str(dest)], check=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _sh(["git", "worktree", "add", "--force", "--detach", str(dest), base], cwd=str(cdir))


def main() -> None:
    ap = argparse.ArgumentParser(description="Check out each task repo at base_commit for the agent.")
    ap.add_argument("--tasks", default="tasks_bloated50.json")
    ap.add_argument("--repo-root", default=os.environ.get("AC_REPO_ROOT", ""),
                    help="where per-instance checkouts go (AC_REPO_ROOT/<iid>)")
    ap.add_argument("--cache", default=os.environ.get("REPO_CACHE", str(Path.home() / "repo_cache")),
                    help="per-repo full clones live here (shared across instances)")
    ap.add_argument("--dataset", default=DEFAULT_DATASET)
    ap.add_argument("--split", default=DEFAULT_SPLIT)
    a = ap.parse_args()

    repo_root = a.repo_root.strip()
    if not repo_root:
        raise SystemExit("set --repo-root or AC_REPO_ROOT (where the agent's cwd per instance lives)")
    repo_root_p = Path(repo_root).expanduser()
    cache = Path(a.cache).expanduser()
    repo_root_p.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)

    ids = _load_ids(a.tasks)
    print(f"resolving {len(ids)} instances from {a.dataset}:{a.split} ...")
    by_id = _resolve_instances(ids, a.dataset, a.split)

    ok = 0
    for iid in ids:
        repo, base = by_id[iid]["repo"], by_id[iid]["base_commit"]
        try:
            cdir = _ensure_cache_clone(repo, cache)
            _ensure_commit(cdir, base)
            _provision_worktree(cdir, repo_root_p / iid, base)
            ok += 1
            print(f"[ok] {iid:40s} {repo}@{base[:10]}")
        except subprocess.CalledProcessError as e:
            print(f"[FAIL] {iid}: {e.stderr.strip()[:200]}", file=sys.stderr)
    print(f"DONE  {ok}/{len(ids)} provisioned under {repo_root_p}")
    if ok != len(ids):
        sys.exit(1)


if __name__ == "__main__":
    main()
