#!/usr/bin/env python3
"""CRT (Candle Range Theory) backtester + scanner with a 15m entry. Alert-only, does NOT trade.
Needs forex_tools.py in the same folder.
Run: python crt_tools.py --backtest | --scan | --demo

PATTERN: candle 2 sweeps candle 1's high or low, then closes back INSIDE candle 1's range.
Stop = beyond the sweep wick (+0.1 ATR). Target = opposite end of candle 1.
Trades with a tiny stop or reward/risk under 1 are skipped.

ENTRY (what this version adds): after candle 2 closes, during the NEXT candle, wait for a
15m market structure shift (MSS): a 15m candle CLOSES beyond the latest minor swing point
(2-bar pivot) formed after the sweep. Cancelled if price hits the stop or the target first.

RULES  crt / crt_trend          pattern only, enter at candle close (2 years of data)
       crt_mss / crt_mss_trend  pattern + 15m MSS entry (only ~58 days of data exist)
       *_trend also needs the trade to agree with the EMA50 side.

HONEST LIMIT: Yahoo gives only ~60 days of 15m data, so the 15m backtest is a small sample
and is labelled INSUFFICIENT under 30 trades. The scanner therefore records every alert and
reports whether it hit target or stop ("forward record"). That record is the real test.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import forex_tools as ft

RESULTS_FILE, STATE_FILE = "crt_results.json", "crt_state.json"
BASE_RULES = ("crt", "crt_trend")
MSS_RULES = ("crt_mss", "crt_mss_trend")
MAX_BARS, BUFFER_ATR, MIN_RISK_ATR, MIN_RR = 12, 0.1, 0.25, 1.0
MSS_MIN_TRADES = 30
MAX_MSS_AGE_MIN = 90  # skip alerts whose structure shift is older than this
STEP = pd.Timedelta(minutes=15)


# ---------------------------------------------------------------- pattern (higher timeframe)
def crt_levels(df, a, trend=False):
    """Per candle: signal (+1 long / -1 short / 0), stop price, target price."""
    h, l, c = (df[k].values for k in ("high", "low", "close"))
    e50 = ft.ema(df["close"], 50).values
    n = len(df)
    sig, stop, tgt = np.zeros(n, int), np.full(n, np.nan), np.full(n, np.nan)
    for i in range(1, n):
        if np.isnan(a[i]):
            continue
        h1, l1, buf = h[i - 1], l[i - 1], BUFFER_ATR * a[i]
        if not (l1 < c[i] < h1):  # must close back inside candle 1's range
            continue
        if l[i] < l1 and h[i] <= h1 and (not trend or c[i] > e50[i]):  # swept low -> long
            sig[i], stop[i], tgt[i] = 1, l[i] - buf, h1
        elif h[i] > h1 and l[i] >= l1 and (not trend or c[i] < e50[i]):  # swept high -> short
            sig[i], stop[i], tgt[i] = -1, h[i] + buf, l1
    return sig, stop, tgt


def valid(s, entry, stop, tgt, atr_now):
    risk = abs(entry - stop)
    ok = (stop < entry < tgt) if s == 1 else (tgt < entry < stop)
    return bool(ok and risk >= MIN_RISK_ATR * atr_now and abs(tgt - entry) >= MIN_RR * risk), risk


def backtest_crt(df, sig, stop_arr, tgt_arr, a, spread):
    """Pattern only: enter at the open after candle 2 closes."""
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    n, trades, i = len(df), [], 0
    while i < n - 1:
        s = int(sig[i])
        if s == 0:
            i += 1
            continue
        ei = i + 1
        entry, stop, tgt = o[ei], stop_arr[i], tgt_arr[i]
        ok, risk = valid(s, entry, stop, tgt, a[i])
        if not ok:
            i += 1
            continue
        end, j, ex = min(n - 1, ei + MAX_BARS), ei, None
        while j <= end:
            hit_stop = l[j] <= stop if s == 1 else h[j] >= stop
            hit_tgt = h[j] >= tgt if s == 1 else l[j] <= tgt
            if hit_stop:  # stop first if both hit in one candle (pessimistic)
                ex = stop
                break
            if hit_tgt:
                ex = tgt
                break
            j += 1
        if ex is None:
            j, ex = end, c[end]
        trades.append({"entry_time": df.index[ei], "dir": s, "R": ((ex - entry) * s - spread) / risk})
        i = j + 1
    return pd.DataFrame(trades, columns=["entry_time", "dir", "R"])


# ---------------------------------------------------------------- 15m structure shift
def find_mss(m, s, sweep_start, win_start, win_end, stop, tgt, hrs):
    """m = 15m candles. Return the position of the first 15m bar inside [win_start, win_end)
    whose close breaks the latest confirmed minor swing formed after the sweep, else None.
    Long: close above the latest 2-bar pivot high. Short: close below the latest pivot low.
    A pivot only counts once 2 bars after it have closed. Returns None if the stop or target
    is touched first, or if candle 2 is not covered by enough 15m data."""
    lo = m.index.searchsorted(sweep_start)
    ws = m.index.searchsorted(win_start)
    we = min(m.index.searchsorted(win_end), len(m))
    if ws >= len(m) or ws - lo < max(2, int(2 * hrs)):
        return None
    hi, lw, cl = m["high"].values, m["low"].values, m["close"].values
    sp = lo + int(np.argmin(lw[lo:ws]) if s == 1 else np.argmax(hi[lo:ws]))  # the sweep extreme
    for j in range(ws, we):
        if s == 1 and (lw[j] <= stop or hi[j] >= tgt):
            return None
        if s == -1 and (hi[j] >= stop or lw[j] <= tgt):
            return None
        level = None
        for p in range(j - 3, sp, -1):
            if p < 2:
                break
            if s == 1 and hi[p] > hi[p - 2:p].max() and hi[p] > hi[p + 1:p + 3].max():
                level = hi[p]
                break
            if s == -1 and lw[p] < lw[p - 2:p].min() and lw[p] < lw[p + 1:p + 3].min():
                level = lw[p]
                break
        if level is not None and ((cl[j] > level) if s == 1 else (cl[j] < level)):
            return j
    return None


def backtest_mss(htf, m, sig, stop_arr, tgt_arr, a, spread, hrs):
    """Pattern + 15m MSS entry. Enter at the open of the 15m bar after the shift."""
    o, h, l, c = (m[k].values for k in ("open", "high", "low", "close"))
    delta = pd.Timedelta(hours=hrs)
    hold = int(MAX_BARS * hrs * 4)
    trades, busy = [], None
    for i in range(1, len(htf)):
        s = int(sig[i])
        if s == 0:
            continue
        t0 = htf.index[i]
        if t0 < m.index[0]:
            continue  # 15m data does not cover candle 2
        if busy is not None and t0 + delta < busy:
            continue  # previous trade still open
        j = find_mss(m, s, t0, t0 + delta, t0 + 2 * delta, stop_arr[i], tgt_arr[i], hrs)
        if j is None or j + 1 >= len(m):
            continue
        entry, stop, tgt = o[j + 1], stop_arr[i], tgt_arr[i]
        ok, risk = valid(s, entry, stop, tgt, a[i])
        if not ok:
            continue
        end, k, ex = min(len(m) - 1, j + 1 + hold), j + 1, None
        while k <= end:
            hit_stop = l[k] <= stop if s == 1 else h[k] >= stop
            hit_tgt = h[k] >= tgt if s == 1 else l[k] <= tgt
            if hit_stop:
                ex = stop
                break
            if hit_tgt:
                ex = tgt
                break
            k += 1
        if ex is None:
            k, ex = end, c[end]
        trades.append({"entry_time": m.index[j + 1], "dir": s, "R": ((ex - entry) * s - spread) / risk})
        busy = m.index[k] + STEP
    return pd.DataFrame(trades, columns=["entry_time", "dir", "R"])


def judge_mss(s_all):
    if s_all["trades"] < MSS_MIN_TRADES:
        return "INSUFFICIENT"
    return "PASS" if s_all["avg_R"] > 0.05 and s_all["profit_factor"] > 1.1 else "FAIL"


# ---------------------------------------------------------------- data
def load15(symbol, period="58d"):
    """15m candles from Yahoo Finance (max ~60 days), unfinished candle dropped."""
    import yfinance as yf
    raw = yf.download(symbol, interval="15m", period=period, auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"no 15m data for {symbol}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
    idx = pd.to_datetime(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    return df[df.index + STEP <= pd.Timestamp.now(tz="UTC")]


_DEMO = {}


def _demo_full(symbol):
    """Fake random-walk 15m candles (about 500 days) shared by the demo loaders."""
    if symbol not in _DEMO:
        n = 3000 * 16
        rng = np.random.default_rng(sum(ord(ch) for ch in symbol) + 7)
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, n)))
        open_ = np.concatenate([[close[0]], close[:-1]])
        wick = np.abs(rng.normal(0, 0.0007, n))
        idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("15min") - STEP, periods=n, freq="15min")
        _DEMO[symbol] = pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + wick),
                                      "low": np.minimum(open_, close) * (1 - wick), "close": close}, index=idx)
    return _DEMO[symbol]


def demo_load(symbol, tf, period):
    hrs = ft.TF_HOURS[tf]
    df = _demo_full(symbol).resample(f"{hrs}h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return df[df.index + pd.Timedelta(hours=hrs) <= pd.Timestamp.now(tz="UTC")]


def demo_load15(symbol, period="58d"):
    days = int(period.rstrip("d"))
    return _demo_full(symbol).tail(days * 96)


# ---------------------------------------------------------------- backtest runner
def run_backtest(tf, pairs, notify=True, write=True):
    hrs = ft.TF_HOURS[tf]
    combos, rows = {}, []
    for pair in pairs:
        try:
            df = ft.load(ft.TICKER[pair], tf, "720d")
            m = load15(ft.TICKER[pair], "58d")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {pair}: could not load data ({e.__class__.__name__}: {e})")
            continue
        a = ft.atr(df).values
        cut = df.index[int(len(df) * 0.6)]
        for rule in BASE_RULES:
            sig, st, tg = crt_levels(df, a, rule.endswith("_trend"))
            tr = backtest_crt(df, sig, st, tg, a, ft.SPREAD[pair])
            s_all, s_in = ft.stats(tr), ft.stats(tr[tr["entry_time"] < cut])
            s_out = ft.stats(tr[tr["entry_time"] >= cut])
            v = ft.judge(s_in, s_out, s_all["trades"])
            combos[f"{pair}|{rule}"] = {"verdict": v, "trades": s_all["trades"], "avg_R": round(s_all["avg_R"], 3),
                                        "oos_trades": s_out["trades"], "oos_avg_R": round(s_out["avg_R"], 3),
                                        "oos_pf": round(s_out["profit_factor"], 2)}
            rows.append((pair, rule, s_all, f"{s_out['avg_R']:.2f}", v))
        for rule in MSS_RULES:
            sig, st, tg = crt_levels(df, a, rule.endswith("_trend"))
            tr = backtest_mss(df, m, sig, st, tg, a, ft.SPREAD[pair], hrs)
            s_all = ft.stats(tr)
            v = judge_mss(s_all)
            combos[f"{pair}|{rule}"] = {"verdict": v, "trades": s_all["trades"], "avg_R": round(s_all["avg_R"], 3),
                                        "pf": round(s_all["profit_factor"], 2)}
            rows.append((pair, rule, s_all, "n/a", v))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"\n=== CRT backtest {now} UTC | {tf} candles | spread included ===")
    print("crt / crt_trend = pattern only, ~2 years.  crt_mss / crt_mss_trend = 15m entry, ~58 days only.")
    print(f"{'PAIR':<8}{'RULE':<15}{'TRADES':>6}{'WIN%':>6}{'AVG R':>7}{'PF':>6}{'MAXDD R':>8}{'OOS R':>7}  VERDICT")
    for pair, rule, s, oos, v in rows:
        print(f"{pair:<8}{rule:<15}{s['trades']:>6}{s['win_rate']:>6.0f}{s['avg_R']:>7.2f}"
              f"{s['profit_factor']:>6.2f}{s['max_dd_R']:>8.1f}{oos:>7}  {v}")
    passed = [(k.split("|")[0], k.split("|")[1], c) for k, c in combos.items() if c["verdict"] == "PASS"]
    print(f"\n{len(passed)} of {len(combos)} passed. Some pass by luck; the 15m rules rest on a small sample.")
    if write:
        ft.write_json(RESULTS_FILE, {"tf": tf, "generated": now, "combos": combos})
        if not os.path.exists(STATE_FILE):
            ft.write_json(STATE_FILE, {})
    if notify and combos:
        base_pass = [f"{p} {r}: out-of-sample {c['oos_avg_R']:+.2f}R, {c['trades']} trades"
                     for p, r, c in passed if r in BASE_RULES]
        mss_info = [f"{k.replace('|', ' ')}: {c['trades']} trades, {c['avg_R']:+.2f}R avg"
                    for k, c in combos.items() if k.split("|")[1] in MSS_RULES and c["trades"] > 0]
        msg = (f"📊 CRT backtest ({tf})\n"
               f"Pattern only, ~2y: {chr(10).join(base_pass) if base_pass else 'no rule passed.'}\n"
               f"15m entry, last ~58 days only (small sample):\n"
               f"{chr(10).join(mss_info) if mss_info else 'no trades found.'}\n"
               f"Live alerts will be tracked so you get a real forward record.")
        ft.send_telegram(msg)
    return combos


# ---------------------------------------------------------------- forward tracking
def resolve_open(state, data15, now, notify=True):
    """Check every alerted trade against the 15m bars since the alert; report target/stop/expiry."""
    tally = state.setdefault("tally", {"n": 0, "wins": 0, "sumR": 0.0})
    keep = []
    for t in state.get("open", []):
        m = data15.get(t["pair"])
        if m is None:
            keep.append(t)
            continue
        t0 = pd.Timestamp(t["time"])
        bars = m[m.index >= t0]
        s, ex, why = t["side"], None, ""
        for row in bars.itertuples():
            hit_stop = row.low <= t["stop"] if s == 1 else row.high >= t["stop"]
            hit_tgt = row.high >= t["target"] if s == 1 else row.low <= t["target"]
            if hit_stop:
                ex, why = t["stop"], "stop hit"
                break
            if hit_tgt:
                ex, why = t["target"], "target hit"
                break
        if ex is None and (now - t0) > pd.Timedelta(hours=MAX_BARS * t["hrs"]):
            if len(bars) == 0:
                continue  # cannot resolve, drop it
            ex, why = float(bars["close"].iloc[-1]), "expired"
        if ex is None:
            keep.append(t)
            continue
        r = float(((ex - t["entry"]) * s - ft.SPREAD[t["pair"]]) / t["risk"])
        tally["n"] += 1
        tally["wins"] += int(r > 0)
        tally["sumR"] = round(tally["sumR"] + r, 3)
        msg = (f"{'✅' if r > 0 else '❌'} CRT {'LONG' if s == 1 else 'SHORT'} {t['pair']} closed ({why}): {r:+.1f}R\n"
               f"Forward record: {tally['n']} trades, {tally['wins']} wins, {tally['sumR']:+.1f}R total")
        print(msg.replace("\n", " | "))
        if notify:
            ft.send_telegram(msg)
    state["open"] = keep


# ---------------------------------------------------------------- scanner
def run_scan(tf, pairs, notify=True, write=True, now=None):
    res, state = ft.read_json(RESULTS_FILE), ft.read_json(STATE_FILE)
    combos = res.get("combos", {}) if res.get("tf") == tf else {}
    hrs = ft.TF_HOURS[tf]
    delta = pd.Timedelta(hours=hrs)
    now = now if now is not None else pd.Timestamp.now(tz="UTC")
    state.setdefault("open", [])
    state.setdefault("tally", {"n": 0, "wins": 0, "sumR": 0.0})
    data15, setups = {}, []
    alerts = 0
    for pair in pairs:
        try:
            m = load15(ft.TICKER[pair], "5d")
            df = ft.load(ft.TICKER[pair], tf, "60d")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {pair}: could not load data ({e.__class__.__name__})")
            continue
        data15[pair] = m
        setups.append((pair, m, df))
    resolve_open(state, data15, now, notify)  # settle old alerts first
    for pair, m, df in setups:
        if len(df) < 60 or len(m) < 20:
            continue
        a = ft.atr(df).values
        bar_time = df.index[-1]  # candle 2 (last closed higher-timeframe candle)
        if now >= bar_time + 2 * delta:
            continue  # the entry window (next candle) is over, or the market is closed
        for rule in MSS_RULES:
            sig, st, tg = crt_levels(df, a, rule.endswith("_trend"))
            s, key = int(sig[-1]), f"{pair}|{rule}|{tf}"
            if s == 0 or state.get(key) == str(bar_time):
                continue
            stop, tgt = float(st[-1]), float(tg[-1])
            j = find_mss(m, s, bar_time, bar_time + delta, bar_time + 2 * delta, stop, tgt, hrs)
            if j is None:
                continue
            age_min = (now - (m.index[j] + STEP)).total_seconds() / 60
            after = m.iloc[j + 1:]
            played_out = (len(after) > 0 and ((after["low"].min() <= stop or after["high"].max() >= tgt)
                                              if s == 1 else
                                              (after["high"].max() >= stop or after["low"].min() <= tgt)))
            verdict = combos.get(f"{pair}|{rule}", {}).get("verdict", "UNTESTED")
            base_rule = "crt_trend" if rule.endswith("_trend") else "crt"
            base_v = combos.get(f"{pair}|{base_rule}", {}).get("verdict", "UNTESTED")
            entry = float(m["close"].iloc[-1])
            ok, risk = valid(s, entry, stop, tgt, a[-1])
            if age_min > MAX_MSS_AGE_MIN or played_out or verdict == "FAIL" or not ok:
                continue
            dp = 3 if pair == "USDJPY" else 2 if pair == "XAUUSD" else 5
            msg = (f"{'🟢 CRT LONG' if s == 1 else '🔴 CRT SHORT'} {pair} ({tf}) [{rule}]\n"
                   f"The {tf} candle swept the previous candle's {'low' if s == 1 else 'high'} and closed back inside, "
                   f"then the 15m structure shifted ({age_min:.0f} min ago).\n"
                   f"Ref {entry:.{dp}f} | stop {stop:.{dp}f} | target {tgt:.{dp}f} | RR {abs(tgt - entry) / risk:.1f}\n"
                   f"Verdicts: 15m entry {verdict} (only ~58 days of data) | pattern {base_v}\n"
                   f"Risk 1% or less. Not a guarantee.")
            print(msg.replace("\n", " | "))
            if notify:
                ft.send_telegram(msg)
            state[key] = str(bar_time)
            state["open"].append({"pair": pair, "rule": rule, "side": s, "entry": entry, "stop": stop,
                                  "target": tgt, "risk": float(risk), "hrs": hrs,
                                  "time": str(m.index[-1] + STEP)})
            alerts += 1
    print(f"Scan done: {alerts} new signal(s), {len(state['open'])} open.")
    if write:
        ft.write_json(STATE_FILE, state)
        if not os.path.exists(RESULTS_FILE):
            ft.write_json(RESULTS_FILE, {})
    return alerts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--demo", action="store_true", help="fake data, no internet, nothing saved")
    ap.add_argument("--tf", default=os.getenv("FOREX_TF", "4h"))
    args = ap.parse_args()
    if args.tf not in ft.TF_HOURS:
        print("Unsupported timeframe, use 1h or 4h")
        return 1
    pairs = ft.get_pairs()
    if args.demo:
        ft.load, globals()["load15"] = demo_load, demo_load15
        run_backtest(args.tf, pairs, notify=False, write=False)
    elif args.scan:
        run_scan(args.tf, pairs)
    elif args.backtest:
        run_backtest(args.tf, pairs)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
