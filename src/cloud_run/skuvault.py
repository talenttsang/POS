"""Small SkuVault client for the register: on-hand stock at one location,
and removing / adding items there when sales and refunds happen.

Tokens come from gettokens (email + password) and are cached. Warehouse
codes map to numeric ids through getWarehouses. The inventory read is the
whole-account getInventoryByLocation dump, filtered to one location and
cached for a few minutes; the register is small enough for that.
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("skuvault")
API = "https://app.skuvault.com/api/"
STOCK_TTL = 180


class SkuVaultError(Exception):
    pass


class Client:
    def __init__(self, get_secret):
        """get_secret(name) returns SKUVAULT_EMAIL / SKUVAULT_PASSWORD."""
        self._get_secret = get_secret
        self._tokens = None
        self._warehouses = None
        self._warehouses_at = 0
        self._stock = {}          # (warehouse, location) -> (fetched_at, {sku: qty})
        self._lock = threading.Lock()

    # -- transport ----------------------------------------------------------
    def _post(self, path, body, auth=True, timeout=120, retries=2):
        if auth:
            body = {**self._auth(), **body}
        req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": "genc-pos/1.0"})
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code == 429 and attempt < retries:
                    # SkuVault allows about 5 calls a minute per endpoint and says how long to wait
                    # in the body ("Retry after 7.9 seconds"); wait it out rather than fail the move.
                    m = re.search(r"Retry after ([\d.]+)", detail)
                    time.sleep(min(60, float(m.group(1)) + 1 if m else 20))
                    continue
                if e.code in (401, 403) and auth:
                    self._tokens = None
                raise SkuVaultError(f"SkuVault {path} returned {e.code}: {detail}") from None
            except urllib.error.URLError as e:
                raise SkuVaultError(f"SkuVault {path} unreachable: {e.reason}") from None

    def _auth(self):
        if not self._tokens:
            r = self._post("gettokens", {"Email": self._get_secret("SKUVAULT_EMAIL"),
                                         "Password": self._get_secret("SKUVAULT_PASSWORD")}, auth=False, timeout=30)
            if not r.get("TenantToken"):
                raise SkuVaultError("SkuVault login failed")
            self._tokens = {"TenantToken": r["TenantToken"], "UserToken": r["UserToken"]}
        return self._tokens

    # -- lookups ------------------------------------------------------------
    def warehouse_id(self, code):
        if not self._warehouses or time.time() - self._warehouses_at > 3600:
            r = self._post("inventory/getWarehouses", {})
            self._warehouses = {w["Code"].upper(): int(w["Id"]) for w in r.get("Warehouses", [])}
            self._warehouses_at = time.time()
        wid = self._warehouses.get(code.upper())
        if wid is None:
            raise SkuVaultError(f"SkuVault has no warehouse {code}")
        return wid

    def stock(self, warehouse, location, max_age=STOCK_TTL):
        """{sku: quantity} at one location. Cached; SKUs with no stock are absent."""
        key = (warehouse.upper(), location.upper())
        with self._lock:
            hit = self._stock.get(key)
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
        quantities = {}
        page = 0
        while True:
            try:
                r = self._post("inventory/getInventoryByLocation", {"PageSize": 10000, "PageNumber": page}, retries=0)
            except SkuVaultError:
                # Throttled or down: a stale answer beats none for a register showing badges.
                if hit:
                    return hit[1]
                raise
            items = r.get("Items") or {}
            for sku, rows in items.items():
                for row in rows:
                    if (str(row.get("WarehouseCode", "")).upper(), str(row.get("LocationCode", "")).upper()) == key:
                        quantities[sku] = quantities.get(sku, 0) + int(row.get("Quantity") or 0)
            if len(items) < 10000:
                break
            page += 1
        with self._lock:
            self._stock[key] = (time.time(), quantities)   # (fetched at, {sku: qty})
        return quantities

    def adjust(self, warehouse, location, sku, delta):
        """Shift the cached quantity after a move so the register reflects it at once."""
        key = (warehouse.upper(), location.upper())
        with self._lock:
            hit = self._stock.get(key)
            if hit:
                hit[1][sku] = max(0, hit[1].get(sku, 0) + delta)

    # -- moves --------------------------------------------------------------
    def move(self, direction, warehouse, location, lines, reason):
        """Remove (direction 'out') or add ('in') lines [{sku, quantity, note}] at a
        location in one bulk call. Returns the raw response; raises on any
        item error so the caller can retry the whole batch."""
        wid = self.warehouse_id(warehouse)
        items = [{"Sku": l["sku"], "WarehouseId": wid, "LocationCode": location, "Quantity": int(l["quantity"]),
                  "Reason": reason, "Note": l.get("note") or ""} for l in lines]
        path = "inventory/removeItemBulk" if direction == "out" else "inventory/addItemBulk"
        r = self._post(path, {"Items": items})
        errors = r.get("Errors") or []
        if errors or str(r.get("Status", "OK")).upper() not in ("OK", "SUCCESS", ""):
            raise SkuVaultError(f"{path}: {json.dumps(r)[:500]}")
        return r
