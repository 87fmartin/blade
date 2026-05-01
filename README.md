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

The OpenClaw agent reads `blade_alerts.json` and handles delivery.

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
Full Name,Title,Org,LinkedIn URL,Promotion or Job Change,Job Openings,HubSpot Owner ID
```

`Promotion or Job Change` should contain `Promotion` or `Job Change`
(case-insensitive) when the person changed roles, empty otherwise.
`Job Openings` should be an integer; non-numeric values are treated as 0.

## Google Workspace access (for OpenClaw)

When running on OpenClaw, blade reads Clay's spreadsheet exports out of a
shared Drive folder via the `gws` CLI
(<https://github.com/googleworkspace/cli>). One-time setup:

1. **GCP project** — pick or create one and enable the Drive and Sheets APIs:
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
5. **Run the setup script** on the OpenClaw instance:
   ```bash
   export CLAY_DRIVE_FOLDER_ID=<folder-id>
   ./setup-gws.sh
   ```
   It installs `gws`, exports `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`, and
   lists the folder to confirm access.

After that, blade can read sheets headlessly, e.g.:

```bash
gws drive files list --params "{\"q\": \"'$CLAY_DRIVE_FOLDER_ID' in parents\"}"
gws sheets spreadsheets values get --params '{"spreadsheetId": "<id>", "range": "Sheet1!A:Z"}'
```

Scopes: the service account only needs read access to the one shared folder —
no domain-wide delegation, no admin consent.

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
