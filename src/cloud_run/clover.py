"""Card payments through the company's Clover proxy (the small service in
front of Clover's Connect REST API that the other registers already use).

The proxy answers HTTP 200 with {"status": <Clover's status>, "data": ...}
for payment calls, so the outcome is read from that inner status. No reply
at all from the reader ("No response from Clover payment service") means the
customer may still have paid: that is an *unknown* outcome, settled later by
looking the order up.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://ec2-3-129-203-114.us-east-2.compute.amazonaws.com"


class CloverError(Exception):
    pass


class Client:
    def __init__(self, get_secret):
        self.base = os.environ.get("CLOVER_PROXY_URL", DEFAULT_URL).rstrip("/")
        # Server-to-server: no Origin unless one is configured (the proxy's CORS layer
        # answers 500 to an origin it does not list).
        self.origin = os.environ.get("CLOVER_ORIGIN")
        self.sandbox = os.environ.get("CLOVER_SANDBOX") == "1"
        self._get_secret = get_secret

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "genc-pos/1.0"}
        if self.origin:
            h["Origin"] = self.origin
        secret = self._get_secret("CLOVER_PROXY_SECRET")
        if secret:
            h[os.environ.get("CLOVER_PROXY_SECRET_HEADER", "x-proxy-secret")] = secret
        return {**h, **(extra or {})}

    def _call(self, method, path, body=None, params=None, headers=None, timeout=30):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise CloverError(f"Clover proxy {path} returned {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise CloverError(f"Clover proxy unreachable: {e.reason}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise CloverError(f"Clover proxy {path} sent a non-JSON reply") from None

    # -- lookups ------------------------------------------------------------
    def devices(self, store):
        r = self._call("GET", "/api/get-clover-devices", params={"storehash": store})
        return r if isinstance(r, list) else r.get("devices") or r.get("data") or r

    def roster(self, store):
        """[{merchant_id, store, device_id}] the proxy holds for a store key."""
        r = self._call("GET", "/clover/token-by-store", params={"store": store})
        return r if isinstance(r, list) else []

    def device_for(self, store):
        for row in self.roster(store):
            if row.get("device_id"):
                return row["device_id"]
        return None

    def charges_by_order(self, store, order_id):
        r = self._call("GET", "/clover/charges-by-order", params={"order_id": order_id, "store": store})
        return r

    # -- money --------------------------------------------------------------
    def send_payment(self, store, device, amount_minor, order_id, note=""):
        """Start a charge on the reader and wait for the customer. Returns
        (outcome, payment_id, message, raw) with outcome paid | failed | unknown."""
        path = "/api/clover/send-payment-sandbox" if self.sandbox else "/api/clover/send-payment"
        body = {"amount": int(amount_minor), "store": store, "orderId": order_id}
        if device:
            body["device"] = device
        if note:
            body["note"] = note
        try:
            r = self._call("POST", path, body, headers={"Idempotency-Key": order_id}, timeout=180)
        except CloverError as exc:
            return "unknown", None, str(exc), None
        return self._outcome(r)

    def refund(self, store, payment_id, amount_minor, order_id):
        body = {"store": store, "paymentId": payment_id, "amount": int(amount_minor), "orderId": order_id}
        r = self._call("POST", "/clover/refund", body, headers={"Idempotency-Key": f"refund-{order_id}"}, timeout=120)
        outcome, rid, message, raw = self._outcome(r)
        if outcome != "paid":
            raise CloverError(message or "refund failed")
        return rid, raw

    @staticmethod
    def _outcome(r):
        status = r.get("status") if isinstance(r, dict) else None
        data = r.get("data") if isinstance(r, dict) else r
        try:
            code = int(status) if status is not None else 200
        except (TypeError, ValueError):
            code = 200
        if code < 300:
            d = data if isinstance(data, dict) else {}
            pid = d.get("id") or d.get("paymentId") or (d.get("payment") or {}).get("id")
            state = str(d.get("status") or d.get("result") or "").upper()
            if state and state not in ("SUCCESS", "APPROVED", "PAID", "SUCCEEDED", "OK"):
                return "failed", pid, d.get("message") or f"Clover said {state}", r
            return "paid", pid, None, r
        message = data.get("message") if isinstance(data, dict) else (str(data) if data else "")
        if code >= 500 or "No response" in (message or ""):
            return "unknown", None, message or f"Clover returned {code}", r
        return "failed", None, message or f"Clover returned {code}", r
