# Pikumi POS

A cash register for trade-show booths: Flask on Cloud Run, Supabase (Postgres)
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
variable in `deploy.sh`:

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

The master code (secret `POS_ACCESS_CODE`) opens the admin portal on its own.
That is how the first admin gets created, and the recovery path if every admin
forgets their PIN. Keep it with whoever owns the project, not with booth staff.
The service reads it live and re-reads every minute, so creating or rotating
it needs no redeploy.

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

Configuration is by environment variable (see the docstring in `main.py`).
Two values come from Secret Manager: `SUPABASE_DB_URL`, mounted at deploy
time, and `POS_ACCESS_CODE`, the master code described above.

Create the master code once (one line, Windows cmd; pick your own digits):

```
echo|set /p="123456"|gcloud secrets create POS_ACCESS_CODE --replication-policy=automatic --project=shp-ai-bot-2026 --data-file=-
```

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

`deploy.sh` deploys the service `genc-pos` to Cloud Run in
`northamerica-northeast1` (Montreal) on GCP project `genc-pos-2026` from
source, and prints its URL. The project holds its own copies of the
`SUPABASE_DB_URL` and `POS_ACCESS_CODE` secrets. It runs
from a Claude cloud session, a workstation, or the `deploy.yml` workflow
once the `GCP_SA_KEY` repository secret is set.

The deploying identity needs these roles on `shp-ai-bot-2026`: Cloud Run
Admin, Cloud Build Editor, Artifact Registry Administrator, Storage Admin,
Service Usage Admin, and Service Account User on the Compute Engine default
service account that the service runs as. That runtime account also needs
Secret Manager Secret Accessor for the two secrets above.

## Inventory sync with SkuVault

A store names the SkuVault warehouse that holds its stock (admin portal,
store form; the Markham store is warehouse `PKP`, "PIKUMI Pacific Mall"). An
optional bin narrows it to one location. When a warehouse is set:

- product cards show the on-hand quantity at that location ("3 in stock",
  "out of stock") and a cart line warns when it exceeds stock; selling is
  not blocked;
- each sale takes the sold units from the bins that hold them, fullest first,
  splitting across bins when one is short, with reason `Sold`; a full refund
  puts them back in the same bins with reason `Return (Bricks & Mortar)`.
  Bins read as `PKP[1L1]` and are written as `1L1`. A sale larger than the
  warehouse holds leaves its move failed until stock is received
  (both must exist under SkuVault Admin > Reasons; override with
  `SKUVAULT_REASON_OUT` / `SKUVAULT_REASON_IN`);
- every move is a row in `pos_inventory_moves`, sent by a background worker
  after the sale commits, so SkuVault being down never blocks a sale. Failed
  moves are retried on the next sale or from the admin portal's Inventory
  sync panel.

Credentials are the `SKUVAULT_EMAIL` and `SKUVAULT_PASSWORD` secrets in the
service's project. SkuVault allows about five calls a minute per endpoint, so
stock is cached for three minutes per location and served stale when
throttled. `src/cloud_run/skuvault.py` is the whole client.

## Card payments

The register connects to no card reader. Cards are taken on a separate
terminal: the cashier taps **Card**, runs the amount on the terminal, then
taps **Card approved**, which records the sale with
`payment_method = 'card'` (no cash detail) through the same `POST /api/sales`
as cash. Refunding a card sale records it as back to the card
(`refund_method = 'card'`, done on the terminal) unless the admin picks
**Refund in cash instead**. Card sales never appear in the cash drawer
summary.

## Custom domain

`domain.sh` puts `pos.gencbeauty.com` in front of the service with a global
HTTPS load balancer and a Google-managed certificate, and prints the one DNS
record (an A record for `pos`) the domain owner has to add. It is idempotent.
Check the certificate with
`gcloud compute ssl-certificates describe genc-pos-cert --global --project=genc-pos-2026`;
it reads ACTIVE once the record resolves. The run.app URL keeps working
alongside the domain.

## Data

Lives in Supabase (Postgres). Schema files are in `supabase/migrations/` and
are applied once each, in name order, by `apply_migration.py`.

| Table / view | What it holds |
|---|---|
| `pos_transactions` | One row per sale: where, who rang it, currency, totals, cash received and change, refund details. |
| `pos_transaction_items` | One row per line sold: SKU, name, quantity, unit price, line total, and the product and barcode it came from. |
| `pos_products` | The product list, one row per SKU: name, category, price, cost, quantity brought to the show, flags, and the raw sheet row. |
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
