# blade

Detects buying signals in a Clay table of sales prospects and emits a JSON
file of draft Slack alerts (DMs + channel digest) for an external Slack
posting agent (e.g. OpenClaw) to deliver.

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
   - `dms[]` — one entry per signaled prospect: `{to, owner_id, prospect, priority, text}`. `to` is the Slack handle of the contact owner (or `@francisco` fallback for unowned contacts).
   - `digest` — `{channel, text}` for the `#sales-signals` summary post.
   - `run_at` — UTC timestamp.
2. **`blade_state.json`** — dedup state. Same signal set won't re-fire next run; signal-set changes do trigger a re-alert.

The OpenClaw agent reads `blade_alerts.json` and posts the messages.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Edit `blade.py`:

- `OWNER_TO_SLACK` — HubSpot owner ID → Slack handle (already populated).
- `FALLBACK_SLACK_HANDLE` — handle used when a contact has no mapped owner.
- `DIGEST_CHANNEL` — channel for the digest post.
- Column-name constants at the top, if your Clay table headers differ.

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
