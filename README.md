# blade

Detects buying signals in a Clay table of sales prospects and emits a JSON
file of per-owner alert drafts plus a digest summary, for an external agent
(e.g. OpenClaw) to deliver. Delivery routing — Slack handles, channels,
email, etc. — lives in the agent, not here.

## Signals

- **Job change / promotion** — `Promotion or Job Change` column is non-empty.
  The script distinguishes `Promotion` from `Job Change` based on the cell text.
- **Hiring spree** — `Job Openings` ≥ 5.

Priority tiers:

| Priority | Condition |
|---|---|
| P1 | Both signals |
| P2 | Job change / promotion only |
| P3 | Hiring spree only |

Prospects with no signal are skipped.

## Outputs

1. **`blade_alerts.json`** — written each run. Contains:
   - `alerts[]` — one entry per signaled prospect: `{owner_id, prospect, priority, text}`. `owner_id` is the HubSpot owner ID (or `null` for unowned contacts); the agent maps it to whatever destination it uses.
   - `digest` — pre-rendered digest text summarizing the run.
   - `run_at` — UTC timestamp.
2. **`blade_state.json`** — dedup state. Same signal set won't re-fire next run; signal-set changes do trigger a re-alert.

The OpenClaw agent reads `blade_alerts.json` and handles delivery, including
mapping `owner_id` → Slack handle via `owner_slack_map.json` (see below).

## Owner → Slack mapping

`owner_slack_map.json` is a config artifact for the OpenClaw agent. blade.py
does not read it. Edit it when the team changes:

```json
{
  "owners": {
    "<hubspot owner id>": "@slack-handle"
  },
  "fallback": "@francisco"
}
```

`fallback` is used by the agent when a prospect's `owner_id` is `null` or not
present in `owners`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If your Clay table uses different column headers, edit the `COL_*` constants
at the top of `blade.py`.

Environment variables (only needed when pulling from Clay; not needed for `--csv`):

```bash
export CLAY_API_KEY=...
export CLAY_TABLE_ID=...
```

## Usage

```bash
# Test against a CSV export — preview to stdout, no files written
python blade.py --csv table.csv --dry-run

# Run against a CSV — writes blade_alerts.json + updates blade_state.json
python blade.py --csv table.csv

# Run against a Google Sheet (via gws — see "Google Workspace access" below)
python blade.py --sheet <spreadsheet-id>
CLAY_SHEET_ID=<id> python blade.py        # or via env var
CLAY_SHEET_ID=<id> python blade.py --sheet-range 'Prospects!A:Z'

# Run against the Clay API
python blade.py

# Wipe dedup state and re-alert every current signal on this run
python blade.py --csv table.csv --reset-state
```

## CSV format

The CSV must have these headers (rename the constants in `blade.py` to remap):

```
Full Name,Title,Org,LinkedIn URL,Work Email,Promotion or Job Change,Job Openings,HubSpot Owner ID
```

`Promotion or Job Change` should contain `Promotion` or `Job Change`
(case-insensitive) when the person changed roles, empty otherwise.
`Job Openings` should be an integer; non-numeric values are treated as 0.
`LinkedIn URL` and `Work Email` are both optional — when present they're
embedded in the alert text (LinkedIn as a clickable link on the prospect's
name, email as a separate line beneath).

## Google Workspace access (for OpenClaw)

When running on OpenClaw, blade reads Clay's spreadsheet exports straight from
the Sheets API using a Google service account. One-time setup:

1. **GCP project** — pick or create one and enable the Drive and Sheets APIs
   (Drive lets `gws`-style folder browsing work; Sheets is what blade itself
   calls):
   ```bash
   gcloud services enable drive.googleapis.com sheets.googleapis.com
   ```
2. **Service account** — create one and download its JSON key:
   ```bash
   gcloud iam service-accounts create blade-agent \
     --display-name="blade OpenClaw agent"
   gcloud iam service-accounts keys create service-account.json \
     --iam-account=blade-agent@<project>.iam.gserviceaccount.com
   ```
3. **Share the Clay folder** with the service account's email
   (`blade-agent@<project>.iam.gserviceaccount.com`) as **Viewer**. The folder
   ID is the path segment after `/folders/` in the share URL.
4. **Drop the key on OpenClaw** at `~/.config/blade/service-account.json`
   (mode 600). `service-account.json` is already gitignored.
5. **Point blade at it.** Set on the OpenClaw box (e.g. in `~/.bashrc`):
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=$HOME/.config/blade/service-account.json
   export CLAY_SHEET_ID=<the sheet's file ID>
   ```
   Then `python blade.py --dry-run` should preview alerts off the live Sheet.

Scopes: blade only requests `spreadsheets.readonly` against the one shared
sheet — no domain-wide delegation, no admin consent.

### Optional: gws CLI for ad-hoc folder browsing

[`googleworkspace/cli`](https://github.com/googleworkspace/cli) (`gws`) is
handy for poking at the Drive folder from the shell — listing files, finding
sheet IDs. **It is not required for blade.py to run.** `setup-gws.sh` installs
it and confirms the SA can see the folder:

```bash
export CLAY_DRIVE_FOLDER_ID=<folder-id>
./setup-gws.sh
```

Note: the npm-distributed `gws` binary is built against glibc 2.39, so it
won't run on older Linux distros. If you hit `GLIBC_2.39 not found`, install
via `cargo install --git https://github.com/googleworkspace/cli --locked`
instead, or skip `gws` entirely — blade doesn't need it.

## Clay API note

`load_clay()` assumes a `GET /v1/tables/{table_id}/rows` endpoint with a Bearer
token. Clay's workspace API surface varies — adjust the URL and response
parsing in `blade.py` to match your workspace. CSV mode is the most reliable
path for testing.

## Dedup behavior

State key is the LinkedIn URL (or `name|org` lowercased if missing). A
prospect is re-alerted when their set of fired signals changes — e.g. P3
(hiring spree) last week → P1 (hiring spree + promotion) this week triggers a
new alert. A change in the openings count alone does not (signal type is
unchanged).

Run with `--reset-state` to drop the file and re-alert every signaled prospect
on the next run.
