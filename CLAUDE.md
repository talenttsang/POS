# Pikumi POS – Agent Instructions

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

- Hosting: Render free plan, from `render.yaml` (Docker build of
  `src/cloud_run/`), redeployed on every push to `main`, running in **store
  mode** (`POS_MODE=store`: the register sells at permanent stores, not
  events).
- Database: Supabase; schema in `supabase/migrations/`, applied once each by
  `apply_migration.py` over the Supabase Management API (`SUPABASE_ACCESS_TOKEN`,
  with `SUPABASE_PROJECT_REF` naming the project) or a direct connection
  (`SUPABASE_DB_URL`).
- Credentials: the service reads only environment variables, set in the
  Render dashboard (`SUPABASE_DB_URL`, `POS_ACCESS_CODE`, optional
  `CLOVER_PROXY_SECRET`). The repo's scripts resolve theirs through
  `lib/secrets.py` (env or `.env` first, then Secret Manager). The till master
  code is `POS_ACCESS_CODE`; never paste it into a chat.
- Product photos are links in `pos_products.image_url` (product upload or the
  register's edit sheet). There is no SkuVault connection.

## Verifying changes

Test against a throwaway local Postgres (see how the migrations and importer
run with `--via db`), then drive the register and portal in headless Chromium.
Push to `main` only after that (Render deploys it), and smoke-check
`/api/health` on the live URL.
