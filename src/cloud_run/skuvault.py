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

    def _dump(self, max_age, retries=0):
        """The whole account's stock, {WAREHOUSE: {sku: {LOCATION: qty}}}, cached.
        A bin inside a warehouse reads as PARENT[BIN], e.g. PKP[1L1]."""
        with self._lock:
            hit = self._stock.get("dump")
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
        dump = {}
        page = 0
        while True:
            try:
                # With nothing cached yet, wait out one throttle rather than show no stock at all.
                r = self._post("inventory/getInventoryByLocation", {"PageSize": 10000, "PageNumber": page},
                               retries=retries if hit else max(retries, 1))
            except SkuVaultError:
                # Throttled or down: a stale answer beats none for a register showing badges.
                if hit:
                    return hit[1]
                raise
            items = r.get("Items") or {}
            for sku, rows in items.items():
                for row in rows:
                    wh = str(row.get("WarehouseCode", "")).upper()
                    loc = str(row.get("LocationCode", "")).upper()
                    by_loc = dump.setdefault(wh, {}).setdefault(sku, {})
                    by_loc[loc] = by_loc.get(loc, 0) + int(row.get("Quantity") or 0)
            if len(items) < 10000:
                break
            page += 1
        with self._lock:
            self._stock["dump"] = (time.time(), dump)
        return dump

    @staticmethod
    def bin_code(location):
        """What a write call names a location by: PKP[1L1] -> 1L1, PKP -> PKP."""
        m = re.search(r"\[([^\]]+)\]$", location or "")
        return m.group(1) if m else location

    def stock(self, warehouse, location=None, max_age=STOCK_TTL):
        """{sku: on hand} across a warehouse, or at one location (or bin) of it."""
        wh = self._dump(max_age).get(warehouse.upper(), {})
        want = location.upper() if location else None
        out = {}
        for sku, by_loc in wh.items():
            n = sum(q for loc, q in by_loc.items() if want is None or loc == want or self.bin_code(loc) == want)
            if n:
                out[sku] = n
        return out

    def locations(self, warehouse, max_age=600):
        """Codes a write call can name in a warehouse: its top-level location and every bin."""
        key = ("locs", warehouse.upper())
        with self._lock:
            hit = self._stock.get(key)
            if hit and time.time() - hit[0] < max_age:
                return hit[1]
        codes, page = set(), 0
        while True:
            r = self._post("inventory/getLocations", {"PageSize": 1000, "PageNumber": page}, retries=2)
            items = r.get("Items") or []
            for l in items:
                if str(l.get("WarehouseCode", "")).upper() == warehouse.upper():
                    code = l.get("ContainerCode") or l.get("LocationCode")
                    if code:
                        codes.add(str(code).upper())
            if len(items) < 1000:
                break
            page += 1
        with self._lock:
            self._stock[key] = (time.time(), codes)
        return codes

    def bins(self, warehouse, sku, max_age=0):
        """[(bin to write to, qty)] holding a SKU in a warehouse, fullest first. Fresh by default."""
        by_loc = self._dump(max_age, retries=2).get(warehouse.upper(), {}).get(sku, {})
        return sorted(((self.bin_code(loc), q) for loc, q in by_loc.items() if q > 0), key=lambda x: -x[1])

    def adjust(self, warehouse, location, sku, delta):
        """Shift the cached quantity after a move so the register reflects it at once."""
        with self._lock:
            hit = self._stock.get("dump")
            if not hit:
                return
            by_loc = hit[1].setdefault(warehouse.upper(), {}).setdefault(sku, {})
            if location:
                key = next((k for k in by_loc if k == location.upper() or self.bin_code(k) == location.upper()), location.upper())
                by_loc[key] = max(0, by_loc.get(key, 0) + delta)
                return
            if delta > 0:
                key = next(iter(by_loc), warehouse.upper())
                by_loc[key] = by_loc.get(key, 0) + delta
                return
            need = -delta
            for k in sorted(by_loc, key=lambda k: -by_loc[k]):
                take = min(need, by_loc[k]); by_loc[k] -= take; need -= take
                if not need:
                    break

    # -- moves --------------------------------------------------------------
    def move_items(self, direction, warehouse, items, reason):
        """Remove ('out') or add ('in') [{sku, quantity, location, note}] in one bulk
        call, each item at its own location or bin. Raises on any item error."""
        wid = self.warehouse_id(warehouse)
        body = [{"Sku": i["sku"], "WarehouseId": wid, "LocationCode": i["location"], "Quantity": int(i["quantity"]),
                 "Reason": reason, "Note": i.get("note") or ""} for i in items]
        path = "inventory/removeItemBulk" if direction == "out" else "inventory/addItemBulk"
        r = self._post(path, {"Items": body}, retries=2)
        errors = r.get("Errors") or []
        if errors or str(r.get("Status", "OK")).upper() not in ("OK", "SUCCESS", ""):
            raise SkuVaultError(f"{path}: {json.dumps(r)[:500]}")
        return r

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
