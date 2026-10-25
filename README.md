# Pikumi POS

A cash register for trade-show booths: Flask on Render's free plan, Supabase (Postgres)
behind it, an admin portal for users, events and product uploads, and an
analytics tab for the CFO. It started life in the `pos/` folder of
`mannetsang/trustpilot-ai-pipeline` and was extracted here with its history.
The screen design it follows is `design/superhairpieces-pos-reference.html`
(a self-contained prototype; open it through `python -m http.server` as its
banner explains).

Customers are not modelled on purpose. The first use is the cash till at the
ESI Montreal trade show, selling the products on the show-prep Google Sheet.

## The register

`src/cloud_run/` is the register: a Flask service that serves the page in
`static/`, the admin portal at `/admin`, and a JSON API over the Supabase
database. Staff open it in a browser on the booth tablet or laptop, tap their
name, enter their PIN, and sell. A USB or Bluetooth barcode scanner works as a
keyboard: the search box treats a code followed by Enter as a scan.

### Venues: stores or events

A deployment runs in one of two modes, set by the `POS_MODE` environment
variable (`render.yaml`):

- **store** (this deployment): venues are permanent stores with a name,
  address and currency.
- **event**: venues are shows with a name, currency and a start and end date;
  the event running today is the default.

Venues are managed in the admin portal. The register sells at the venue chosen
in its header (remembered per device), every sale carries the venue it was
rung under, and the Sales list and Analytics read one venue at a time.
Archiving a venue hides it from the selector and keeps its sales. Both kinds
live in the `pos_events` table with a `kind` column, so one database can serve
a store deployment and an event deployment side by side.

### Users and the admin portal

Accounts live in `pos_users`. Each person has a name, a role and a personal
PIN of 4 to 8 digits, stored as a salted hash. Cashiers can sell and see
sales. Admins can also open `/admin` to add users, reset PINs, change roles
and deactivate people, and they approve refunds on the register by entering
their name and PIN.

The master code (environment variable `POS_ACCESS_CODE`) opens the admin portal on its own.
That is how the first admin gets created, and the recovery path if every admin
forgets their PIN. Keep it with whoever owns the project, not with booth staff.
Changing it in the Render dashboard restarts the service with the new code.

Five wrong PINs in a row lock a name for one minute.

What it enforces server-side, so the browser can never ring a wrong amount:
every line is priced from `pos_products`, totals are recomputed (there is no
tax: the total is the sum of the shelf prices minus any discount), cash
received must cover the total, and each sale carries a `clientRecordId` from
the browser so a retry after a dropped connection cannot record the sale twice.
Refunds are cash only and need the till code typed again as approval.

Every sale has a PDF invoice at `/invoice/<number>.pdf` (the "Download
invoice" button on the sale-complete screen and "Invoice" in a sale's detail).
It follows the BigCommerce print-invoice layout: company block, billing and
shipping details, order facts, items, totals and comments. The company name
and address are set in `src/cloud_run/invoice.py` and can be overridden per
deployment with the `INVOICE_COMPANY` and `INVOICE_ADDRESS` environment
variables. An optional customer name typed next to the Invoice button goes in
the billing block; otherwise it reads "Walk-in customer".

Configuration is by environment variable (see the docstring in `main.py`):
`SUPABASE_DB_URL` and `POS_ACCESS_CODE` are required, the rest optional.
They are set in the Render dashboard, never committed.

### Running it locally

```
pip install -r src\cloud_run\requirements.txt
set SUPABASE_DB_URL=... && set POS_ACCESS_CODE=1234 && python src\cloud_run\main.py
```

then open http://localhost:8080.

### Uploading a price list in the portal

The Products panel of the admin portal accepts a CSV or Excel file in any of
three layouts, recognised from the header row: the template it offers, a
Clover inventory export (the `Items` sheet of the .xlsx), or the show-prep
sheet. Preview shows what would change; Upload applies it. Blank cells and
absent columns keep existing values, duplicate SKUs merge into one product
with all their barcodes, test and example rows are skipped, and the "hide
products not in this file" box deactivates whatever the file no longer lists.

### Deploying

The register runs on Render's free plan from `render.yaml`:

1. In Render, New > Blueprint, and pick this repository. It builds
   `src/cloud_run/Dockerfile` and redeploys on every push to `main`.
2. Fill in the secrets it asks for: `SUPABASE_DB_URL` (Supabase > Connect >
   Transaction pooler, port 6543, with the database password filled in; the
   direct connection is IPv6-only and Render cannot reach it),
   `POS_ACCESS_CODE`, and `CLOVER_PROXY_SECRET` if the store takes cards.
3. Set up the Supabase project's tables (below), then open
   `https://<service>.onrender.com/api/health`: it reads `"db": "ok"`.

A free service sleeps after 15 minutes without traffic and takes 30-60
seconds to wake, so open the register a minute before trading. A free
Supabase project pauses after a week without activity; an uptime monitor
calling `/api/health` every few minutes keeps both awake, since that route
queries the database.

The session cookie is HTTPS-only. For a plain-http test box on the LAN, set
`COOKIE_SECURE=0`; http://localhost works without it.

## Product photos

Each product's photo is a link in `pos_products.image_url`: the `image_url`
column of the product upload, or the Photo link field of the register's edit
sheet (pencil on a product card). Only http(s) links are accepted; a product
without one, or whose link fails to load, shows its initials.

## Card payments on a Clover Flex

A store names the Clover store key the proxy knows it by (admin portal,
store form; the Markham store is `pikumi`). The Flex serial is optional: the
proxy keeps a reader per store key, and the register uses that one unless a
serial is given. The register then shows a **Card** button next to **Cash**:

1. `POST /api/card/charge` writes a `pos_card_payments` row and asks the
   company's Clover proxy (`CLOVER_PROXY_URL`, default the EC2 host) to start
   the charge on the reader, in a background thread;
2. the register polls `GET /api/card/<record id>` every two seconds while
   the customer taps;
3. **paid** records the sale with `payment_method = 'card'` (no cash detail)
   and queues the inventory move; **failed** shows Clover's message with
   "Try again" (a fresh record id, so a declined attempt is never confused
   with a retry); **unknown** (no reply from the reader) offers "Check with
   Clover", which looks the order up by record id and settles it either way.

Refunding a card sale sends the money back to the card through the proxy
first (`refund_method = 'card'`); card sales never appear in the cash drawer
summary. `CLOVER_SANDBOX=1` uses the proxy's sandbox endpoint; an optional
`CLOVER_PROXY_SECRET` secret is sent as the `x-proxy-secret` header (name
overridable with `CLOVER_PROXY_SECRET_HEADER`). `src/cloud_run/clover.py` is
the whole client.

## Custom domain

Add the domain under the service's Settings > Custom Domains in Render and
create the DNS record it shows; Render issues the certificate. Custom domains
work on the free plan.

## Data

Lives in Supabase (Postgres). Schema files are in `supabase/migrations/` and
are applied once each, in name order, by `apply_migration.py`.

| Table / view | What it holds |
|---|---|
| `pos_transactions` | One row per sale: where, who rang it, currency, totals, cash received and change, refund details. |
| `pos_transaction_items` | One row per line sold: SKU, name, quantity, unit price, line total, and the product and barcode it came from. |
| `pos_products` | The product list, one row per SKU: name, category, price, cost, quantity brought to the show, flags, photo link, and the raw sheet row. |
| `pos_product_barcodes` | Scannable codes (UPC, EAN, ASIN) pointing at a product. Several codes may point at one product. |
| `pos_daily_cash_summary` | View. Per till, per Toronto calendar day, per currency: sales, cash in, change out, cash refunds, net drawer. |
| `pos_events` | Shows and selling periods: code, name, dates, currency, active flag. Sales carry `event_id`. |
| `pos_users` | Register and admin accounts: name, role, salted PIN hash, active flag, last sign-in. |
| `pos_schema_migrations` | Which migration files have been applied. |

Rules the database enforces, so no register build can disagree:

- `grand_total = subtotal - discount_total + tax_total`. Tax is the venue's
  `tax_rate` (admin portal, Tax %; the Markham store is HST 13%) applied to
  what is left after the discount, and `tax_label` is printed on invoices.
  A venue with rate 0 sells tax-free.
- An inspection order records `deposit_total` and `amount_paid` equals it;
  a normal sale has no deposit and `amount_paid` equals `grand_total`.
- Cash sales must carry `cash_received`, and `change_given` must equal
  `cash_received - amount_paid`.
- `payment_method` is `cash` only. Widen the check constraint when card, gift
  card or split tenders are added.
- A refund cannot exceed what was paid; `status = 'refunded'` requires one.
- `client_record_id` is unique: the register queues sales offline and retries,
  and a retry must not create a duplicate.
- Amounts are in the row's `currency`. Never sum across currencies.

Row Level Security is on with no policies, so only server-side code holding
the service key can read or write. Add policies once the register's auth is
decided.

## Setting up a new Supabase project

No local checkout needed: open `supabase/setup.sql` on GitHub (Raw), copy all
of it into Supabase > SQL Editor, and Run, once. It creates every table and
records each migration as applied. Regenerate it with
`python apply_migration.py --write-setup` whenever a migration is added.

A later migration on its own: paste that one file into the SQL Editor, then
run `insert into pos_schema_migrations (name) values ('<file name>');` so
`apply_migration.py` knows it is done.

## Applying a migration

One line, Windows cmd, from the repo root:

```
python apply_migration.py
```

`--list` shows applied and pending files without changing anything.

Credentials come from Secret Manager on `shp-ai-bot-2026` through
`lib/secrets.py` (a copy of the shared helper from the pipeline repo), or from
a gitignored `.env` for local runs. There are two
routes in and the script picks the first that has credentials:

| Route | Secret | Needs | Where it works |
|---|---|---|---|
| `--via api` | `SUPABASE_ACCESS_TOKEN` (a Supabase personal access token) | HTTPS only | Anywhere, including Claude cloud sessions |
| `--via db` | `SUPABASE_DB_URL` | outbound port 6543 (or 5432), plus `pip install "psycopg[binary]"` or `psql` on PATH | Your own machine |

The API route runs the SQL through the Supabase Management API against project
`ngwlwntvoteuafeplobx` (the project reference is an identifier, not a secret;
override it with the `SUPABASE_PROJECT_REF` environment variable).

## Importing the product list

Products come from a Google Sheet shared with the sessions' service account.
Two layouts are recognised from the header row: the show-prep tab
`所有拿货及定价` (the default) and a Clover inventory export (`Items` tab with
`SKU`, `Product Code`, `Price`, `Quantity`, `Categories`, `Hidden?`). One line,
Windows cmd:

```
python import_products.py
python import_products.py --sheet-id <id> --tab Items --skip-sku Test1 --deactivate-missing
```

A blank cell, or a column the layout lacks (Clover has no cost), keeps the
value already in the database, so a price list without costs never wipes the
costs that margin depends on.

`--dry-run` reads and reports without writing. `--deactivate-missing` also
marks SKUs that are no longer on the sheet as not sellable. Re-running is
safe: rows are matched on SKU. What the importer does with the sheet's quirks
is documented at the top of the script: the `ESI Montreal -` prefix is dropped
from names, a SKU on several rows becomes one product with several barcodes,
quantities such as `2+1*` become 3 with the raw text kept, and a blank
Montreal price makes the product inactive.

Prices on the sheet are whole dollars and are charged exactly as listed.

## Adding a migration

Create `supabase/migrations/0002_<what>.sql`. Write it to be safe to re-run
(`if not exists`, `create or replace`) and never edit a file that has already
been applied somewhere.
