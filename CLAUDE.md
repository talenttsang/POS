# GenC Beauty POS – Agent Instructions

## Agent Behavior

- If you believe something is correct, say "correct."
- If you believe something is wrong, say "wrong." Do not side with the user just to agree.

## Git

- Commit each finished piece of work and push to GitHub.
- Never commit secrets: tokens, service-account keys, `.env` files.

## Local Environment

Manne runs commands locally on **Windows** (`cmd.exe`): keep commands on one
line, use `python` not `python3`, prefer `gcloud`/`git` over shell builtins.

## What this is

A cash register for trade-show booths. Read `README.md` first: it covers the
register, users and the admin portal, events, the data model and migrations,
product uploads, running locally and deploying.

- Live service: Cloud Run `genc-pos` in `northamerica-northeast1` on GCP
  project `genc-pos-2026` (deploy with `bash deploy.sh`; the session service
  account is an owner of that project).
- Database: Supabase project `ngwlwntvoteuafeplobx`; schema in
  `supabase/migrations/`, applied once each by `apply_migration.py` over the
  Supabase Management API (secret `SUPABASE_ACCESS_TOKEN`) or a direct
  connection (secret `SUPABASE_DB_URL`).
- Credentials: Secret Manager, resolved by `lib/secrets.py`. The service reads
  its secrets from its own project `genc-pos-2026`; scripts run from a session
  default to `shp-ai-bot-2026`, where the same secrets also live. In cloud sessions use `/opt/gcp-venv/bin/python` for
  anything that touches Secret Manager. The till master code is the
  `POS_ACCESS_CODE` secret; never paste it into a chat.
- Product photos come from the SkuVault-synced `products` table in the same
  Supabase database, matched on SKU.

## Verifying changes

Test against a throwaway local Postgres (see how the migrations and importer
run with `--via db`), then drive the register and portal in headless Chromium.
Deploy only after that, and smoke-check `/api/health` on the live URL.
