#!/usr/bin/env python3
"""
Forex backtester + signal scanner (alert-only, does NOT trade).

Three classic rules are tested on ~2 years of past data (spread cost included):
  ema_cross          trend: EMA20 crosses EMA50
  rsi_reversion      reversal: RSI(14) leaves oversold/overbought
  donchian_breakout  breakout: close beats the previous 20-bar high/low
Every trade uses a 1.5 x ATR stop and a 2R target, one trade at a time.

--backtest  tests every pair x rule. The first 60% of history is "in-sample", the last 40%
            is "out-of-sample" (rules never tuned on it). A rule gets PASS only if it is
            profitable in BOTH parts, with enough trades. Results go to forex_results.json
            and a Telegram summary.
--scan      looks at the latest CLOSED candle and alerts on fresh signals, skipping rules
            whose backtest verdict is FAIL. State goes to forex_state.json.
--demo      runs the backtest on fake random data (no internet) to test the code.

Settings (environment variables or flags):
  FOREX_TF      4h (default) or 1h      (--tf also works)
  FOREX_PAIRS   default "EURUSD,GBPUSD,USDJPY,XAUUSD"; also AUDUSD, USDCAD
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

Needs: pip install requests pandas numpy yfinance
IMPORTANT: a PASS means "held up in past data", not "will make money". With a dozen
combinations tested, a few will pass by luck. Forward-test with tiny risk before trusting it.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

TICKER = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
          "XAUUSD": "GC=F",  # gold futures as a proxy for spot gold
          "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X"}
# Assumed round-trip spread in price units (roughly 1 pip; 0.30 for gold). Real spreads vary.
SPREAD = {"EURUSD": 0.00010, "GBPUSD": 0.00012, "USDJPY": 0.010, "XAUUSD": 0.30,
          "AUDUSD": 0.00012, "USDCAD": 0.00015}
TF_HOURS = {"1h": 1, "4h": 4}

STOP_ATR = 1.5
R_MULT = 2.0
MAX_BARS = 30
MIN_TRADES = 40
RESULTS_FILE = "forex_results.json"
STATE_FILE = "forex_state.json"


# ---------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ---------------------------------------------------------------- strategies (+1 long, -1 short)
def _signal(long_cond, short_cond, index):
    s = pd.Series(0, index=index)
    s[long_cond.fillna(False)] = 1
    s[short_cond.fillna(False)] = -1
    return s


def sig_ema_cross(df):
    e1, e2 = ema(df["close"], 20), ema(df["close"], 50)
    return _signal((e1 > e2) & (e1.shift() <= e2.shift()), (e1 < e2) & (e1.shift() >= e2.shift()), df.index)


def sig_rsi_reversion(df):
    r = rsi(df["close"])
    return _signal((r.shift() < 30) & (r >= 30), (r.shift() > 70) & (r <= 70), df.index)


def sig_donchian_breakout(df):
    hh = df["high"].rolling(20).max().shift()
    ll = df["low"].rolling(20).min().shift()
    up, dn = df["close"] > hh, df["close"] < ll
    return _signal(up & ~up.shift(fill_value=False), dn & ~dn.shift(fill_value=False), df.index)


STRATEGIES = {"ema_cross": sig_ema_cross, "rsi_reversion": sig_rsi_reversion,
              "donchian_breakout": sig_donchian_breakout}


# ---------------------------------------------------------------- data
def make_demo(symbol, tf, n=3000):
    """Fake random-walk candles for offline testing."""
    h = TF_HOURS[tf]
    rng = np.random.default_rng(sum(ord(ch) for ch in symbol))
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 0.0015, n))
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=h), periods=n, freq=f"{h}h")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + wick),
                         "low": np.minimum(open_, close) * (1 - wick), "close": close}, index=idx)


def load(symbol, tf, period):
    """Download hourly candles from Yahoo Finance, resample to tf, drop the unfinished candle."""
    import yfinance as yf
    raw = yf.download(symbol, interval="1h", period=period, auto_adjust=True, progress=False)
    if raw is None or raw.empty:
        raise RuntimeError(f"no data for {symbol}")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close"]].dropna()
    idx = pd.to_datetime(df.index)
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    h = TF_HOURS[tf]
    if h > 1:
        df = df.resample(f"{h}h").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return df[df.index + pd.Timedelta(hours=h) <= pd.Timestamp.now(tz="UTC")]


# ---------------------------------------------------------------- backtest engine
def backtest(df, signals, atr_vals, spread, stop_mult=STOP_ATR, r_mult=R_MULT, max_bars=MAX_BARS):
    """Signal on candle i's close -> enter at candle i+1's open. If stop and target are hit in
    the same candle, the stop is assumed to hit first (pessimistic). Returns trades with R results."""
    o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
    n, trades, i = len(df), [], 0
    while i < n - 1:
        s = int(signals[i])
        if s == 0 or np.isnan(atr_vals[i]):
            i += 1
            continue
        ei = i + 1
        entry, risk = o[ei], stop_mult * atr_vals[i]
        stop, tgt = entry - s * risk, entry + s * risk * r_mult
        end, j, exit_price = min(n - 1, ei + max_bars), ei, None
        while j <= end:
            hit_stop = l[j] <= stop if s == 1 else h[j] >= stop
            hit_tgt = h[j] >= tgt if s == 1 else l[j] <= tgt
            if hit_stop:
                exit_price = stop
                break
            if hit_tgt:
                exit_price = tgt
                break
            j += 1
        if exit_price is None:
            j, exit_price = end, c[end]
        trades.append({"entry_time": df.index[ei], "dir": s, "R": ((exit_price - entry) * s - spread) / risk})
        i = j + 1  # one trade at a time
    return pd.DataFrame(trades, columns=["entry_time", "dir", "R"])


def stats(tr):
    if tr is None or len(tr) == 0:
        return {"trades": 0, "win_rate": 0.0, "avg_R": 0.0, "profit_factor": 0.0, "max_dd_R": 0.0, "total_R": 0.0}
    r = tr["R"].values
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    eq = np.cumsum(r)
    return {"trades": int(len(r)), "win_rate": float((r > 0).mean() * 100), "avg_R": float(r.mean()),
            "profit_factor": float(min(wins / losses, 99)) if losses > 0 else 99.0,
            "max_dd_R": float((np.maximum.accumulate(eq) - eq).max()), "total_R": float(r.sum())}


def judge(s_in, s_out, total):
    if total < MIN_TRADES:
        return "INSUFFICIENT"
    if s_in["avg_R"] > 0 and s_out["trades"] >= 15 and s_out["avg_R"] > 0.05 and s_out["profit_factor"] > 1.1:
        return "PASS"
    return "FAIL"


# ---------------------------------------------------------------- io helpers
def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def write_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)


def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[info] Telegram not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            print(f"[warn] telegram HTTP {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"[warn] telegram failed ({e.__class__.__name__})")
        return False


def get_pairs():
    names = [p.strip().upper() for p in os.getenv("FOREX_PAIRS", "EURUSD,GBPUSD,USDJPY,XAUUSD").split(",") if p.strip()]
    bad = [p for p in names if p not in TICKER]
    if bad:
        print(f"[warn] unknown pairs ignored: {', '.join(bad)} (supported: {', '.join(TICKER)})")
    return [p for p in names if p in TICKER]


# ---------------------------------------------------------------- backtest runner
def run_backtest(tf, pairs, notify=True, write=True):
    combos, rows = {}, []
    for pair in pairs:
        try:
            df = load(TICKER[pair], tf, "720d")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {pair}: could not load data ({e.__class__.__name__}: {e})")
            continue
        a = atr(df).values
        cut = df.index[int(len(df) * 0.6)]
        for sname, fn in STRATEGIES.items():
            tr = backtest(df, fn(df).fillna(0).astype(int).values, a, SPREAD[pair])
            s_all = stats(tr)
            s_in, s_out = stats(tr[tr["entry_time"] < cut]), stats(tr[tr["entry_time"] >= cut])
            verdict = judge(s_in, s_out, s_all["trades"])
            combos[f"{pair}|{sname}"] = {"verdict": verdict, "trades": s_all["trades"],
                                         "avg_R": round(s_all["avg_R"], 3),
                                         "oos_trades": s_out["trades"], "oos_avg_R": round(s_out["avg_R"], 3),
                                         "oos_pf": round(s_out["profit_factor"], 2)}
            rows.append((pair, sname, s_all, s_out, verdict))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    print(f"\n=== Forex backtest {now} UTC | {tf} candles | spread included | stop {STOP_ATR} ATR, target {R_MULT}R ===")
    print(f"{'PAIR':<8}{'RULE':<19}{'TRADES':>6}{'WIN%':>6}{'AVG R':>7}{'PF':>6}{'MAXDD R':>8}{'OOS R':>7}  VERDICT")
    for pair, sname, s, so, v in rows:
        print(f"{pair:<8}{sname:<19}{s['trades']:>6}{s['win_rate']:>6.0f}{s['avg_R']:>7.2f}"
              f"{s['profit_factor']:>6.2f}{s['max_dd_R']:>8.1f}{so['avg_R']:>7.2f}  {v}")
    passed = [(p, n, c) for (p, n), c in ((k.split("|"), v) for k, v in combos.items()) if c["verdict"] == "PASS"]
    print(f"\n{len(passed)} of {len(combos)} combinations passed. Some pass by luck; forward-test with tiny risk.")

    if write:
        write_json(RESULTS_FILE, {"tf": tf, "generated": now, "combos": combos})
        if not os.path.exists(STATE_FILE):
            write_json(STATE_FILE, {})
    if notify and combos:
        if passed:
            lines = [f"{p} {n}: out-of-sample {c['oos_avg_R']:+.2f}R avg, {c['oos_trades']} trades" for p, n, c in passed]
            
