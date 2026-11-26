"""
Angel One helpers: credentials (server-side only), REST login, candles, token map.
Credentials are read from environment variables / a .env file and are NEVER
sent to the browser or written into any HTML.
"""
import csv
import os
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pyotp
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
API_ROOT = "https://apiconnect.angelone.in"
KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")

SCAN_DELAY = 0.35


# ------------------------------------------------------------------ credentials
def data_dir():
    """Writable folder (app-private on Android)."""
    d = os.environ.get("HS_DATA_DIR") or os.environ.get("ANDROID_PRIVATE") or BASE
    os.makedirs(d, exist_ok=True)
    return d


def env_paths():
    return [os.path.join(data_dir(), ".env"), os.path.join(BASE, ".env"),
            os.path.join(os.path.expanduser("~"), ".hiren_sagar.env")]


def _parse_env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def get_credentials():
    cred = {k: os.environ.get(k, "") for k in KEYS}
    for p in env_paths():
        if os.path.exists(p):
            for k, v in _parse_env_file(p).items():
                if k in KEYS and not cred.get(k):
                    cred[k] = v
    return cred


def credentials_configured():
    c = get_credentials()
    return all(c.get(k) for k in KEYS)


def save_credentials(api_key, client_code, pin, totp_secret):
    path = os.path.join(data_dir(), ".env")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"ANGEL_API_KEY={api_key.strip()}\nANGEL_CLIENT_CODE={client_code.strip()}\n"
                f"ANGEL_PIN={pin.strip()}\nANGEL_TOTP_SECRET={totp_secret.strip().replace(' ', '')}\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ REST client
class AngelClient:
    def __init__(self):
        self.jwt = None
        self.api_key = None
        self.lock = threading.Lock()
        self.last_error = ""

    def _headers(self, auth=True):
        h = {
            "Content-Type": "application/json", "Accept": "application/json",
            "X-UserType": "USER", "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": self.api_key or "",
        }
        if auth and self.jwt:
            h["Authorization"] = "Bearer " + self.jwt
        return h

    @property
    def logged_in(self):
        return bool(self.jwt)

    def login(self, retries=3):
        c = get_credentials()
        if not all(c.get(k) for k in KEYS):
            self.last_error = "Credentials not configured"
            return False
        self.api_key = c["ANGEL_API_KEY"]
        for attempt in range(retries):
            try:
                totp = pyotp.TOTP(c["ANGEL_TOTP_SECRET"].replace(" ", "")).now()
                r = requests.post(
                    API_ROOT + "/rest/auth/angelbroking/user/v1/loginByPassword",
                    json={"clientcode": c["ANGEL_CLIENT_CODE"], "password": c["ANGEL_PIN"], "totp": totp},
                    headers=self._headers(auth=False), timeout=20)
                data = r.json()
                if data.get("status") and data.get("data", {}).get("jwtToken"):
                    self.jwt = data["data"]["jwtToken"]
                    self.last_error = ""
                    return True
                self.last_error = str(data.get("message") or "login failed")
            except Exception as e:  # network / json
                self.last_error = str(e)
            time.sleep(2)
        self.jwt = None
        return False

    def _candle_request(self, params):
        r = requests.post(API_ROOT + "/rest/secure/angelbroking/historical/v1/getCandleData",
                          json=params, headers=self._headers(), timeout=25)
        return r.json()

    def candles(self, token, interval, start_dt, end_dt, exchange="NSE"):
        """Raw candle rows [[ts,o,h,l,c,v],...] ; [] when none. Retries + re-login."""
        params = {"exchange": exchange, "symboltoken": str(token), "interval": interval,
                  "fromdate": start_dt.strftime("%Y-%m-%d %H:%M"),
                  "todate": end_dt.strftime("%Y-%m-%d %H:%M")}
        for attempt in range(3):
            try:
                res = self._candle_request(params)
                if res.get("status"):
                    return res.get("data") or []
                msg = str(res.get("message", "")).lower()
                if "token" in msg or "invalid" in msg or "unauthor" in msg:
                    with self.lock:
                        self.login(retries=1)
            except Exception:
                pass
            time.sleep(1.0 + attempt)
        return []


CLIENT = AngelClient()


# ------------------------------------------------------------------ candles -> arrays
def _parse_ts(s):
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return datetime.fromisoformat(s)


class Candles:
    """Minimal replacement for the pandas DataFrame used in c.py."""
    def __init__(self, rows):
        seen, clean = set(), []
        for r in rows:
            if r[0] in seen:
                continue
            seen.add(r[0])
            clean.append(r)
        clean.sort(key=lambda r: r[0])
        self.ts = [_parse_ts(r[0]) for r in clean]
        arr = np.array([[float(x) for x in r[1:6]] for r in clean], float) if clean else np.zeros((0, 5))
        self.open, self.high, self.low, self.close, self.vol = (arr[:, i] for i in range(5))

    def __len__(self):
        return len(self.ts)

    def drop_last(self):
        for a in ("ts", "open", "high", "low", "close", "vol"):
            setattr(self, a, getattr(self, a)[:-1])


def fetch_candle_data(token, interval, days_back, end=None):
    end = min(end or datetime.now(), datetime.now())
    rows = CLIENT.candles(token, interval, end - timedelta(days=days_back), end)
    return Candles(rows)


def fetch_range(token, tf, from_date, to_date, warm_days, max_days):
    start_dt = datetime.combine(from_date, datetime.min.time()) - timedelta(days=warm_days)
    end_dt = min(datetime.now(), datetime.combine(to_date, datetime.min.time()) + timedelta(hours=23, minutes=59))
    step = timedelta(days=max_days - 1)
    rows, cur = [], start_dt
    while cur < end_dt:
        nxt = min(cur + step, end_dt)
        rows += CLIENT.candles(token, tf, cur, nxt)
        cur = nxt + timedelta(minutes=1)
        time.sleep(SCAN_DELAY)
    return Candles(rows)


# ------------------------------------------------------------------ tokens / universe
_TOKEN_MAP = {}


def load_tokens():
    """Same mapping rules as c.py: NSE only, 'name' first then 'symbol' (-EQ stripped) overrides."""
    global _TOKEN_MAP
    if _TOKEN_MAP:
        return _TOKEN_MAP
    path = next((p for p in (os.path.join(BASE, "data", "nse_tokens.csv"),
                             os.path.join(BASE, "angel_one_symbol_tokens.csv")) if os.path.exists(p)), None)
    if not path:
        return {}
    by_name, by_sym = {}, {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if "exch_seg" in r and str(r["exch_seg"]).upper() != "NSE":
                continue
            tok = str(r["token"]).split(".")[0]
            by_name[str(r.get("name", "")).strip().upper()] = tok
            by_sym[str(r["symbol"]).replace("-EQ", "").strip().upper()] = tok
    by_name.update(by_sym)
    _TOKEN_MAP = by_name
    return _TOKEN_MAP


UNIVERSE_FILES = {
    "nifty500": ("nifty500.csv",),
    "fno": ("Custom CSV Universe.csv", "Custom_CSV_Universe.csv"),
}


def load_stock_list(universe):
    for name in UNIVERSE_FILES.get(universe, ()):
        for folder in (os.path.join(BASE, "data"), BASE):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                with open(p, encoding="utf-8-sig") as f:
                    rd = csv.DictReader(f)
                    cols = {(c or "").strip().lower(): c for c in (rd.fieldnames or [])}
                    col = next((cols[c] for c in ("symbol", "tradingsymbol", "company name", "stock", "stock name")
                                if c in cols), None)
                    if not col:
                        return []
                    seen, out = set(), []
                    for r in rd:
                        s = str(r.get(col) or "").strip().upper()
                        if s and s not in seen:
                            seen.add(s)
                            out.append(s)
                    return out
    return []
