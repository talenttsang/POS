"""Pikumi POS: the cash register for the Pikumi K-Beauty Outlets stores.

Serves the register page (static/), the admin portal (/admin) and a JSON API
over the Supabase Postgres database. Cash sales only. Every sale is written as
one pos_transactions row plus its pos_transaction_items, with the totals
checked again on the server so the browser can never ring a wrong amount.

Accounts live in pos_users: each person has a name and a personal PIN
(salted hash). Cashiers unlock the register; admins also open the portal and
approve refunds. The master code (below) opens the portal on its own, which is
how the first admin gets created and how access is recovered.

Env vars:
  SUPABASE_DB_URL     Postgres connection string. If unset, read from Secret
                      Manager (secret SUPABASE_DB_URL) with the runtime identity.
  POS_ACCESS_CODE     Master code for the admin portal. If unset, read from
                      Secret Manager (secret POS_ACCESS_CODE) and re-read every
                      minute, so it can be created or rotated without a deploy.

Venues (pos_events) come in two kinds: an event (a show, with dates) and a
store (a permanent location, with an address). POS_MODE picks which kind this
deployment sells under (default event); the register sells under the venue
chosen in its header and Analytics reads one venue at a time.
  GCP_PROJECT         Secret Manager project. Default shp-ai-bot-2026.
"""

import csv
import hashlib
import hmac
import io
import json
import os
import secrets as pysecrets
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps

import clover
import psycopg
import skuvault
from flask import Flask, Response, abort, jsonify, request, send_from_directory, session
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

app = Flask(__name__, static_folder="static", static_url_path="/static")

PROJECT = os.environ.get("GCP_PROJECT", "shp-ai-bot-2026")
MODE = os.environ.get("POS_MODE", "event")          # event | store
if MODE not in ("event", "store"):
    raise SystemExit("POS_MODE must be event or store")
VENUE = "store" if MODE == "store" else "event"      # the word the UI uses
CENT = Decimal("0.01")

# ---------------------------------------------------------------------------
# Secrets and configuration
# ---------------------------------------------------------------------------

_secret_cache = {}


def secret(name, ttl=60):
    """Env var first, then Secret Manager, cached for ttl seconds. None if absent."""
    value = os.environ.get(name)
    if value:
        return value.strip()
    hit = _secret_cache.get(name)
    if hit and hit[0] > time.time():
        return hit[1]
    value = None
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(
            request={"name": f"projects/{PROJECT}/secrets/{name}/versions/latest"}
        )
        value = response.payload.data.decode("utf-8").strip()
    except Exception as exc:  # noqa: BLE001 - absence is a normal state here
        app.logger.warning("secret %s unavailable: %s", name, type(exc).__name__)
    _secret_cache[name] = (time.time() + ttl, value)
    return value


def access_code():
    return secret("POS_ACCESS_CODE")


# The Flask session cookie is signed with a key derived from the database URL,
# so it survives restarts and is the same across instances.
_db_url = secret("SUPABASE_DB_URL", ttl=3600)
SV = skuvault.Client(lambda name: secret(name, ttl=3600))
CLV = clover.Client(lambda name: secret(name, ttl=3600))
app.secret_key = hashlib.sha256(("pos-session:" + (_db_url or uuid.uuid4().hex)).encode()).digest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("K_SERVICE") is not None,
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 14)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()


def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            url = secret("SUPABASE_DB_URL", ttl=3600)
            if not url:
                abort(503, "database not configured")
            # Supabase's pooler runs in transaction mode: no server-side
            # prepared statements.
            _pool = ConnectionPool(
                url, min_size=0, max_size=4, open=True, kwargs={"prepare_threshold": None, "row_factory": dict_row}
            )
        return _pool


def query(sql, params=None, one=False):
    with pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return (rows[0] if rows else None) if one else rows


# ---------------------------------------------------------------------------
# Auth: personal PINs from pos_users; the master code opens the admin portal
# ---------------------------------------------------------------------------

PIN_RE = re.compile(r"^\d{4,8}$")


def hash_pin(pin, salt=None):
    salt = salt or pysecrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def pin_ok(pin, stored):
    try:
        _, salt, _ = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_pin(pin, salt), stored)


def master_ok(candidate):
    expected = access_code()
    return bool(expected) and hmac.compare_digest(str(candidate).strip(), expected)


# Wrong-PIN throttle, per name, per instance: five misses lock a name for a minute.
_attempts = {}


def throttled(key):
    count, until = _attempts.get(key, (0, 0))
    return count >= 5 and until > time.time()


def record_attempt(key, ok):
    if ok:
        _attempts.pop(key, None)
        return
    count, _ = _attempts.get(key, (0, 0))
    _attempts[key] = (count + 1, time.time() + 60)


def clean_name(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:60]


def find_user(name):
    return query("select * from pos_users where lower(name) = lower(%s) and is_active", (name,), one=True)


def authenticate(name, pin):
    """Return the active user whose name and PIN match, or None. Throttles misses."""
    name = clean_name(name)
    key = name.lower()
    if not name or throttled(key):
        time.sleep(0.5)
        return None
    user = find_user(name)
    ok = bool(user) and pin_ok(str(pin or ""), user["pin_hash"])
    record_attempt(key, ok)
    if not ok:
        time.sleep(0.5)
        return None
    query("update pos_users set last_login_at = now() where id = %s returning id", (user["id"],))
    return user


def unlocked():
    return bool(session.get("user_id"))


def is_admin():
    return session.get("role") == "admin"


def require_unlock(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not unlocked():
            abort(401, "till is locked")
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not is_admin():
            abort(401, "admin sign-in required")
        return fn(*args, **kwargs)
    return wrapper


def approved_by_admin(body):
    """Refunds and other sensitive actions: an admin's name + PIN, or the master code."""
    if master_ok(body.get("code", "")):
        return "master code"
    user = authenticate(body.get("adminName"), body.get("adminPin"))
    if user and user["role"] == "admin":
        return user["name"]
    return None


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def serialise_event(e):
    return {"id": str(e["id"]), "code": e["code"], "name": e["name"], "kind": e.get("kind", "event"),
            "address": e.get("address"),
            "startsOn": e["starts_on"].isoformat() if e.get("starts_on") else None,
            "endsOn": e["ends_on"].isoformat() if e.get("ends_on") else None,
            "svWarehouse": e.get("sv_warehouse"), "svLocation": e.get("sv_location"),
            "cloverStore": e.get("clover_store"), "cloverDevice": e.get("clover_device"),
            "taxRate": str(e.get("tax_rate") or 0), "taxLabel": e.get("tax_label"),
            "currency": e["currency"], "isActive": e["is_active"]}


def active_events():
    return query("select * from pos_events where is_active and kind = %s order by starts_on desc nulls last, name", (MODE,))


def default_event(events):
    """Stores: the first by name. Events: the one running today, else the next, else the latest."""
    from datetime import date
    if MODE == "store":
        return sorted(events, key=lambda e: e["name"].lower())[0] if events else None
    today = date.today()
    running = [e for e in events if e["starts_on"] <= today <= e["ends_on"]]
    if running:
        return running[0]
    upcoming = sorted([e for e in events if e["starts_on"] > today], key=lambda e: e["starts_on"])
    if upcoming:
        return upcoming[0]
    return events[0] if events else None


def event_from_request(required=True):
    """The event named by ?event= (id or code) or the JSON body's eventId, else the default."""
    body = request.get_json(silent=True) or {}
    key = (request.args.get("event") or body.get("eventId") or "").strip()
    if key:
        e = query("select * from pos_events where id::text = %s or code = %s", (key, key), one=True)
        if not e or e.get("kind", "event") != MODE:
            abort(404, f"no such {VENUE}")
        return e
    e = default_event(active_events())
    if not e and required:
        abort(503, f"no {VENUE} set up: add one in the admin portal")
    return e


@app.get("/api/config")
def config():
    events = active_events()
    default = default_event(events)
    return jsonify({
        "mode": MODE,
        "events": [serialise_event(e) for e in events],
        "defaultEventId": str(default["id"]) if default else None,
        "unlocked": unlocked(), "staff": session.get("staff"), "role": session.get("role"),
        "configured": access_code() is not None,
        "hasUsers": query("select exists (select 1 from pos_users where is_active) as e", one=True)["e"],
    })


@app.get("/api/admin/events")
@require_admin
def admin_events():
    return jsonify([serialise_event(e) for e in query("select * from pos_events where kind = %s order by is_active desc, starts_on desc nulls last, name", (MODE,))])


def event_fields(body, creating):
    from datetime import date
    fields = {}
    if "name" in body or creating:
        name = clean_name(body.get("name"))
        if not name:
            abort(400, "name required")
        fields["name"] = name
    for key, col in (("startsOn", "starts_on"), ("endsOn", "ends_on")):
        if key in body or (creating and MODE == "event"):
            v = str(body.get(key) or "").strip()
            if not v and MODE == "store":
                fields[col] = None
                continue
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                abort(400, f"{key} must be YYYY-MM-DD")
            fields[col] = date.fromisoformat(v)
    if "address" in body:
        fields["address"] = re.sub(r"\s+", " ", str(body.get("address") or "")).strip()[:200] or None
    for key, col in (("svWarehouse", "sv_warehouse"), ("svLocation", "sv_location")):
        if key in body:
            v = re.sub(r"[^A-Za-z0-9_\-\[\]]", "", str(body.get(key) or "")).upper()[:40]
            fields[col] = v or None
    for key, col in (("cloverStore", "clover_store"), ("cloverDevice", "clover_device")):
        if key in body:
            fields[col] = re.sub(r"[^A-Za-z0-9_\-]", "", str(body.get(key) or ""))[:80] or None
    if "taxRate" in body:
        # Typed as a percentage in the portal (13 for HST 13%); stored as a fraction.
        raw = str(body.get("taxRate") or "0").strip().rstrip("%")
        try:
            pct = Decimal(raw or "0")
        except Exception:
            abort(400, "tax rate must be a number of percent, e.g. 13")
        if pct < 0 or pct >= 100:
            abort(400, "tax rate must be between 0 and 99 percent")
        fields["tax_rate"] = (pct / 100).quantize(Decimal("0.00001"))
    if "taxLabel" in body:
        fields["tax_label"] = re.sub(r"\s+", " ", str(body.get("taxLabel") or "")).strip()[:40] or None
    if creating:
        fields["kind"] = MODE
    if "currency" in body or creating:
        cur = str(body.get("currency") or "CAD").upper()
        if cur not in ("CAD", "USD", "EUR"):
            abort(400, "currency must be CAD, USD or EUR")
        fields["currency"] = cur
    if "isActive" in body:
        fields["is_active"] = bool(body["isActive"])
    if fields.get("starts_on") and fields.get("ends_on") and fields["ends_on"] < fields["starts_on"]:
        abort(400, "end date is before start date")
    if bool(fields.get("sv_warehouse")) != bool(fields.get("sv_location")):
        abort(400, "give both the SkuVault warehouse and location, or neither")
    if fields.get("clover_device") and not (fields.get("clover_store") or body.get("cloverStore")):
        abort(400, "a Clover device needs a Clover store key")
    if fields.get("sv_warehouse"):
        try:
            SV.warehouse_id(fields["sv_warehouse"])
        except skuvault.SkuVaultError as exc:
            abort(400, str(exc))
    return fields


def slug(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "event"
    code, n = base, 2
    while query("select 1 from pos_events where code = %s", (code,), one=True):
        code, n = f"{base}-{n}", n + 1
    return code


@app.post("/api/admin/events")
@require_admin
def admin_create_event():
    body = request.get_json(silent=True) or {}
    fields = event_fields(body, creating=True)
    code = re.sub(r"[^a-z0-9-]+", "-", str(body.get("code") or "").lower()).strip("-") or slug(fields["name"])
    if query("select 1 from pos_events where code = %s", (code,), one=True):
        abort(409, f"a {VENUE} with that code already exists")
    cols = ["code", *fields, "created_by"]
    e = query(f"insert into pos_events ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) returning *",
              [code, *fields.values(), session.get("staff")], one=True)
    return jsonify(serialise_event(e)), 201


@app.patch("/api/admin/events/<uuid:event_id>")
@require_admin
def admin_update_event(event_id):
    body = request.get_json(silent=True) or {}
    e = query("select * from pos_events where id = %s and kind = %s", (str(event_id), MODE), one=True)
    if not e:
        abort(404, f"no such {VENUE}")
    fields = event_fields(body, creating=False)
    if not fields:
        abort(400, "nothing to change")
    starts = fields.get("starts_on", e["starts_on"]); ends = fields.get("ends_on", e["ends_on"])
    if starts and ends and ends < starts:
        abort(400, "end date is before start date")
    sets = ", ".join(f"{k} = %s" for k in fields)
    e = query(f"update pos_events set {sets} where id = %s returning *", [*fields.values(), str(event_id)], one=True)
    return jsonify(serialise_event(e))


@app.get("/api/users")
def user_names():
    """Active names for the lock screen. Public by design: names only."""
    rows = query("select name, role from pos_users where is_active order by role desc, lower(name)")
    return jsonify(rows)


@app.post("/api/unlock")
def unlock():
    body = request.get_json(silent=True) or {}
    user = authenticate(body.get("staff"), body.get("pin"))
    if not user:
        abort(403, "name or PIN wrong")
    session.permanent = True
    session["user_id"] = str(user["id"])
    session["staff"] = user["name"]
    session["role"] = user["role"]
    return jsonify({"ok": True, "staff": user["name"], "role": user["role"]})


@app.post("/api/lock")
def lock():
    session.clear()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin portal
# ---------------------------------------------------------------------------


def serialise_user(u):
    return {"id": str(u["id"]), "name": u["name"], "role": u["role"], "isActive": u["is_active"],
            "lastLoginAt": u["last_login_at"].isoformat() if u["last_login_at"] else None,
            "createdAt": u["created_at"].isoformat(), "createdBy": u["created_by"]}


@app.post("/api/admin/login")
def admin_login():
    body = request.get_json(silent=True) or {}
    if body.get("code"):
        if access_code() is None:
            abort(503, "master code not configured yet")
        if not master_ok(body["code"]):
            time.sleep(0.5)
            abort(403, "master code wrong")
        session.permanent = True
        session.update(user_id="master", staff="Master code", role="admin")
        return jsonify({"ok": True, "staff": "Master code", "role": "admin"})
    user = authenticate(body.get("staff"), body.get("pin"))
    if not user or user["role"] != "admin":
        abort(403, "name or PIN wrong, or not an admin")
    session.permanent = True
    session.update(user_id=str(user["id"]), staff=user["name"], role=user["role"])
    return jsonify({"ok": True, "staff": user["name"], "role": user["role"]})


@app.get("/api/admin/users")
@require_admin
def admin_users():
    rows = query("select * from pos_users order by is_active desc, role desc, lower(name)")
    return jsonify([serialise_user(u) for u in rows])


@app.post("/api/admin/users")
@require_admin
def admin_create_user():
    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name"))
    role = body.get("role", "cashier")
    pin = str(body.get("pin") or "")
    if not name:
        abort(400, "name required")
    if role not in ("cashier", "admin"):
        abort(400, "role must be cashier or admin")
    if not PIN_RE.match(pin):
        abort(400, "PIN must be 4 to 8 digits")
    if find_user(name):
        abort(409, "an active user with that name already exists")
    user = query(
        "insert into pos_users (name, role, pin_hash, created_by) values (%s, %s, %s, %s) returning *",
        (name, role, hash_pin(pin), session.get("staff")), one=True,
    )
    return jsonify(serialise_user(user)), 201


@app.patch("/api/admin/users/<uuid:user_id>")
@require_admin
def admin_update_user(user_id):
    body = request.get_json(silent=True) or {}
    user = query("select * from pos_users where id = %s", (str(user_id),), one=True)
    if not user:
        abort(404, "no such user")
    sets, params = [], []
    if "name" in body:
        name = clean_name(body["name"])
        if not name:
            abort(400, "name required")
        clash = find_user(name)
        if clash and str(clash["id"]) != str(user_id):
            abort(409, "an active user with that name already exists")
        sets.append("name = %s"); params.append(name)
    if "role" in body:
        if body["role"] not in ("cashier", "admin"):
            abort(400, "role must be cashier or admin")
        sets.append("role = %s"); params.append(body["role"])
    if "pin" in body:
        if not PIN_RE.match(str(body["pin"])):
            abort(400, "PIN must be 4 to 8 digits")
        sets.append("pin_hash = %s"); params.append(hash_pin(str(body["pin"])))
    if "isActive" in body:
        if body["isActive"] and find_user(user["name"]) and str(find_user(user["name"])["id"]) != str(user_id):
            abort(409, "an active user with that name already exists")
        sets.append("is_active = %s"); params.append(bool(body["isActive"]))
    if not sets:
        abort(400, "nothing to change")
    params.append(str(user_id))
    user = query(f"update pos_users set {', '.join(sets)} where id = %s returning *", params, one=True)
    if str(user_id) == session.get("user_id") and (not user["is_active"] or user["role"] != "admin"):
        session.clear()
    return jsonify(serialise_user(user))


# ---------------------------------------------------------------------------
# Admin: products by CSV
# ---------------------------------------------------------------------------

CSV_COLUMNS = ["sku", "name", "category", "price", "cost", "quantity", "barcodes", "is_set", "is_clearance", "is_active", "note"]
CSV_HELP = {
    "sku": "required, unique; matches an existing product to update it",
    "name": "shown on the register; defaults to the SKU",
    "category": "e.g. CN, KR, US, Clearance",
    "price": "selling price, e.g. 8 or 8.00; blank makes the product unsellable",
    "cost": "optional",
    "quantity": "quantity brought to the show, whole number",
    "barcodes": "one or more scan codes separated by |, e.g. 3331684142074|X004MQWD9P",
    "is_set": "TRUE or FALSE",
    "is_clearance": "TRUE or FALSE",
    "is_active": "TRUE or FALSE; FALSE hides the product from the register",
    "note": "optional",
}
MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 5000


def csv_response(rows, filename):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    # A BOM makes Excel open UTF-8 (accents, Chinese) correctly.
    body = "\ufeff" + out.getvalue()
    return Response(body, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@app.get("/api/admin/products/template.csv")
@require_admin
def products_template():
    rows = [CSV_COLUMNS,
            ["EXAMPLE-SKU-1", "Example shampoo 350ml", "KR", "16.00", "7.50", "20", "8809640735561", "FALSE", "FALSE", "TRUE", ""],
            ["EXAMPLE-SKU-2", "Example brush set", "CN", "8", "3.10", "12", "3331684122427|3331684122434", "TRUE", "FALSE", "TRUE", "two barcodes"],
            ["# delete the two example rows above; column meanings: " + "; ".join(f"{k} = {v}" for k, v in CSV_HELP.items())]]
    return csv_response(rows, "pos-products-template.csv")


@app.get("/api/admin/products/export.csv")
@require_admin
def products_export():
    rows = query(
        """select p.sku, p.name, p.category, p.price, p.cost, p.show_qty, p.is_set, p.is_clearance, p.is_active, p.note,
                  coalesce((select string_agg(b.barcode, '|' order by b.barcode) from pos_product_barcodes b where b.product_id = p.id), '') as barcodes
           from pos_products p order by p.category, p.name"""
    )
    def flag(v):
        return "TRUE" if v else "FALSE"
    def num(v):
        return "" if v is None else str(v)
    body = [CSV_COLUMNS] + [[r["sku"], r["name"], r["category"], num(r["price"]), num(r["cost"]), num(r["show_qty"]),
                            r["barcodes"], flag(r["is_set"]), flag(r["is_clearance"]), flag(r["is_active"]), r["note"] or ""]
                           for r in rows]
    return csv_response(body, f"pos-products-{datetime.now(timezone.utc):%Y%m%d}.csv")


def parse_money_cell(text):
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    m = re.fullmatch(r"(?:CA\$|US\$|€|\$)?(-?\d+(?:\.\d+)?)", text)
    if not m:
        raise ValueError(f"not a price: {text!r}")
    value = Decimal(m.group(1))
    if value < 0:
        raise ValueError(f"negative: {text!r}")
    return money(value)


def parse_flag(text):
    t = (text or "").strip().lower()
    if t in ("", None):
        return None
    if t in ("true", "yes", "y", "1", "x"):
        return True
    if t in ("false", "no", "n", "0"):
        return False
    raise ValueError(f"not TRUE/FALSE: {text!r}")


# Other layouts the upload accepts, mapped onto the template's column names.
# Clover's inventory export and the show-prep sheet. Picked by header match.
LAYOUTS = {
    "clover": {"sku": "SKU", "name": "Name", "category": "Categories", "price": "Price", "quantity": "Quantity",
               "barcodes": "Product Code", "hidden": "Hidden?"},
    "show-prep": {"sku": "SKU", "name": "NAME", "category": "类别", "price": "蒙特利尔定价", "cost": "成本",
                  "quantity": "本次展会拿货数量", "barcodes": "UPC", "note": "Note", "clearance": "清仓"},
}
EVENT_PREFIX = re.compile(r"^ESI\s+\w+\s*-\s*", re.IGNORECASE)


def read_rows(raw, filename):
    """Rows of cell strings from a CSV or an Excel workbook (first sheet, or 'Items')."""
    if (filename or "").lower().endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
        try:
            import openpyxl
        except ImportError:
            raise ValueError("Excel upload needs the openpyxl package") from None
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb["Items"] if "Items" in wb.sheetnames else wb[wb.sheetnames[0]]
        for row in ws.iter_rows(values_only=True):
            yield ["" if v is None else (f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)) for v in row]
        wb.close()
        return
    text = raw.decode("utf-8-sig", errors="replace")
    yield from csv.reader(io.StringIO(text))


def parse_products_csv(raw, filename=None):
    """Return (rows, problems).

    Accepts the template layout, a Clover inventory export or the show-prep
    sheet, picked from the header row. A blank cell means "leave unchanged"
    for an existing product and "use the default" for a new one, so a file
    with just sku and price can fix prices without touching anything else.
    Only non-blank cells make it into a record. The same SKU on several rows
    is one product with all their barcodes.
    """
    header = None
    layout = "template"
    rows, problems, seen = [], [], {}
    for n, cells in enumerate(read_rows(raw, filename), start=1):
        cells = [str(c) for c in cells]
        if not any(c.strip() for c in cells) or cells[0].strip().startswith("#"):
            continue
        if header is None:
            raw_header = [c.strip().lstrip("\ufeff") for c in cells]
            for name, cols in LAYOUTS.items():
                if all(t in raw_header for t in cols.values()):
                    layout = name
                    header = [next((k for k, t in cols.items() if t == h), "") for h in raw_header]
                    break
            else:
                header = [h.lower() for h in raw_header]
                unknown = [h for h in header if h and h not in CSV_COLUMNS]
                if "sku" not in header:
                    raise ValueError("the header row must include a 'sku' column (or be a Clover export / show-prep sheet)")
                if unknown:
                    problems.append(f"ignored unknown column(s): {', '.join(unknown)}")
            problems.append(f"layout: {layout}")
            continue
        if len(rows) >= MAX_CSV_ROWS:
            problems.append(f"stopped after {MAX_CSV_ROWS} rows")
            break
        cell = {h: (cells[i].strip() if i < len(cells) else "") for i, h in enumerate(header) if h}
        sku = cell.get("sku", "")
        blank_price = "price" in cell and not cell["price"]
        cell = {k: v for k, v in cell.items() if v}
        if not sku:
            problems.append(f"row {n}: blank sku, skipped")
            continue
        if sku.upper().startswith("EXAMPLE-SKU-") or re.fullmatch(r"test\d*", sku, re.IGNORECASE):
            problems.append(f"row {n}: {sku} looks like an example or test row, skipped")
            continue
        if layout != "template":
            if "name" in cell:
                cell["name"] = EVENT_PREFIX.sub("", cell["name"]) or sku
            if "hidden" in cell:
                cell["is_active"] = "FALSE" if cell.pop("hidden").lower() in ("yes", "true") else "TRUE"
            if "clearance" in cell:
                cell["is_clearance"] = cell.pop("clearance")
            if layout == "show-prep" and "note" in cell:
                cell["is_set"] = "TRUE" if cell["note"].lower() == "set" else "FALSE"
            if "quantity" in cell and not re.fullmatch(r"-?\d+", cell["quantity"]):
                digits = re.findall(r"\d+", cell["quantity"])      # '2+1*' -> 3
                cell["quantity"] = str(sum(int(d) for d in digits)) if digits else ""
                if not cell["quantity"]:
                    cell.pop("quantity")
        if sku in seen:
            first = rows[seen[sku]]
            extra = [b.strip() for b in re.split(r"[|;]", cell.get("barcodes", "")) if b.strip()]
            for code in extra:
                if code not in first.setdefault("barcodes", []):
                    first["barcodes"].append(code)
            if "quantity" in cell and first.get("show_qty") is not None and re.fullmatch(r"-?\d+", cell["quantity"]):
                first["show_qty"] += int(cell["quantity"])
            try:
                other_price = parse_money_cell(cell.get("price", ""))
            except ValueError:
                other_price = None
            if other_price is not None and first.get("price") != other_price:
                problems.append(f"row {n}: {sku} listed again at {other_price} vs {first.get('price')}; keeping the first")
            else:
                problems.append(f"row {n}: {sku} listed again, barcodes merged into row {first['row']}")
            continue
        record = {"sku": sku, "row": n}
        try:
            if "name" in cell:
                record["name"] = cell["name"]
            if "category" in cell:
                record["category"] = cell["category"]
            if "price" in cell:
                record["price"] = parse_money_cell(cell["price"])
            record["blank_price"] = blank_price
            if "cost" in cell:
                record["cost"] = parse_money_cell(cell["cost"])
            if "quantity" in cell:
                q = cell["quantity"]
                if re.fullmatch(r"-?\d+", q):
                    record["show_qty"] = int(q)
                else:
                    problems.append(f"row {n} {sku}: quantity {q!r} is not a whole number, ignored")
            for key in ("is_set", "is_clearance", "is_active"):
                if key in cell:
                    v = parse_flag(cell[key])
                    if v is not None:
                        record[key] = v
            if "note" in cell:
                record["note"] = cell["note"]
            if "barcodes" in cell:
                record["barcodes"] = [b.strip() for b in re.split(r"[|;]", cell["barcodes"]) if b.strip()]
        except ValueError as exc:
            problems.append(f"row {n} {sku}: {exc}, row skipped")
            continue
        seen[sku] = len(rows)
        rows.append(record)
    if header is None:
        raise ValueError("the file is empty")
    # A barcode opens exactly one product: a code seen on two SKUs stays on the first.
    owner = {}
    for r in rows:
        for code in list(r.get("barcodes", [])):
            if code in owner and owner[code] != r["sku"]:
                problems.append(f"barcode {code} is on both {owner[code]} and {r['sku']}; left on {owner[code]}")
                r["barcodes"].remove(code)
            else:
                owner[code] = r["sku"]
    return rows, problems


@app.post("/api/admin/products/import")
@require_admin
def products_import():
    upload = request.files.get("file")
    raw = upload.read(MAX_CSV_BYTES + 1) if upload else request.get_data(cache=False)[: MAX_CSV_BYTES + 1]
    if not raw:
        abort(400, "no file")
    if len(raw) > MAX_CSV_BYTES:
        abort(400, "file larger than 2 MB")
    preview = request.args.get("preview") == "1" or request.form.get("preview") == "1"
    deactivate_missing = request.form.get("deactivate_missing") == "1"
    try:
        rows, problems = parse_products_csv(raw, upload.filename if upload else None)
    except ValueError as exc:
        abort(400, str(exc))

    skus = [r["sku"] for r in rows]
    existing = {r["sku"]: r for r in query("select * from pos_products where sku = any(%s)", (skus,))} if skus else {}
    to_add = [r for r in rows if r["sku"] not in existing]
    to_update = [r for r in rows if r["sku"] in existing]
    # A brand-new product with no price cannot be sold: say so up front.
    for r in to_add:
        if r.get("price") is None:
            problems.append(f"row {r['row']} {r['sku']}: new product without a price, imported as unsellable")
    unchanged = [r for r in to_update if not any(k in r for k in ("name", "category", "price", "cost", "show_qty", "is_set", "is_clearance", "is_active", "note", "barcodes"))]
    if unchanged:
        problems.append(f"{len(unchanged)} row(s) had only a sku and change nothing")
    missing = query("select sku from pos_products where is_active and not (sku = any(%s)) order by sku", (skus,)) if deactivate_missing else []
    summary = {"rows": len(rows), "added": len(to_add), "updated": len(to_update), "deactivated": len(missing),
               "problems": problems, "preview": preview}
    if missing:
        problems.append("not in the file, will be hidden from the register: " + ", ".join(r["sku"] for r in missing[:30]) + (" …" if len(missing) > 30 else ""))
    if preview:
        return jsonify(summary)

    imported_at = datetime.now(timezone.utc)
    with pool().connection() as conn:
        with conn.transaction():
            for r in rows:
                fields = {k: v for k, v in r.items() if k in ("name", "category", "price", "cost", "show_qty", "is_set", "is_clearance", "is_active", "note")}
                is_new = r["sku"] not in existing
                if is_new:
                    fields.setdefault("name", r["sku"])
                    fields.setdefault("category", "Uncategorised")
                    if "price" not in fields:
                        fields["price"] = None
                # A product becomes sellable when it gets a price, unless is_active says otherwise.
                if "price" in fields and "is_active" not in fields:
                    fields["is_active"] = fields["price"] is not None
                fields["sheet"] = json.dumps({k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items() if k not in ("row", "blank_price")}, ensure_ascii=False)
                fields["sheet_row"] = r["row"]
                fields["imported_at"] = imported_at
                if not is_new:
                    sets = ", ".join(f"{k} = %s" for k in fields)
                    conn.execute(f"update pos_products set {sets} where sku = %s", [*fields.values(), r["sku"]])
                else:
                    cols = ["sku", *fields]
                    conn.execute(
                        f"insert into pos_products ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))})",
                        [r["sku"], *fields.values()],
                    )
                if "barcodes" in r:
                    conn.execute("delete from pos_product_barcodes where product_id = (select id from pos_products where sku = %s)", (r["sku"],))
                    for code in r["barcodes"]:
                        conn.execute(
                            """insert into pos_product_barcodes (barcode, product_id)
                               select %s, id from pos_products where sku = %s
                               on conflict (barcode) do update set product_id = excluded.product_id""",
                            (code, r["sku"]),
                        )
            if missing:
                conn.execute("update pos_products set is_active = false where sku = any(%s)", ([r["sku"] for r in missing],))
            totals = conn.execute(
                "select (select count(*) from pos_products) as products, (select count(*) from pos_products where is_active and price is not null) as sellable,"
                " (select count(*) from pos_product_barcodes) as barcodes"
            ).fetchone()
    summary["database"] = totals
    return jsonify(summary)


@app.get("/admin")
def admin_page():
    response = send_from_directory(app.static_folder, "admin.html")
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

# Product photos come from the SkuVault-synced `products` table (pictures[]),
# matched on SKU. A scalar subquery keeps pos_products free of copied URLs and
# picks up new photos as soon as that table syncs.
IMAGE_SQL = ("(select pr.pictures[1] from products pr where pr.sku = p.sku"
             " and cardinality(pr.pictures) > 0 and pr.pictures[1] <> '' limit 1) as image_url")
PRODUCT_COLS = f"p.id, p.sku, p.name, p.category, p.price, p.is_set, p.is_clearance, p.show_qty, {IMAGE_SQL}"


@app.get("/api/products")
@require_unlock
def products():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    where = ["p.is_active", "p.price is not null"]
    params = []
    if category and category != "all":
        where.append("p.category = %s")
        params.append(category)
    if q:
        where.append("(p.name ilike %s or p.sku ilike %s or exists (select 1 from pos_product_barcodes b where b.product_id = p.id and b.barcode = %s))")
        params += [f"%{q}%", f"%{q}%", q]
    # Clearance goes last, in price order; everything else by category then name.
    rows = query(
        f"select {PRODUCT_COLS} from pos_products p where {' and '.join(where)} "
        "order by (p.category = 'Clearance' or p.is_clearance), p.category, "
        "case when p.category = 'Clearance' or p.is_clearance then p.price end, p.name limit 300",
        params,
    )
    stock = venue_stock(event_from_request(required=False))
    return jsonify([serialise_product(r, stock) for r in rows])


def venue_stock(event):
    """{sku: on hand} at the venue's SkuVault location, or None when the venue
    has no location or SkuVault cannot be reached right now."""
    if not event or not event.get("sv_location"):
        return None
    try:
        return SV.stock(event["sv_warehouse"], event["sv_location"])
    except skuvault.SkuVaultError as exc:
        app.logger.warning("stock unavailable: %s", exc)
        return None


@app.get("/api/categories")
@require_unlock
def categories():
    rows = query("select category, count(*) as n from pos_products where is_active and price is not null group by 1 order by (category = 'Clearance'), 1")
    return jsonify(rows)


@app.get("/api/barcode/<code>")
@require_unlock
def by_barcode(code):
    row = query(
        f"select {PRODUCT_COLS} from pos_products p join pos_product_barcodes b on b.product_id = p.id "
        "where b.barcode = %s and p.is_active and p.price is not null",
        (code.strip(),), one=True,
    )
    if not row:
        # Some codes are typed as the SKU itself.
        row = query(f"select {PRODUCT_COLS} from pos_products p where p.sku = %s and p.is_active and p.price is not null",
                    (code.strip(),), one=True)
    if not row:
        abort(404, "no product with that barcode")
    return jsonify(serialise_product(row, venue_stock(event_from_request(required=False))))


def serialise_product(r, stock=None):
    return {
        "id": str(r["id"]), "sku": r["sku"], "name": r["name"], "category": r["category"],
        "price": str(r["price"]), "isSet": r["is_set"], "isClearance": r["is_clearance"], "showQty": r["show_qty"],
        "image": r.get("image_url"),
        "stock": None if stock is None else stock.get(r["sku"], 0),
    }


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def totals(lines, discount, tax_rate=0):
    """Shelf prices, minus the whole-sale discount, plus the venue's tax on what is left."""
    subtotal = sum((money(l["unit_price"]) * l["quantity"] for l in lines), Decimal("0"))
    discount = money(discount or 0)
    if discount < 0 or discount > subtotal:
        abort(400, "discount out of range")
    tax = money((subtotal - discount) * Decimal(str(tax_rate or 0)))
    return subtotal, discount, tax, money(subtotal - discount + tax)


def build_lines(items):
    """Validate cart lines against the catalogue. Prices come from the database, never the browser."""
    if not items:
        abort(400, "no items")
    ids = [str(i.get("productId")) for i in items]
    rows = query(f"select {PRODUCT_COLS} from pos_products p where p.id::text = any(%s) and p.is_active and p.price is not null", (ids,))
    by_id = {str(r["id"]): r for r in rows}
    lines = []
    for n, item in enumerate(items, start=1):
        product = by_id.get(str(item.get("productId")))
        if not product:
            abort(400, f"unknown or inactive product on line {n}")
        qty = int(item.get("quantity") or 0)
        if qty <= 0 or qty > 999:
            abort(400, f"bad quantity on line {n}")
        lines.append({"line_no": n, "product": product, "quantity": qty, "unit_price": product["price"],
                      "barcode": (str(item.get("barcode") or "").strip() or None)})
    return lines


def record_id_of(body):
    record_id = str(body.get("clientRecordId") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", record_id):
        abort(400, "clientRecordId required")
    return record_id


def insert_sale(conn, record_id, event, lines, discount, note, payment_method, cash=None, staff=None, user_id=None, tax_exempt=False):
    """Write the sale and its lines inside the caller's transaction; idempotent on record_id."""
    existing = conn.execute("select * from pos_transactions where client_record_id = %s", (record_id,)).fetchone()
    if existing:
        return existing, False
    # The cashier can switch the venue's tax off for one sale (a tax-exempt customer).
    tax_rate = 0 if tax_exempt else (event.get("tax_rate") or 0)
    subtotal, discount, tax, grand = totals(lines, discount, tax_rate)
    if payment_method == "cash":
        cash = money(cash or 0)
        if cash < grand:
            abort(400, "cash received is less than the total")
        change = money(cash - grand)
    else:
        cash, change = None, Decimal("0.00")
    txn = conn.execute(
        """insert into pos_transactions (client_record_id, event_id, location_id, staff_name, staff_user_id, currency,
             payment_method, subtotal, discount_total, tax_rate, tax_label, tax_total, grand_total, amount_paid,
             cash_received, change_given, note)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           returning *""",
        (record_id, event["id"], event["code"], staff or "—", user_id if user_id != "master" else None,
         event["currency"], payment_method, subtotal, discount, tax_rate,
         (event.get("tax_label") if tax else None), tax, grand, grand, cash, change, note),
    ).fetchone()
    for l in lines:
        p = l["product"]
        conn.execute(
            """insert into pos_transaction_items (transaction_id, line_no, sku, product_name, category, quantity,
                 unit_price, product_id, barcode)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (txn["id"], l["line_no"], p["sku"], p["name"], p["category"], l["quantity"], l["unit_price"], p["id"], l["barcode"]),
        )
    queue_moves(conn, txn, event, "out",
                [{"line_no": l["line_no"], "sku": l["product"]["sku"], "quantity": l["quantity"]} for l in lines])
    return txn, True


@app.post("/api/sales")
@require_unlock
def create_sale():
    body = request.get_json(silent=True) or {}
    record_id = record_id_of(body)
    lines = build_lines(body.get("items") or [])
    event = event_from_request()
    note = (str(body.get("note") or "").strip() or None)
    with pool().connection() as conn:
        with conn.transaction():
            txn, created = insert_sale(conn, record_id, event, lines, body.get("discount"), note, "cash",
                                       cash=body.get("cashReceived"), staff=session.get("staff"), user_id=session.get("user_id"),
                                       tax_exempt=bool(body.get("taxExempt")))
            payload = sale_payload(conn, txn)
    if created:
        start_move_worker()
    return jsonify(payload), 201 if created else 200


# ---------------------------------------------------------------------------
# Card payments on the Clover Flex, through the company's Clover proxy.
# The register starts a charge, the reader prompts the customer, and the
# register polls until the outcome is known. The sale is only written once
# Clover says paid, so the Sales list never shows a declined card.
# ---------------------------------------------------------------------------
_card_threads = {}


@app.post("/api/card/charge")
@require_unlock
def card_charge():
    body = request.get_json(silent=True) or {}
    record_id = record_id_of(body)
    event = event_from_request()
    if not event.get("clover_store"):
        abort(400, f"this {VENUE} has no Clover store key set up")
    lines = build_lines(body.get("items") or [])
    tax_exempt = bool(body.get("taxExempt"))
    subtotal, discount, tax, grand = totals(lines, body.get("discount"), 0 if tax_exempt else (event.get("tax_rate") or 0))
    if grand <= 0:
        abort(400, "nothing to charge")
    note = (str(body.get("note") or "").strip() or None)
    cart = {"items": [{"productId": str(l["product"]["id"]), "quantity": l["quantity"], "barcode": l["barcode"]} for l in lines],
            "discount": str(discount), "note": note, "taxExempt": tax_exempt}
    with pool().connection() as conn:
        row = conn.execute("select * from pos_card_payments where client_record_id = %s", (record_id,)).fetchone()
        if not row:
            row = conn.execute(
                """insert into pos_card_payments (client_record_id, event_id, amount, currency, store_key, device, staff_name, cart)
                   values (%s, %s, %s, %s, %s, %s, %s, %s) returning *""",
                (record_id, event["id"], grand, event["currency"], event["clover_store"], event.get("clover_device"),
                 session.get("staff"), json.dumps(cart)),
            ).fetchone()
            conn.commit()
            t = threading.Thread(target=run_card_charge, args=(record_id, session.get("staff"), session.get("user_id")), daemon=True)
            _card_threads[record_id] = t
            t.start()
    return jsonify(card_status_payload(row)), 202


def run_card_charge(record_id, staff, user_id):
    """Background: ask the reader, then record the sale if it was paid."""
    try:
        with pool().connection() as conn:
            row = conn.execute("select * from pos_card_payments where client_record_id = %s", (record_id,)).fetchone()
        note = f"Pikumi POS {clover.clover_ref(row['id'])}"
        device = row["device"]
        if not device:
            # The proxy keeps the Flex for each store key; use its roster so the request names a reader.
            try:
                device = CLV.device_for(row["store_key"])
            except clover.CloverError as exc:
                app.logger.warning("card charge %s: roster lookup failed: %s", record_id, exc)
            if device:
                with pool().connection() as conn:
                    conn.execute("update pos_card_payments set device = %s where id = %s", (device, row["id"])); conn.commit()
        amount_minor = int(round(float(row["amount"]) * 100))
        outcome, pid, message, raw = CLV.send_payment(row["store_key"], device, amount_minor, clover.clover_ref(row["id"]), record_id, note)
        if outcome == "unknown":
            # As the helper does: a 500 or a lost reply may hide a completed charge. Ask before giving up.
            outcome, pid, message, raw = verify_card(row, amount_minor, message, raw, attempts=3, wait=3)
        settle_card(record_id, outcome, pid, message, raw, staff, user_id)
    except Exception as exc:  # never let a thread die silently
        app.logger.exception("card charge %s: %s", record_id, exc)
        settle_card(record_id, "unknown", None, str(exc)[:300], None, staff, user_id)
    finally:
        _card_threads.pop(record_id, None)


def verify_card(row, amount_minor, message, raw, attempts=1, wait=0):
    """Poll the proxy's payment-status for this attempt. Paid wins; otherwise the
    attempt is failed (the terminal cancelled or declined it) and can be retried."""
    ref = clover.clover_ref(row["id"])
    for i in range(attempts):
        # The proxy's charge record is the surest sign; payment-status is the helper's check.
        try:
            charge = CLV.charge_for(row["store_key"], ref)
        except clover.CloverError as exc:
            app.logger.warning("charges-by-order for %s: %s", ref, exc)
            charge = None
        if charge and str(charge.get("status", "")).upper() == "PAID" and charge.get("payment_id"):
            return "paid", charge["payment_id"], None, {"sent": raw, "charge": charge}
        try:
            status = CLV.payment_status(row["store_key"], ref, amount_minor)
        except clover.CloverError as exc:
            app.logger.warning("payment-status for %s: %s", ref, exc)
            status = None
        if status and status.get("paid"):
            return "paid", status.get("paymentId"), None, {"sent": raw, "status": status}
        if i < attempts - 1:
            time.sleep(wait)
    return "failed", None, message or "Payment cancelled on the terminal", raw


def settle_card(record_id, outcome, pid, message, raw, staff, user_id):
    with pool().connection() as conn:
        with conn.transaction():
            row = conn.execute("select * from pos_card_payments where client_record_id = %s for update", (record_id,)).fetchone()
            if row["status"] == "paid":
                return
            if outcome == "paid":
                event = conn.execute("select * from pos_events where id = %s", (row["event_id"],)).fetchone()
                cart = row["cart"]
                lines = build_lines(cart["items"])
                txn, _ = insert_sale(conn, record_id, event, lines, cart.get("discount"), cart.get("note"), "card",
                                     staff=staff or row["staff_name"], user_id=user_id, tax_exempt=bool(cart.get("taxExempt")))
                conn.execute("update pos_card_payments set status = 'paid', transaction_id = %s, clover_payment_id = %s, message = null, "
                             "response = %s, updated_at = now() where id = %s",
                             (txn["id"], pid, json.dumps(raw)[:4000] if raw is not None else None, row["id"]))
            else:
                conn.execute("update pos_card_payments set status = %s, message = %s, response = %s, updated_at = now() where id = %s",
                             (outcome, (message or "")[:300], json.dumps(raw)[:4000] if raw is not None else None, row["id"]))
    if outcome == "paid":
        start_move_worker()


def card_status_payload(row, sale=None):
    return {"clientRecordId": row["client_record_id"], "status": row["status"], "amount": str(row["amount"]),
            "message": row.get("message"), "paymentId": row.get("clover_payment_id"), "sale": sale}


@app.get("/api/card/<record_id>")
@require_unlock
def card_status(record_id):
    with pool().connection() as conn:
        row = conn.execute("select * from pos_card_payments where client_record_id = %s", (record_id,)).fetchone()
        if not row:
            abort(404, "no such card payment")
        sale = None
        if row["transaction_id"]:
            t = conn.execute("select * from pos_transactions where id = %s", (row["transaction_id"],)).fetchone()
            sale = sale_payload(conn, t)
    return jsonify(card_status_payload(row, sale))


@app.post("/api/card/<record_id>/check")
@require_unlock
def card_check(record_id):
    """Outcome unknown (no reply from the reader): ask Clover whether the order was paid."""
    with pool().connection() as conn:
        row = conn.execute("select * from pos_card_payments where client_record_id = %s", (record_id,)).fetchone()
    if not row:
        abort(404, "no such card payment")
    if row["status"] in ("paid", "failed"):
        return card_status(record_id)
    outcome, pid, message, raw = verify_card(row, int(round(float(row["amount"]) * 100)),
                                             "Clover has no payment for this sale; charge again", None)
    settle_card(record_id, outcome, pid, message, raw, session.get("staff"), session.get("user_id"))
    return card_status(record_id)


@app.get("/api/admin/clover/devices")
@require_admin
def admin_clover_devices():
    store = re.sub(r"[^A-Za-z0-9_\-]", "", request.args.get("store", ""))[:80]
    if not store:
        abort(400, "store key required")
    try:
        return jsonify(CLV.devices(store))
    except clover.CloverError as exc:
        abort(502, str(exc))


# ---------------------------------------------------------------------------
# Inventory sync: each sale line leaves the venue's SkuVault location, and a
# full refund puts it back. Moves are rows first, then sent by a worker, so a
# SkuVault outage never blocks a sale and a failed call can be retried.
# ---------------------------------------------------------------------------
_move_lock = threading.Lock()
MOVE_ATTEMPTS = 5
# SkuVault only accepts reasons defined under Admin > Reasons in the account.
REASON_OUT = os.environ.get("SKUVAULT_REASON_OUT", "Sold")
REASON_IN = os.environ.get("SKUVAULT_REASON_IN", "Return (Bricks & Mortar)")


def queue_moves(conn, txn, event, direction, lines):
    if not event or not event.get("sv_location"):
        return
    for l in lines:
        conn.execute(
            """insert into pos_inventory_moves (transaction_id, line_no, sku, quantity, direction, warehouse, location)
               values (%s, %s, %s, %s, %s, %s, %s) on conflict (transaction_id, line_no, direction) do nothing""",
            (txn["id"], l["line_no"], l["sku"], l["quantity"], direction, event["sv_warehouse"], event["sv_location"]),
        )
        # Reflect it in the register straight away; the worker confirms with SkuVault.
        SV.adjust(event["sv_warehouse"], event["sv_location"], l["sku"], -l["quantity"] if direction == "out" else l["quantity"])


def start_move_worker():
    threading.Thread(target=process_moves, daemon=True).start()


def process_moves():
    """Send every pending or retryable move, one SkuVault call per sale and direction."""
    if not _move_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    done = failed = 0
    try:
        with pool().connection() as conn:
            rows = conn.execute(
                """select m.*, t.transaction_number from pos_inventory_moves m join pos_transactions t on t.id = m.transaction_id
                   where m.status <> 'ok' and m.attempts < %s order by m.created_at""", (MOVE_ATTEMPTS,)
            ).fetchall()
            groups = {}
            for r in rows:
                groups.setdefault((r["transaction_id"], r["direction"], r["warehouse"], r["location"]), []).append(r)
            for (txn_id, direction, wh, loc), group in groups.items():
                number = group[0]["transaction_number"]
                note = f"POS sale #{number}" if direction == "out" else f"POS refund of sale #{number}"
                ids = [r["id"] for r in group]
                try:
                    resp = SV.move(direction, wh, loc, [{"sku": r["sku"], "quantity": r["quantity"], "note": note} for r in group],
                                   REASON_OUT if direction == "out" else REASON_IN)
                    conn.execute("update pos_inventory_moves set status = 'ok', attempts = attempts + 1, last_error = null, response = %s, "
                                 "done_at = now(), updated_at = now() where id = any(%s)", (json.dumps(resp)[:2000], ids))
                    done += len(ids)
                except skuvault.SkuVaultError as exc:
                    app.logger.warning("inventory move failed for sale #%s: %s", number, exc)
                    conn.execute("update pos_inventory_moves set status = 'failed', attempts = attempts + 1, last_error = %s, "
                                 "updated_at = now() where id = any(%s)", (str(exc)[:500], ids))
                    failed += len(ids)
                conn.commit()
    except Exception as exc:  # the worker must never take the request thread down
        app.logger.exception("inventory worker: %s", exc)
    finally:
        _move_lock.release()
    return {"sent": done, "failed": failed}


@app.get("/api/admin/inventory")
@require_admin
def admin_inventory():
    rows = query(
        """select m.id, t.transaction_number as sale, m.line_no, m.sku, m.quantity, m.direction, m.warehouse, m.location,
                  m.status, m.attempts, m.last_error, m.created_at, m.done_at
           from pos_inventory_moves m join pos_transactions t on t.id = m.transaction_id
           order by (m.status = 'ok'), m.created_at desc limit 200""")
    return jsonify([{**r, "created_at": r["created_at"].isoformat(), "done_at": r["done_at"].isoformat() if r["done_at"] else None} for r in rows])


@app.post("/api/admin/inventory/retry")
@require_admin
def admin_inventory_retry():
    query("update pos_inventory_moves set attempts = 0, updated_at = now() where status = 'failed' returning id")
    return jsonify(process_moves())


def sale_payload(conn, txn):
    items = conn.execute(
        "select line_no, sku, product_name, category, quantity, unit_price, line_total from pos_transaction_items "
        "where transaction_id = %s order by line_no", (txn["id"],)
    ).fetchall()
    return serialise_sale(txn, items)


def serialise_sale(t, items):
    def s(v):
        return None if v is None else str(v)
    return {
        "id": str(t["id"]), "number": t["transaction_number"], "status": t["status"], "staff": t["staff_name"],
        "eventId": str(t["event_id"]) if t.get("event_id") else None, "locationId": t["location_id"],
        "currency": t["currency"], "paymentMethod": t["payment_method"],
        "subtotal": s(t["subtotal"]), "discount": s(t["discount_total"]), "total": s(t["grand_total"]),
        "tax": s(t.get("tax_total")), "taxRate": s(t.get("tax_rate")), "taxLabel": t.get("tax_label"),
        "amountPaid": s(t["amount_paid"]), "cashReceived": s(t["cash_received"]), "change": s(t["change_given"]),
        "refundAmount": s(t["refund_amount"]), "refundReason": t["refund_reason"],
        "refundedAt": t["refunded_at"].isoformat() if t["refunded_at"] else None,
        "note": t["note"], "soldAt": t["sold_at"].isoformat(),
        "items": [{"lineNo": i["line_no"], "sku": i["sku"], "name": i["product_name"], "category": i["category"],
                   "quantity": i["quantity"], "unitPrice": s(i["unit_price"]), "lineTotal": s(i["line_total"])} for i in items],
    }


@app.get("/api/sales")
@require_unlock
def list_sales():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 50)), 200)
    event = event_from_request()
    params = [event["id"]]
    where = "event_id = %s"
    if q:
        where += " and (transaction_number::text = %s or staff_name ilike %s)"
        params += [q, f"%{q}%"]
    params.append(limit)
    with pool().connection() as conn:
        txns = conn.execute(f"select * from pos_transactions where {where} order by sold_at desc limit %s", params).fetchall()
        ids = [t["id"] for t in txns]
        items = conn.execute(
            "select transaction_id, line_no, sku, product_name, category, quantity, unit_price, line_total "
            "from pos_transaction_items where transaction_id = any(%s) order by line_no", (ids,)
        ).fetchall() if ids else []
    grouped = {}
    for i in items:
        grouped.setdefault(i["transaction_id"], []).append(i)
    return jsonify([serialise_sale(t, grouped.get(t["id"], [])) for t in txns])


@app.post("/api/sales/<int:number>/refund")
@require_unlock
def refund(number):
    body = request.get_json(silent=True) or {}
    # A refund needs an admin's approval: their name and PIN, or the master code.
    approver = session.get("staff") if is_admin() else approved_by_admin(body)
    if not approver:
        abort(403, "admin approval failed")
    reason = str(body.get("reason") or "").strip()[:200]
    if not reason:
        abort(400, "reason required")
    with pool().connection() as conn:
        with conn.transaction():
            t = conn.execute("select * from pos_transactions where transaction_number = %s for update", (number,)).fetchone()
            if not t:
                abort(404, "no such sale")
            if t["refund_amount"] is not None:
                abort(409, "already refunded")
            amount = money(body.get("amount") or t["amount_paid"])
            if amount <= 0 or amount > t["amount_paid"]:
                abort(400, "refund amount out of range")
            full = amount == money(t["amount_paid"])
            method = "cash"
            # A card sale is refunded to the card through Clover unless the admin chooses cash
            # (for example when Clover refuses the refund).
            if t["payment_method"] == "card" and str(body.get("method") or "card") != "cash":
                # Money goes back to the same card through the Clover proxy before we record it.
                cp = conn.execute("select * from pos_card_payments where transaction_id = %s", (t["id"],)).fetchone()
                if not cp or not cp["clover_payment_id"]:
                    abort(409, "no Clover payment is recorded for this sale")
                try:
                    CLV.refund(cp["store_key"], cp["device"] or CLV.device_for(cp["store_key"]), cp["clover_payment_id"],
                               int(round(float(amount) * 100)), clover.clover_ref(cp["id"]))
                except clover.CloverError as exc:
                    abort(502, f"Clover refund failed: {exc}")
                method = "card"
            t = conn.execute(
                """update pos_transactions set refund_amount = %s, refund_method = %s, refund_reason = %s,
                     refund_approved_by = %s, refunded_at = now(), status = case when %s then 'refunded' else status end
                   where id = %s returning *""",
                (amount, method, reason, approver, full, t["id"]),
            ).fetchone()
            if full:
                event = conn.execute("select * from pos_events where id = %s", (t["event_id"],)).fetchone() if t["event_id"] else None
                items = conn.execute("select line_no, sku, quantity from pos_transaction_items where transaction_id = %s", (t["id"],)).fetchall()
                queue_moves(conn, t, event, "in", items)
            payload = sale_payload(conn, t)
    if full:
        start_move_worker()
    return jsonify(payload)


@app.get("/invoice/<int:number>.pdf")
@require_unlock
def invoice_pdf(number):
    """The sale as a PDF invoice. ?to= adds customer lines (newline separated)
    to the billing block; without it the invoice reads 'Walk-in customer'."""
    from invoice import build_invoice_pdf  # imported here: fpdf2 is only needed for invoices
    bill_to = request.args.get("to", "")[:300]
    with pool().connection() as conn:
        t = conn.execute("select * from pos_transactions where transaction_number = %s", (number,)).fetchone()
        if not t:
            abort(404, "no such sale")
        items = conn.execute(
            "select line_no, sku, product_name, quantity, unit_price, line_total from pos_transaction_items "
            "where transaction_id = %s order by line_no", (t["id"],)
        ).fetchall()
        venue = conn.execute("select name, address from pos_events where id = %s", (t["event_id"],)).fetchone() if t["event_id"] else None
    pdf = build_invoice_pdf(t, items, venue, bill_to)
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="invoice-{number}.pdf"',
                             "Cache-Control": "private, no-store"})


@app.get("/api/summary")
@require_unlock
def summary():
    event = event_from_request()
    rows = query(
        """select sale_day::text as day, sales, inspection_deposits, amount_paid::text,
                  cash_received::text, change_given::text, coalesce(cash_refunded, 0)::text as cash_refunded,
                  net_cash_in_drawer::text
           from pos_daily_cash_summary where location_id = %s and currency = %s order by sale_day desc limit 14""",
        (event["code"], event["currency"]),
    )
    return jsonify(rows)


# ---------------------------------------------------------------------------
# Analytics (admins): what the CFO asks for
# ---------------------------------------------------------------------------

TZ = "America/Toronto"


def date_range():
    """from/to query params as inclusive local dates; defaults to everything."""
    def parse(name):
        v = request.args.get(name, "").strip()
        if not v:
            return None
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            abort(400, f"{name} must be YYYY-MM-DD")
        return v
    return parse("from"), parse("to")


def range_sql(alias="t"):
    """WHERE fragment and params limiting to the event and the local-date range."""
    start, end = date_range()
    event = event_from_request()
    where, params = [f"{alias}.event_id = %s", f"{alias}.status <> 'void'"], [event["id"]]
    if start:
        where.append(f"({alias}.sold_at at time zone %s)::date >= %s"); params += [TZ, start]
    if end:
        where.append(f"({alias}.sold_at at time zone %s)::date <= %s"); params += [TZ, end]
    return " and ".join(where), params


@app.get("/api/analytics")
@require_admin
def analytics():
    where, params = range_sql()
    with pool().connection() as conn:
        kpi = conn.execute(f"""
            select count(*)                                   as sales,
                   coalesce(sum(t.grand_total), 0)            as gross_revenue,
                   coalesce(sum(t.discount_total), 0)         as discounts,
                   coalesce(sum(t.refund_amount), 0)          as refunds,
                   count(*) filter (where t.refund_amount is not null) as refunded_sales,
                   coalesce(sum(t.grand_total), 0) - coalesce(sum(t.refund_amount), 0) as net_revenue,
                   min(t.sold_at) as first_sale, max(t.sold_at) as last_sale,
                   (select coalesce(sum(i.quantity), 0) from pos_transaction_items i join pos_transactions t2 on t2.id = i.transaction_id
                     where {where.replace('t.', 't2.')}) as units
            from pos_transactions t where {where}""", params + params).fetchone()
        margin = conn.execute(f"""
            select coalesce(sum(i.line_total), 0) as line_revenue,
                   coalesce(sum(i.line_total) filter (where p.cost is not null), 0) as revenue_with_cost,
                   coalesce(sum(p.cost * i.quantity) filter (where p.cost is not null), 0) as cost_of_goods
            from pos_transaction_items i
            join pos_transactions t on t.id = i.transaction_id
            left join pos_products p on p.id = i.product_id
            where {where}""", params).fetchone()
        by_day = conn.execute(f"""
            select (t.sold_at at time zone %s)::date::text as day, count(*) as sales,
                   sum(t.grand_total) as revenue, coalesce(sum(t.refund_amount), 0) as refunds,
                   (select coalesce(sum(i.quantity), 0) from pos_transaction_items i where i.transaction_id = any(array_agg(t.id))) as units
            from pos_transactions t where {where}
            group by 1 order by 1""", [TZ] + params).fetchall()
        by_hour = conn.execute(f"""
            select extract(hour from t.sold_at at time zone %s)::int as hour, count(*) as sales, sum(t.grand_total) as revenue
            from pos_transactions t where {where} group by 1 order by 1""", [TZ] + params).fetchall()
        by_category = conn.execute(f"""
            select coalesce(i.category, 'Uncategorised') as category, sum(i.quantity) as units, sum(i.line_total) as revenue,
                   sum(p.cost * i.quantity) filter (where p.cost is not null) as cost_of_goods,
                   sum(i.line_total) filter (where p.cost is not null) as revenue_with_cost
            from pos_transaction_items i join pos_transactions t on t.id = i.transaction_id
            left join pos_products p on p.id = i.product_id
            where {where} group by 1 order by revenue desc""", params).fetchall()
        top_products = conn.execute(f"""
            select i.sku, max(i.product_name) as name, max(i.category) as category, sum(i.quantity) as units,
                   sum(i.line_total) as revenue, max(p.cost) as cost, max(p.show_qty) as brought,
                   case when max(p.cost) is not null then sum(i.line_total) - max(p.cost) * sum(i.quantity) end as margin
            from pos_transaction_items i join pos_transactions t on t.id = i.transaction_id
            left join pos_products p on p.id = i.product_id
            where {where} group by i.sku order by revenue desc, units desc limit 15""", params).fetchall()
        by_staff = conn.execute(f"""
            select t.staff_name as staff, count(*) as sales, sum(t.grand_total) as revenue,
                   coalesce(sum(t.refund_amount), 0) as refunds, coalesce(sum(t.discount_total), 0) as discounts
            from pos_transactions t where {where} group by 1 order by revenue desc""", params).fetchall()
        stock = conn.execute("""
            select coalesce(category, 'Uncategorised') as category,
                   sum(show_qty) filter (where show_qty > 0) as brought,
                   sum(coalesce(sold.units, 0)) as sold,
                   sum(price * greatest(coalesce(show_qty, 0) - coalesce(sold.units, 0), 0)) filter (where price is not null) as remaining_value
            from pos_products p
            left join (select i.product_id, sum(i.quantity) as units from pos_transaction_items i
                       join pos_transactions t on t.id = i.transaction_id where t.event_id = %s and t.status <> 'void'
                       group by i.product_id) sold on sold.product_id = p.id
            where p.is_active group by 1 order by brought desc nulls last""", (event_from_request()["id"],)).fetchall()

    def n(v):
        return None if v is None else (str(v) if isinstance(v, Decimal) else v)
    def rows(rs):
        return [{k: (v.isoformat() if hasattr(v, "isoformat") else n(v)) for k, v in r.items()} for r in rs]
    cogs = margin["cost_of_goods"]; rwc = margin["revenue_with_cost"]
    event = event_from_request()
    return jsonify({
        "event": serialise_event(event),
        "range": {"from": date_range()[0], "to": date_range()[1], "timezone": TZ, "currency": event["currency"]},
        "kpi": {
            "sales": kpi["sales"], "units": int(kpi["units"]), "grossRevenue": n(kpi["gross_revenue"]), "discounts": n(kpi["discounts"]),
            "refunds": n(kpi["refunds"]), "refundedSales": kpi["refunded_sales"], "netRevenue": n(kpi["net_revenue"]),
            "avgBasket": n(money(kpi["gross_revenue"] / kpi["sales"])) if kpi["sales"] else None,
            "lineRevenue": n(margin["line_revenue"]), "revenueWithCost": n(rwc), "costOfGoods": n(cogs),
            "grossMargin": n(money(rwc - cogs)) if rwc else None,
            "grossMarginPct": (float((rwc - cogs) / rwc * 100) if rwc else None),
            "costCoveragePct": (float(rwc / margin["line_revenue"] * 100) if margin["line_revenue"] else None),
            "firstSale": kpi["first_sale"].isoformat() if kpi["first_sale"] else None,
            "lastSale": kpi["last_sale"].isoformat() if kpi["last_sale"] else None,
        },
        "byDay": rows(by_day), "byHour": rows(by_hour), "byCategory": rows(by_category),
        "topProducts": rows(top_products), "byStaff": rows(by_staff), "stock": rows(stock),
    })


@app.get("/api/analytics/export.csv")
@require_admin
def analytics_export():
    where, params = range_sql()
    rows = query(f"""
        select t.transaction_number, t.location_id as event_code, (t.sold_at at time zone %s) as sold_local, t.staff_name, t.status,
               t.subtotal, t.discount_total, t.grand_total, t.cash_received, t.change_given, t.refund_amount, t.refund_reason,
               i.line_no, i.sku, i.product_name, i.category, i.quantity, i.unit_price, i.line_total, p.cost
        from pos_transactions t
        join pos_transaction_items i on i.transaction_id = t.id
        left join pos_products p on p.id = i.product_id
        where {where} order by t.sold_at, t.transaction_number, i.line_no""", [TZ] + params)
    header = ["sale_number", "event", "sold_at_local", "staff", "status", "sale_subtotal", "sale_discount", "sale_total", "cash_received",
              "change_given", "refund_amount", "refund_reason", "line_no", "sku", "product", "category", "quantity", "unit_price",
              "line_total", "unit_cost"]
    body = [header] + [[r["transaction_number"], r["event_code"], r["sold_local"].strftime("%Y-%m-%d %H:%M:%S"), r["staff_name"], r["status"],
                        r["subtotal"], r["discount_total"], r["grand_total"], r["cash_received"], r["change_given"],
                        r["refund_amount"] if r["refund_amount"] is not None else "", r["refund_reason"] or "",
                        r["line_no"], r["sku"], r["product_name"] or "", r["category"] or "", r["quantity"], r["unit_price"],
                        r["line_total"], r["cost"] if r["cost"] is not None else ""] for r in rows]
    return csv_response(body, f"pos-sales-{event_from_request()['code']}-{datetime.now(timezone.utc):%Y%m%d}.csv")


# ---------------------------------------------------------------------------
# Pages and health
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    response = send_from_directory(app.static_folder, "index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/health")
def health():
    # Not /healthz: Google's front end answers that path itself with a 404.
    try:
        query("select 1 as ok", one=True)
        db = "ok"
    except Exception as exc:  # noqa: BLE001
        db = f"error: {type(exc).__name__}"
    return jsonify({"status": "ok" if db == "ok" else "degraded", "db": db, "masterCode": "set" if access_code() else "missing",
                    "time": datetime.now(timezone.utc).isoformat()}), (200 if db == "ok" else 503)


@app.errorhandler(400)
@app.errorhandler(401)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(502)
@app.errorhandler(503)
def api_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": err.description}), err.code
    return err


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
