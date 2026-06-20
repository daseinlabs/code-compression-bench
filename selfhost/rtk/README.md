# rtk — provisioning the real rtk-ai/rtk binary for the `rtk` arm

`rtk` is a **hook arm**, not a proxy (see `arms/README.md` → *rtk*). There is no
server to run: the product is the `rtk` CLI binary, and the arm rewrites
`Bash <cmd>` → `rtk <cmd>` via a Claude Code PreToolUse hook so the binary
compresses shell stdout before it enters context. The only "provisioning" is
making the **real binary** resolvable by the runner.

## The trap this directory fixes

The panel launch is a **non-interactive** ssh:

```sh
gcloud compute ssh cc-bench --command '<… python -m bench.cc_runner --arms rtk …>'
```

A non-interactive ssh **does not source** `~/.bashrc` / `~/.profile`. So a binary
under `~/.local/bin` — added to `PATH` only by those rc files — is **invisible**
to the runner process. `RtkArm.ready()` then runs `rtk --version`, gets nothing,
and returns `(False, "rtk binary not found …")`, so the arm **self-skips and runs
nothing**. (`which rtk` from the same non-interactive shell confirms: none.)

## Fix

Run, on the runner box (cc-bench):

```sh
bash selfhost/rtk/install.sh
```

It symlinks the rtk binary onto **`/usr/local/bin`** — already on the stock
non-interactive `PATH` (`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:…`) —
and verifies resolution under `env -i` (a stripped environment, exactly what the
runner sees). Knobs: `RTK_SRC` (binary to publish; autodetects `~/.local/bin/rtk`
then `command -v rtk`), `RTK_DEST` (default `/usr/local/bin/rtk`), `RTK_INSTALL=1`
(fetch via the official installer if no binary is found).

**Belt-and-suspenders:** also pin the absolute path in the box `.env` (loaded by
`cc_runner.main()` via python-dotenv), so `ready()` and the rewrite resolve it even
without the symlink:

```sh
RTK_BIN=/usr/local/bin/rtk      # or /home/nicks/.local/bin/rtk
```

## Confirm READY under the exact launch form

Before scheduling, prove the arm is READY under the **same non-interactive
`--command`** the panel uses:

```sh
gcloud compute ssh cc-bench --zone us-central1-a --project dasein-473321 \
  --tunnel-through-iap --command \
  '~/code-compression-bench/.venv/bin/python -c "from arms.rtk import RtkArm; print(RtkArm().ready())"'
```

Must print `(True, 'ok (rtk <version>)')`. Then smoke that a trajectory `Bash`
call shows the `rtk ` prefix and compressed stdout.
