"""
HIREN SAGAR + SMC Lite  -  Order Block engine (Pine v6 matched)
Extracted verbatim from c.py (Logic 1). No credentials, no UI, no pandas.
signals = [(bar_index, 'BUY'/'SELL', entry, sl)]  one per newly created order block
"""
from typing import List, Tuple

import numpy as np

# ---- Pine inputs (same names / defaults as the indicator) -------------------
OB_VAL_MODE    = "Standard"           # Basic / Standard / Strict
OB_CANDLE_MODE = "Last Candle"        # Last Candle / Full Cluster / Most Relevant
OB_BOUND_MODE  = "Full Candle Range"  # Full Candle Range / Candle Body
OB_ATR_LEN     = 14
OB_DISP_ATR    = 0.5
OB_MIN_BODY    = 50
OB_MIN_RUN     = 1
OB_LOOKBACK    = 10
OB_SW_LEN      = 5
OB_MIT_BULL    = "Close Beyond Boundary"
OB_MIT_BEAR    = "Close Beyond Boundary"
OB_BODY_PCT    = 30.0
OB_MIT_PCT     = 100.0
OB_DUP_MODE    = "Keep Stronger"
OB_DUP_THR     = 60.0
OB_MAX_TOTAL   = 10
OB_MAX_SIDE    = 10
OB_RETENTION   = "Remove Oldest"
OB_USE_AGE     = False
OB_MAX_AGE     = 500

G_MINTICK     = 0.05      # NSE tick size
SL_BUFFER_ATR = 0.1       # SL = just beyond OB end line (x ATR)

RR1, RR2, RR3  = 1.0, 2.0, 3.0
FIRST_TARGET_R = 1.0
STEP_R         = 1.0
MAX_STEPS      = 500
MIN_BARS       = 60

SD_SWING_LEN = 10
SD_HISTORY   = 20
SD_BOX_WIDTH = 2.5
SD_ATR_LEN   = 50
SD_CLEAR_R   = 2.0


class SDZone:
    """SMC Lite supply / demand box."""
    def __init__(self, kind, top, bot, poi, swing_idx, start_idx):
        self.kind = kind
        self.top = top
        self.bot = bot
        self.poi = poi
        self.swing_idx = swing_idx
        self.start_idx = start_idx
        self.end_idx = -1


def compute_atr(highs, lows, closes, length):
    prev_close = np.concatenate(([closes[0]], closes[:-1]))
    tr = np.maximum(highs - lows,
                    np.maximum(np.abs(highs - prev_close), np.abs(lows - prev_close)))
    tr[0] = highs[0] - lows[0]
    atr = np.full(len(tr), np.nan)
    if len(tr) < length:
        return atr
    atr[length - 1] = tr[:length].mean()
    for k in range(length, len(tr)):
        atr[k] = (atr[k - 1] * (length - 1) + tr[k]) / length
    return atr


def _pivots(arr, n, high=True):
    """Pivot value of bar i-n, known at bar i (like ta.pivothigh / ta.pivotlow)."""
    out = np.full(len(arr), np.nan)
    for i in range(2 * n, len(arr)):
        p = i - n
        left = arr[p - n:p]
        right = arr[p + 1:p + n + 1]
        if high and arr[p] > left.max() and arr[p] >= right.max():
            out[i] = arr[p]
        if (not high) and arr[p] < left.min() and arr[p] <= right.min():
            out[i] = arr[p]
    return out


def _overlap_pct(t1, b1, t2, b2):
    ov = min(t1, t2) - max(b1, b2)
    sm = min(t1 - b1, t2 - b2)
    return ov / sm * 100.0 if ov > 0 and sm > 0 else 0.0


def compute_hiren_smc_full(opens, highs, lows, closes, volumes=None, timestamps=None):
    """Returns (active_obs, signals).
    signals = [(bar_index, 'BUY'/'SELL', entry, sl)]  - one per newly created order block.
    BUY  = bullish OB created, SL = a little below OB bottom.
    SELL = bearish OB created, SL = a little above OB top."""
    o = np.asarray(opens, float)
    h = np.asarray(highs, float)
    l = np.asarray(lows, float)
    c = np.asarray(closes, float)
    n = len(c)
    if n < MIN_BARS:
        return [], [], []

    atr = compute_atr(h, l, c, OB_ATR_LEN)
    ph = _pivots(h, OB_SW_LEN, True)
    pl = _pivots(l, OB_SW_LEN, False)

    f_body   = OB_VAL_MODE in ("Standard", "Strict")
    f_struct = OB_VAL_MODE == "Strict"
    warm = OB_LOOKBACK + 12 + max(OB_ATR_LEN, 2 * OB_SW_LEN)

    obs: list = []
    used: set = set()
    signals: List[Tuple[int, str, float, float]] = []
    last_sh = last_sl = np.nan
    all_obs: list = []      # every OB ever created, with start / end bar (for confluence)
    cur_i = [0]

    def drop(k):
        z = obs.pop(k)
        z["end"] = cur_i[0]

    def body_ratio(k):
        rng = h[k] - l[k]
        return abs(c[k] - o[k]) / rng if rng > 0 else 0.0

    def is_dir(k, d):
        return c[k] > o[k] if d == 1 else c[k] < o[k]

    def is_opp(k, d):
        return c[k] < o[k] if d == 1 else c[k] > o[k]

    def zone_done(z, i):
        hh = z["top"] - z["bot"]
        mm = OB_MIT_BULL if z["up"] else OB_MIT_BEAR
        below = z["up"]
        if mm == "Wick Touch":
            trig = l[i] <= z["top"] if below else h[i] >= z["bot"]
        elif mm == "Candle Body Entry":
            pen = (z["top"] - min(o[i], c[i])) if below else (max(o[i], c[i]) - z["bot"])
            trig = pen > 0 and pen >= OB_BODY_PCT / 100.0 * hh
        elif mm == "Full Mitigation":
            pen = (z["top"] - l[i]) if below else (h[i] - z["bot"])
            trig = pen > 0 and pen >= OB_MIT_PCT / 100.0 * hh
        elif mm == "Close Beyond Boundary":
            trig = c[i] < z["bot"] if below else c[i] > z["top"]
        else:  # Wick Beyond Boundary
            trig = l[i] < z["bot"] if below else h[i] > z["top"]
        if trig:
            return True
        return OB_USE_AGE and (i - z["conf"]) >= OB_MAX_AGE

    def pick_victim(dirf):
        idx, best = -1, None
        for k in range(len(obs) - 1):
            z = obs[k]
            if dirf == 0 or (dirf == 1 and z["up"]) or (dirf == -1 and not z["up"]):
                if OB_RETENTION == "Remove Oldest":
                    return k
                if best is None or z["strength"] < best:
                    best, idx = z["strength"], k
        return idx

    def count_dir(dirf):
        return sum(1 for z in obs if dirf == 0 or (dirf == 1 and z["up"]) or (dirf == -1 and not z["up"]))

    def enforce_caps():
        for dirf, cap in ((1, OB_MAX_SIDE), (-1, OB_MAX_SIDE), (0, OB_MAX_TOTAL)):
            while count_dir(dirf) > cap:
                idx = pick_victim(dirf)
                if idx < 0:
                    break
                drop(idx)

    def detect(i, d):
        if np.isnan(atr[i]) or i <= warm:
            return None
        run = 0
        for k in range(6):
            idx = i - k
            if idx < 0 or not is_dir(idx, d):
                break
            if f_body and body_ratio(idx) < OB_MIN_BODY / 100.0:
                break
            run += 1
        if run < OB_MIN_RUN:
            return None

        jsel, best = -1, None
        for j in range(run, run + OB_LOOKBACK):
            idx = i - j
            if idx < 0:
                break
            if is_opp(idx, d):
                if OB_CANDLE_MODE == "Most Relevant":
                    v = l[idx] if d == 1 else h[idx]
                    if best is None or (v < best if d == 1 else v > best):
                        best, jsel = v, j
                else:
                    jsel = j
                    break
        if jsel < 0:
            return None

        jfirst = jsel
        if OB_CANDLE_MODE == "Full Cluster":
            for cc in range(1, 10):
                idx = i - (jsel + cc)
                if idx >= 0 and is_opp(idx, d):
                    jfirst = jsel + cc
                else:
                    break

        ztop = zbot = None
        for j in range(jsel, jfirst + 1):
            idx = i - j
            if OB_BOUND_MODE == "Candle Body":
                t, b = max(o[idx], c[idx]), min(o[idx], c[idx])
            else:
                t, b = h[idx], l[idx]
            ztop = t if ztop is None else max(ztop, t)
            zbot = b if zbot is None else min(zbot, b)

        ok = ztop > zbot
        move = (c[i] - ztop) if d == 1 else (zbot - c[i])
        strength = move / atr[i]
        if ok and move <= 0:
            ok = False
        if ok and strength < OB_DISP_ATR:
            ok = False
        if ok and f_struct:
            if d == 1:
                ok = (not np.isnan(last_sh)) and c[i] > last_sh and c[i - run] <= last_sh
            else:
                ok = (not np.isnan(last_sl)) and c[i] < last_sl and c[i - run] >= last_sl
        if ok:  # price must not have closed through the zone since the origin candle
            for j in range(0, jsel):
                if (d == 1 and c[i - j] < zbot) or (d == -1 and c[i - j] > ztop):
                    ok = False
                    break
        key = (i - jsel) * 2 + (1 if d == 1 else 0)
        if ok and key in used:
            ok = False
        if ok and OB_DUP_MODE != "Keep Both":
            for e in obs:
                if e["up"] == (d == 1) and _overlap_pct(ztop, zbot, e["top"], e["bot"]) >= OB_DUP_THR:
                    if OB_DUP_MODE == "Keep Stronger" and e["strength"] >= strength:
                        ok = False
            if ok:
                for k in range(len(obs) - 1, -1, -1):
                    e = obs[k]
                    if e["up"] == (d == 1) and _overlap_pct(ztop, zbot, e["top"], e["bot"]) >= OB_DUP_THR:
                        drop(k)
        if not ok:
            return None

        new_z = {"up": d == 1, "top": float(ztop), "bot": float(zbot),
                 "conf": i, "origin": i - jsel, "strength": float(strength),
                 "start": i, "end": None, "key": key}
        obs.append(new_z)
        all_obs.append(new_z)
        used.add(key)
        enforce_caps()
        return float(ztop), float(zbot)

    for i in range(n):
        cur_i[0] = i
        if not np.isnan(ph[i]):
            last_sh = ph[i]
        if not np.isnan(pl[i]):
            last_sl = pl[i]
        for k in range(len(obs) - 1, -1, -1):
            if zone_done(obs[k], i):
                drop(k)
        r = detect(i, 1)
        if r:
            signals.append((i, "BUY", float(c[i]), float(r[1] - max(SL_BUFFER_ATR * atr[i], 2 * G_MINTICK))))
        r = detect(i, -1)
        if r:
            signals.append((i, "SELL", float(c[i]), float(r[0] + max(SL_BUFFER_ATR * atr[i], 2 * G_MINTICK))))

    return list(obs), signals, all_obs


def compute_hiren_smc_obs(opens, highs, lows, closes, volumes=None, timestamps=None):
    """Logic 1 : (active_obs, signals)"""
    obs, signals, _ = compute_hiren_smc_full(opens, highs, lows, closes, volumes, timestamps)
    return obs, signals


# ==================================================================
# GUARDEER VOLUMETRIC ORDER BLOCK ENGINE (port of the Guardeer Pine v6 module)
#   internal BOS / CHoCH break  ->  OB = lowest low (bull) / highest high (bear)
#   between the broken pivot and the break  ->  "Precise" box  ->  "Absolute" mitigation


def compute_sd_zones(highs, lows, closes):
    n = len(closes)
    L = SD_SWING_LEN
    if n < 2 * L + 2:
        return []
    atr = compute_atr(highs, lows, closes, SD_ATR_LEN)
    all_zones: List[SDZone] = []
    slots = {1: [None] * SD_HISTORY, -1: [None] * SD_HISTORY}

    def create(direction, level, c, i):
        a = atr[i]
        if np.isnan(a) or a <= 0:
            return
        thick = a * (SD_BOX_WIDTH / 10.0)
        if thick <= 0:
            return
        if direction == 1:
            top, bot = float(level), float(level) - thick
        else:
            bot, top = float(level), float(level) + thick
        poi = (top + bot) / 2.0
        for z in slots[direction]:
            if z is not None and abs(poi - z.poi) <= a * 2.0:
                return
        z = SDZone("SUPPLY" if direction == 1 else "DEMAND", top, bot, poi, c, i)
        old = slots[direction].pop()
        if old is not None:
            old.end_idx = i
        slots[direction].insert(0, z)
        all_zones.append(z)

    for i in range(2 * L, n):
        c = i - L
        if highs[c] > highs[c - L:c].max() and highs[c] >= highs[c + 1:i + 1].max():
            create(1, highs[c], c, i)
        if lows[c] < lows[c - L:c].min() and lows[c] <= lows[c + 1:i + 1].min():
            create(-1, lows[c], c, i)
        for k, z in enumerate(slots[1]):
            if z is not None and closes[i] >= z.top:
                z.end_idx = i
                slots[1][k] = None
        for k, z in enumerate(slots[-1]):
            if z is not None and closes[i] <= z.bot:
                z.end_idx = i
                slots[-1][k] = None
    return all_zones


def sd_context(zones, j, side, entry, sl):
    risk = abs(entry - sl)
    buy = side == "BUY"
    support = False
    obst = None
    if risk <= 0:
        return {"zone": False, "obst_r": None, "blocked": False}
    for z in zones:
        if z.start_idx > j or (z.end_idx != -1 and z.end_idx <= j):
            continue
        if buy:
            if z.kind == "DEMAND" and z.top >= sl and z.bot <= entry:
                support = True
            elif z.kind == "SUPPLY" and z.top > entry:
                d = max(0.0, z.bot - entry) / risk
                obst = d if obst is None else min(obst, d)
        else:
            if z.kind == "SUPPLY" and z.bot <= sl and z.top >= entry:
                support = True
            elif z.kind == "DEMAND" and z.bot < entry:
                d = max(0.0, entry - z.top) / risk
                obst = d if obst is None else min(obst, d)
    return {"zone": support, "obst_r": obst, "blocked": bool(obst is not None and obst < SD_CLEAR_R)}


def simulate_trade(buy: bool, entry: float, sl: float, sig_i: int,
                   end_idx: int, highs, lows, closes):
    """Endless trailing ladder: when a target hits, SL moves to it and the next target is one step further."""
    risk = abs(entry - sl)
    if risk <= 0:
        return float(closes[end_idx]), end_idx, "INVALID", 0
    sgn = 1 if buy else -1
    cur_sl = sl
    reached = 0
    next_tgt = entry + sgn * risk * FIRST_TARGET_R

    for k in range(sig_i + 1, end_idx + 1):
        if (buy and lows[k] <= cur_sl) or (not buy and highs[k] >= cur_sl):
            reason = "SL" if reached == 0 else f"TRAIL after T{reached}"
            return cur_sl, k, reason, reached

        while reached < MAX_STEPS and ((highs[k] >= next_tgt) if buy else (lows[k] <= next_tgt)):
            reached += 1
            cur_sl = next_tgt
            next_tgt = entry + sgn * risk * (FIRST_TARGET_R + reached * STEP_R)

    return float(closes[end_idx]), end_idx, "OPEN (SL not hit)", reached


# ==================================================================
# UNIVERSE HELPERS
