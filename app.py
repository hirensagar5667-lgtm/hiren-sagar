"""HIREN SAGAR - OB Terminal (Flask backend, Pine-matched Logic 1).
Credentials live only on the server side (.env / environment); the browser never sees them."""
import os
import threading
import time
import uuid
from datetime import datetime

import numpy as np
from flask import Flask, jsonify, render_template, request

import angel
import engine as E

app = Flask(__name__)

TF_CFG = {  # api interval: (minutes, max_days_per_request, warm_days, scan_days_back, audit_days_back)
    "ONE_MINUTE":     (1,    30,   5,   25, 120),
    "THREE_MINUTE":   (3,    60,  10,   45, 120),
    "FIVE_MINUTE":    (5,   100,  15,   70, 120),
    "FIFTEEN_MINUTE": (15,  200,  40,  100, 120),
    "ONE_HOUR":       (60,  400, 120,  300, 120),
    "ONE_DAY":        (1440, 2000, 700, 800, 500),
}


# ------------------------------------------------------------------ job manager
class Job:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.state = "running"
        self.total = 0
        self.done = 0
        self.current = ""
        self.results = []
        self.summary = None
        self.error = ""
        self.cancel = False
        self.started = time.time()

    def public(self, since=0):
        return {"id": self.id, "kind": self.kind, "state": self.state, "total": self.total,
                "done": self.done, "current": self.current, "error": self.error,
                "summary": self.summary, "count": len(self.results),
                "results": self.results[since:], "elapsed": round(time.time() - self.started)}


JOBS = {}
JOB_LOCK = threading.Lock()


def start_job(kind, fn, cfg):
    with JOB_LOCK:
        if any(j.state == "running" for j in JOBS.values()):
            return None
        job = Job(kind)
        JOBS[job.id] = job
        for old in list(JOBS)[:-5]:
            JOBS.pop(old, None)

    def runner():
        try:
            if not angel.CLIENT.logged_in and not angel.CLIENT.login():
                raise RuntimeError("Angel One login failed: " + (angel.CLIENT.last_error or "unknown"))
            fn(job, cfg)
            job.state = "cancelled" if job.cancel else "done"
        except Exception as e:
            job.error = str(e)
            job.state = "error"
    threading.Thread(target=runner, daemon=True).start()
    return job


# ------------------------------------------------------------------ helpers
def is_forming(c, tf):
    ts = c.ts[-1]
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    return (now - ts).total_seconds() < TF_CFG[tf][0] * 60


def mk_sig(side, entry, sl, when, status, ctx):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    m = 1 if side == "BUY" else -1
    return {"side": side, "entry": round(entry, 2), "sl": round(sl, 2),
            "t1": round(entry + m * risk * E.RR1, 2), "t2": round(entry + m * risk * E.RR2, 2),
            "t3": round(entry + m * risk * E.RR3, 2), "risk_pct": round(risk / entry * 100, 2),
            "time": when, "status": status,
            "sd_zone": bool(ctx["zone"]), "sd_blocked": bool(ctx["blocked"]),
            "sd_obst_r": None if ctx["obst_r"] is None else round(ctx["obst_r"], 2)}


def signals_for(c):
    _, sigs = E.compute_hiren_smc_obs(c.open, c.high, c.low, c.close, c.vol, c.ts)
    return sigs


def parse_date(s):
    return datetime.strptime(s.strip(), "%d-%m-%Y").date()


# ------------------------------------------------------------------ modes
def run_scan(job, cfg):
    tf, uni = cfg["tf"], cfg["universe"]
    use_live, sd_filter = cfg["live"], cfg["sd_filter"]
    symbols, tokens = angel.load_stock_list(uni), angel.load_tokens()
    job.total = len(symbols)
    days_back = TF_CFG[tf][3]
    for sym in symbols:
        if job.cancel:
            return
        job.done += 1
        job.current = sym
        token = tokens.get(sym)
        if not token:
            continue
        c = angel.fetch_candle_data(token, tf, days_back)
        if len(c) == 0:
            continue
        forming = is_forming(c, tf)
        if not use_live and forming:
            c.drop_last()
            forming = False
        if len(c) < E.MIN_BARS:
            continue
        n = len(c)
        zones = E.compute_sd_zones(c.high, c.low, c.close)
        lookback = 3 if use_live else 2
        for sig_i, side, entry, sl in signals_for(c):
            if sig_i < n - lookback:
                continue
            ctx = E.sd_context(zones, sig_i, side, entry, sl)
            if sd_filter and ctx["blocked"]:
                continue
            status = "LIVE" if (sig_i == n - 1 and forming) else "CLOSED"
            s = mk_sig(side, entry, sl, c.ts[sig_i].strftime("%d-%b %H:%M"), status, ctx)
            if s:
                s["symbol"] = sym
                job.results.append(s)
        time.sleep(angel.SCAN_DELAY)
    job.summary = {"signals": len(job.results), "symbols": len(symbols)}


def run_audit(job, cfg):
    tf, uni, target = cfg["tf"], cfg["universe"], cfg["date"]
    symbols, tokens = angel.load_stock_list(uni), angel.load_tokens()
    job.total = len(symbols)
    days_back = TF_CFG[tf][4]
    end = datetime.now() if tf == "ONE_DAY" else datetime(target.year, target.month, target.day, 23, 59)
    for sym in symbols:
        if job.cancel:
            return
        job.done += 1
        job.current = sym
        token = tokens.get(sym)
        if not token:
            continue
        c = angel.fetch_candle_data(token, tf, days_back, end=end)
        if len(c) < E.MIN_BARS:
            continue
        day_idx = {i for i, t in enumerate(c.ts) if t.date() == target}
        last_i = len(c) - 1
        day_close = float(c.close[last_i])
        highs, lows = c.high, c.low
        for sig_i, side, entry, sl in signals_for(c):
            if sig_i not in day_idx:
                continue
            buy = side == "BUY"
            risk = abs(entry - sl)
            if risk <= 0:
                continue
            m = 1 if buy else -1
            t1, t2, t3 = (entry + risk * r * m for r in (E.RR1, E.RR2, E.RR3))
            cur_sl, touched = sl, {}
            for k in range(sig_i + 1, last_i + 1):
                if "SL" not in touched and ((buy and lows[k] <= cur_sl) or (not buy and highs[k] >= cur_sl)):
                    touched["SL"] = k
                    break
                if "T1" not in touched and ((highs[k] >= t1) if buy else (lows[k] <= t1)):
                    touched["T1"] = k
                    cur_sl = entry
                if "T2" not in touched and ((highs[k] >= t2) if buy else (lows[k] <= t2)):
                    touched["T2"] = k
                    cur_sl = t1
                if "T3" not in touched and ((highs[k] >= t3) if buy else (lows[k] <= t3)):
                    touched["T3"] = k
                    cur_sl = t2
            hit = ("T3 (1:3)" if "T3" in touched else "T2 (1:2)" if "T2" in touched
                   else "T1 (1:1)" if "T1" in touched else "SL" if "SL" in touched else "-")
            job.results.append({
                "symbol": sym, "side": side,
                "time": c.ts[sig_i].strftime("%H:%M" if tf != "ONE_DAY" else "%d-%b"),
                "entry": round(entry, 2), "sl": round(sl, 2), "day_close": round(day_close, 2),
                "pnl_pct": round((day_close / entry - 1) * 100 * m, 2), "first_hit": hit})
        time.sleep(angel.SCAN_DELAY)
    n = len(job.results)
    wins = sum(1 for r in job.results if r["pnl_pct"] > 0)
    job.summary = {"signals": n, "profitable": wins,
                   "win_pct": round(wins / n * 100) if n else 0,
                   "avg_pnl": round(sum(r["pnl_pct"] for r in job.results) / n, 2) if n else 0}


def backtest_symbol(c, cfg):
    n = len(c)
    if n < E.MIN_BARS + 2:
        return []
    tf = cfg["tf"]
    dates = [t.date() for t in c.ts]
    in_range = [cfg["from_date"] <= d <= cfg["to_date"] for d in dates]
    last_of_day = np.zeros(n, dtype=int)
    j = n - 1
    for k in range(n - 1, -1, -1):
        if k == n - 1 or dates[k] != dates[k + 1]:
            j = k
        last_of_day[k] = j
    last_forming = is_forming(c, tf)
    trades = []
    for sig_i, side, entry, sl in signals_for(c):
        if not in_range[sig_i] or (sig_i == n - 1 and last_forming):
            continue
        buy = side == "BUY"
        risk = abs(entry - sl)
        if risk <= 0:
            continue
        end_idx = int(last_of_day[sig_i]) if cfg["day_only"] else n - 1
        exit_price, exit_idx, reason, reached = E.simulate_trade(
            buy, entry, sl, sig_i, end_idx, c.high, c.low, c.close)
        r = (exit_price - entry) / risk * (1 if buy else -1)
        trades.append({
            "date": dates[sig_i].strftime("%d-%b-%Y"),
            "time": c.ts[sig_i].strftime("%d-%b" if tf == "ONE_DAY" else "%H:%M"),
            "side": side, "entry": round(entry, 2), "sl": round(sl, 2),
            "exit": round(float(exit_price), 2), "result": reason,
            "targets_hit": reached, "r": round(float(r), 2),
            "_sort": c.ts[sig_i].timestamp()})
    return trades


def run_backtest(job, cfg):
    tf, uni = cfg["tf"], cfg["universe"]
    symbols, tokens = angel.load_stock_list(uni), angel.load_tokens()
    job.total = len(symbols)
    _, max_days, warm_days, _, _ = TF_CFG[tf]
    for sym in symbols:
        if job.cancel:
            return
        job.done += 1
        job.current = sym
        token = tokens.get(sym)
        if not token:
            continue
        c = angel.fetch_range(token, tf, cfg["from_date"], cfg["to_date"], warm_days, max_days)
        if len(c) == 0:
            continue
        for t in backtest_symbol(c, cfg):
            t["symbol"] = sym
            job.results.append(t)
    job.results.sort(key=lambda t: t["_sort"])
    for t in job.results:
        t.pop("_sort", None)
    r = np.array([t["r"] for t in job.results], float)
    if len(r):
        gw, gl = float(r[r > 1e-9].sum()), float(-r[r < -1e-9].sum())
        job.summary = {"trades": len(r), "wins": int((r > 1e-9).sum()), "losses": int((r < -1e-9).sum()),
                       "win_rate": round(float((r > 1e-9).sum()) / len(r) * 100, 1),
                       "total_r": round(float(r.sum()), 2), "avg_r": round(float(r.mean()), 2),
                       "pf": None if gl == 0 else round(gw / gl, 2),
                       "best": round(float(r.max()), 2), "worst": round(float(r.min()), 2)}
    else:
        job.summary = {"trades": 0}


# ------------------------------------------------------------------ routes
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    return jsonify({"configured": angel.credentials_configured(),   # booleans only, never values
                    "logged_in": angel.CLIENT.logged_in,
                    "login_error": angel.CLIENT.last_error,
                    "tokens": len(angel.load_tokens()),
                    "universes": {k: len(angel.load_stock_list(k)) for k in angel.UNIVERSE_FILES},
                    "logic": "HIREN SAGAR + SMC Lite (Pine-matched)"})


@app.route("/api/connect", methods=["POST"])
def connect():
    ok = angel.CLIENT.login()
    return jsonify({"logged_in": ok, "message": "Login OK" if ok else (angel.CLIENT.last_error or "Login failed")})


@app.route("/api/setup", methods=["POST"])
def setup():
    """One-time credential save for the phone app. Loopback only; values are never echoed back."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"ok": False, "message": "Setup is only allowed from the device itself."}), 403
    d = request.get_json(silent=True) or {}
    vals = [str(d.get(k, "")).strip() for k in ("api_key", "client_code", "pin", "totp_secret")]
    if not all(vals):
        return jsonify({"ok": False, "message": "Saari 4 values chahiye."}), 400
    angel.save_credentials(*vals)
    angel.CLIENT.jwt = None
    return jsonify({"ok": True, "message": "Saved on device. Ab scan chalao."})


def _uni_tf(d):
    uni = d.get("universe", "fno")
    tf = d.get("tf", "FIFTEEN_MINUTE")
    if uni not in angel.UNIVERSE_FILES or tf not in TF_CFG:
        raise ValueError("Invalid universe / timeframe")
    if not angel.load_stock_list(uni):
        raise ValueError(f"Universe '{uni}' ki CSV nahi mili ya khaali hai.")
    return uni, tf


def _launch(kind, fn, cfg):
    job = start_job(kind, fn, cfg)
    if not job:
        return jsonify({"ok": False, "message": "Ek job pehle se chal raha hai."}), 409
    return jsonify({"ok": True, "job": job.id})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    d = request.get_json(silent=True) or {}
    try:
        uni, tf = _uni_tf(d)
    except ValueError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    return _launch("scan", run_scan, {"universe": uni, "tf": tf, "live": bool(d.get("live")),
                                      "sd_filter": bool(d.get("sd_filter"))})


@app.route("/api/audit", methods=["POST"])
def api_audit():
    d = request.get_json(silent=True) or {}
    try:
        uni, tf = _uni_tf(d)
        target = parse_date(d.get("date", ""))
    except ValueError as e:
        return jsonify({"ok": False, "message": "Date DD-MM-YYYY me do (ya " + str(e) + ")"}), 400
    return _launch("audit", run_audit, {"universe": uni, "tf": tf, "date": target})


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    d = request.get_json(silent=True) or {}
    try:
        uni, tf = _uni_tf(d)
        f = parse_date(d.get("from_date", ""))
        t = parse_date(d.get("to_date", "") or d.get("from_date", ""))
    except ValueError as e:
        return jsonify({"ok": False, "message": "Date DD-MM-YYYY me do (ya " + str(e) + ")"}), 400
    today = datetime.now().date()
    if f > t or f > today:
        return jsonify({"ok": False, "message": "Date range galat hai."}), 400
    return _launch("backtest", run_backtest,
                   {"universe": uni, "tf": tf, "from_date": f, "to_date": min(t, today),
                    "day_only": bool(d.get("day_only")) and tf != "ONE_DAY"})


@app.route("/api/job/<jid>")
def api_job(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"state": "missing"}), 404
    return jsonify(job.public(since=int(request.args.get("since", 0))))


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    for j in JOBS.values():
        if j.state == "running":
            j.cancel = True
    return jsonify({"ok": True})


def run(host=None, port=5000):
    app.run(host=host or os.environ.get("HS_HOST", "127.0.0.1"), port=port, debug=False, threaded=True)


if __name__ == "__main__":
    run()
