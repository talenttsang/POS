# Pikumi POS – Agent Instructions

## Agent Behavior

- If you believe something is correct, say "correct."
- If you believe something is wrong, say "wrong." Do not side with the user just to agree.

## Git

- Commit each finished piece of work and push to GitHub.
- Never commit secrets: tokens, service-account keys, `.env` files.

## Local Environment

Manne runs commands locally on **Windows** (`cmd.exe`): keep commands on one
line, use `python` not `python3`, prefer `git` over shell builtins.

## What this is

A cash register for trade-show booths. Read `README.md` first: it covers the
register, users and the admin portal, events, the data model and migrations,
product uploads, running locally and deploying.

- Live service runs in **store mode** (`POS_MODE=store`: the register sells at
  permanent stores, not events).
- Database: Supabase project `jykfgixmwqfxonccxhri` ("POS", organization
  Talent Production); schema in `supabase/migrations/`, applied once each by
  `apply_migration.py` over the Supabase Management API
  (`SUPABASE_ACCESS_TOKEN`) or a direct connection (`SUPABASE_DB_URL`).
- Credentials: environment variables or a gitignored `.env`, read by
  `lib/secrets.py`. The till master code is `POS_ACCESS_CODE`; never paste it
  into a chat.
- Product photos come from a SkuVault-synced `products` table in the same
  Supabase database, matched on SKU.

## Verifying changes

Test against a throwaway local Postgres (see how the migrations and importer
run with `--via db`), then drive the register and portal in headless Chromium.
Deploy only after that, and smoke-check `/api/health` on the live URL.
