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
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://ec2-3-129-203-114.us-east-2.compute.amazonaws.com"


class CloverError(Exception):
    pass


def clover_ref(row_id):
    """Clover caps externalReferenceId at 12 characters and externalPaymentId at
    32, and refuses an externalPaymentId it has seen before. One attempt is one
    pos_card_payments row, so its row number makes a short reference that is new
    for every attempt: POS123."""
    return f"POS{int(row_id)}"


class Client:
    def __init__(self, get_secret):
        self.base = os.environ.get("CLOVER_PROXY_URL", DEFAULT_URL).rstrip("/")
        # Server-to-server: no Origin unless one is configured (the proxy's CORS layer
        # answers 500 to an origin it does not list).
        self.origin = os.environ.get("CLOVER_ORIGIN")
        self.sandbox = os.environ.get("CLOVER_SANDBOX") == "1"
        # The proxy also takes the BigCommerce store hash the other registers send
        # (it selects the merchant alongside the branch). Blank unless configured.
        self.storehash = os.environ.get("CLOVER_STOREHASH", "")
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
        r = self._call("GET", "/api/get-clover-devices", params={"storehash": self.storehash, "store": store})
        return r.get("elements") if isinstance(r, dict) else r

    def roster(self, store):
        """[{merchant_id, store, device_id}] the proxy holds for a branch key."""
        r = self._call("GET", "/clover/token-by-store", params={"store": store})
        return r if isinstance(r, list) else []

    def device_for(self, store):
        """The Flex serial for a branch: the device list first (what the helper
        uses), the token roster as a fallback."""
        try:
            for d in self.devices(store) or []:
                if d.get("serial"):
                    return d["serial"]
        except CloverError:
            pass
        for row in self.roster(store):
            if row.get("device_id"):
                return row["device_id"]
        return None

    def payment_status(self, store, ref, amount_minor):
        """Did this attempt complete on Clover? {"paid": bool, "paymentId", "cloverOrderId"}."""
        r = self._call("GET", "/api/clover/payment-status",
                       params={"storehash": self.storehash, "orderId": ref, "branch": store,
                               "amount": int(amount_minor), "paymentRef": ref})
        return r if isinstance(r, dict) else {"paid": False}

    # -- money --------------------------------------------------------------
    def send_payment(self, store, device, amount_minor, ref, idempotency_key, note=""):
        """Start a charge on the reader and wait for the customer. Returns
        (outcome, payment_id, message, raw) with outcome paid | failed | unknown."""
        path = "/api/clover/send-payment-sandbox" if self.sandbox else "/api/clover/send-payment"
        # The same body the front-desk helper sends: branch picks the Clover token and
        # reader on the proxy; paymentRef is Clover's externalPaymentId and must be new
        # for every attempt, which our per-attempt record id is.
        body = {"device": device, "amount": int(amount_minor), "storehash": self.storehash,
                "orderId": ref, "branch": store, "store": store, "paymentRef": ref}
        if note:
            body["note"] = note
        try:
            r = self._call("POST", path, body, headers={"Idempotency-Key": idempotency_key}, timeout=180)
        except CloverError as exc:
            return "unknown", None, str(exc), None
        return self._outcome(r)

    def refund(self, store, payment_id, amount_minor, ref):
        body = {"storehash": self.storehash, "branch": store, "store": store, "paymentId": payment_id,
                "amount": int(amount_minor), "orderId": ref}
        r = self._call("POST", "/clover/refund", body, headers={"Idempotency-Key": f"refund-{ref}"}, timeout=120)
        outcome, rid, message, raw = self._outcome(r)
        if outcome != "paid":
            raise CloverError(message or "refund failed")
        return rid, raw

    @staticmethod
    def _outcome(r):
        """Read the proxy's envelope the way the front-desk helper does: status 200
        with data.payment.result == SUCCESS is paid; status 500 is "cancelled on the
        terminal" but may hide a completed charge, so it stays unknown until
        payment-status says otherwise; anything else is failed."""
        if not isinstance(r, dict):
            return "unknown", None, "unreadable reply from the Clover proxy", r
        try:
            code = int(r.get("status") or 200)
        except (TypeError, ValueError):
            code = 200
        data = r.get("data")
        if code == 200:
            payment = data.get("payment") if isinstance(data, dict) else None
            if isinstance(payment, dict) and str(payment.get("result", "")).upper() == "SUCCESS":
                return "paid", payment.get("id"), None, r
            return "unknown", None, "unexpected reply from the terminal", r
        message = data.get("message") if isinstance(data, dict) else (str(data) if data else "")
        if code >= 500:
            return "unknown", None, message or "Payment cancelled on the terminal", r
        return "failed", None, message or "The terminal declined this payment", r
