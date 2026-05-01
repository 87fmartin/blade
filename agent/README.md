# Blade — OpenClaw agent

Sales-ops agent that runs `blade.py` weekly, drafts Slack messages for
prospects with buying signals, and delivers them after Francisco approves.

## Files

- `../SOUL.md` (top-level) — agent identity, responsibilities, and the
  standing order that drives the weekly workflow. OpenClaw injects it into
  context every session. Lives at the repo root because the repo IS the
  workspace.
- `openclaw.json.example` — Slack channel config snippet. Merge into the
  global OpenClaw config.

## Prerequisites

This agent is a layer on top of `blade.py`, so the blade script must already
work end-to-end on this OpenClaw instance:

- Repo cloned **as the workspace itself** at `~/.openclaw/workspace-blade/`
  (not as a subdirectory of it)
- Python venv at `~/.openclaw/workspace-blade/.venv/` with
  `pip install -r requirements.txt` already run
- Service-account JSON at `~/.config/blade/service-account.json` (mode 600)
- `~/.bashrc` exports `GOOGLE_APPLICATION_CREDENTIALS` and `CLAY_SHEET_ID`
- `python blade.py --dry-run` produces a sane preview
- `owner_slack_map.json` reflects the current owner→handle mapping

If any of those isn't true, fix it before installing the agent — see the
top-level `../README.md`.

## Install

**1. Register the agent (one-time):**

```bash
openclaw agents add blade \
  --workspace ~/.openclaw/workspace-blade \
  --bind slack \
  --model opus
```

SOUL.md is already at the workspace root (it's tracked in the repo). No
`cp` step needed.

**2. Wire up Slack.** Find the Slack channel ID for `#sales-signals` (right-
click the channel in Slack → Copy link; the `C...` chunk at the end is the
ID) and your own Slack user ID (your profile → ⋯ → Copy member ID). Open
`~/.openclaw/openclaw.json` and merge in the contents of
`openclaw.json.example`, replacing `C__SALES_SIGNALS_ID__` and
`U__FRANCISCO_ID__` with the real values.

Slack tokens (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`) are expected to be
exported globally already.

**3. Schedule the weekly run:**

```bash
openclaw cron add \
  --name blade-weekly \
  --cron "0 6 * * 1" \
  --tz America/Los_Angeles \
  --session isolated \
  --message "Run weekly buying-signals delivery per standing orders." \
  --model opus \
  --announce
```

## Smoke test (don't wait for Monday)

```bash
openclaw agents send blade "Run the weekly buying-signals delivery now."
```

Expected: blade.py runs, you get a single DM from Blade with the full
preview (per-prospect drafts + #sales-signals digest), ending with
`Reply *approve* to send, *cancel* to skip, or describe edits.`

Reply `cancel` on the first run to confirm the abort path works without
actually posting anything.

## Updating the agent

`git pull` in `~/.openclaw/workspace-blade/`. Because the repo IS the
workspace, the next session picks up SOUL.md changes automatically — no
copy, no agent re-registration.

## Operational notes

- **Approval gate is per-run.** Approval on last week's preview does not
  carry over. The agent will always wait for an explicit yes.
- **24-hour timeout.** If you don't reply within 24h of the preview DM,
  the run cancels itself — nothing gets sent. Blade will DM you that it
  timed out. Re-trigger manually with `openclaw agents send blade ...` if
  you still want to ship that week's signals.
- **Empty alerts** (no signals fired) → Blade DMs you "No buying signals
  this week." and stops. No approval required because nothing's being sent.
- **blade.py failures** → Blade DMs you stderr and stops. It will not retry,
  so you'll see the error and can fix it before next Monday.
- **Slack DM targeting** uses `username:<handle>` (the agent strips the
  leading `@` from `owner_slack_map.json`). If a handle in the map doesn't
  match a real Slack user, the DM fails and Blade reports it in the final
  delivery confirmation. Update the map and re-trigger if needed.
- **Unowned prospects** route to the `fallback` handle in the map. The
  preview marks these `⚠ unowned` so you can spot them before approving.
