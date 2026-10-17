---
name: setup
description: >
  Set up Rightsize for this plugin: check uv, find or clone a Rightsize checkout, install it, verify
  the benchmark data, and say which API keys go in its .env. Usage: /rightsize:setup
---

# /rightsize:setup

The plugin's tools run from a Rightsize checkout. Walk the user through this in order, stopping to
report anything that fails.

1. **uv.** Run `uv --version`. If it's missing, tell the user how to install it
   (https://docs.astral.sh/uv/getting-started/installation/) and stop; don't install it yourself.
2. **The checkout.** Use `$RIGHTSIZE_ROOT` if it's set, else `~/rightsize`. If neither holds a
   checkout (a folder with `configs/suites/`), ask before cloning:
   `git clone https://github.com/Dangsllc/rightsize ~/rightsize`.
   A checkout somewhere else works too: the user sets `RIGHTSIZE_ROOT` to it.
3. **Install.** In the checkout: `uv sync --all-packages`.
4. **Verify the data.** `uv run rightsize data check --suite m1` and `--suite m2`. Both should print
   `ok`. A failure means the data doesn't match its manifest; don't benchmark until it does.
5. **Keys.** Name the variables the user's systems and ladders need (for example
   `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, or a key for their own system's API) and tell them to add
   them to the checkout's `.env`, one `NAME=value` per line. **Never ask the user to paste a key
   into this conversation.** The tools only ever check whether a variable is set.
6. **Smoke test.** Call `list_runs`. Then offer a free baseline run: `plan_run(system="ref:bm25")`,
   shown to the user, then `start(plan_id)` once they agree.
7. **The Workbench.** Tell the user `uv run rightsize ui` (in the checkout) opens a local UI where
   they can watch every run and job the tools start, including ones started from here.

If the tools still don't connect after this, restart Claude Code so the plugin's MCP server
starts again.
