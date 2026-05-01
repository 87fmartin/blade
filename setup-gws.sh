#!/usr/bin/env bash
# Install Google Workspace CLI (gws) and verify the blade agent can read the
# Drive folder that Clay exports spreadsheets into.
#
# Required env vars:
#   GWS_CREDENTIALS_FILE     Path to the service-account JSON key.
#                            Default: $HOME/.config/blade/service-account.json
#   CLAY_DRIVE_FOLDER_ID     The Drive folder ID (the part after /folders/ in
#                            the share URL).
#
# Run on the OpenClaw instance after dropping the service-account JSON in place.
set -euo pipefail

CREDS="${GWS_CREDENTIALS_FILE:-$HOME/.config/blade/service-account.json}"
FOLDER_ID="${CLAY_DRIVE_FOLDER_ID:-}"

if [[ -z "$FOLDER_ID" ]]; then
  echo "ERROR: set CLAY_DRIVE_FOLDER_ID to the Drive folder ID Clay exports into." >&2
  exit 1
fi

if [[ ! -f "$CREDS" ]]; then
  echo "ERROR: service-account JSON not found at $CREDS" >&2
  echo "Place the key there or set GWS_CREDENTIALS_FILE to its path." >&2
  exit 1
fi

# Lock down the key file — gws will refuse a world-readable credentials file.
chmod 600 "$CREDS"

if ! command -v gws >/dev/null 2>&1; then
  echo "Installing gws..."
  if command -v brew >/dev/null 2>&1; then
    brew install googleworkspace-cli
  elif command -v npm >/dev/null 2>&1; then
    npm install -g @googleworkspace/cli
  elif command -v cargo >/dev/null 2>&1; then
    cargo install --git https://github.com/googleworkspace/cli --locked
  else
    echo "ERROR: need brew, npm, or cargo on PATH to install gws." >&2
    echo "Or download a prebuilt binary from https://github.com/googleworkspace/cli/releases" >&2
    exit 1
  fi
fi

export GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE="$CREDS"
echo "gws: $(command -v gws)  ($(gws --version 2>/dev/null || echo 'version unknown'))"
echo "creds: $CREDS"
echo "folder: $FOLDER_ID"
echo

echo "Listing files in the Clay export folder..."
gws drive files list --params "{\"q\": \"'$FOLDER_ID' in parents and trashed = false\", \"pageSize\": 25, \"fields\": \"files(id,name,mimeType,modifiedTime)\"}"

echo
echo "OK — blade can read the folder. Persist these in the OpenClaw env:"
echo "  GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=$CREDS"
echo "  CLAY_DRIVE_FOLDER_ID=$FOLDER_ID"
