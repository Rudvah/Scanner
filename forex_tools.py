"""Forex backtester + signal scanner (alert-only, does NOT trade).
Rules: ema_cross (EMA20/EMA50), rsi_reversion (RSI14 leaves 30/70), donchian_breakout (20-bar high/low).
Every trade: 1.5 x ATR stop, 2R target, one trade at a time, spread included.
--backtest  tests every pair x rule on ~2 years; first 60% in-sample, last 40% out-of-sample.
--scan      alerts on fresh signals on the latest closed candle, skipping rules that FAILED.
--demo      backtest on fake data (no internet).
FOREX_TF: 4h, 1h, or all (both). FOREX_PAIRS: default EURUSD,GBPUSD,USDJPY,XAUUSD.
Needs: pip install requests pandas numpy yfinance"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

TICKER = {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
          "XAUUSD": "GC=F",
          "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X"}
SPREAD = {"EURUSD": 0.00010, "GBPUSD": 0.00012, "USDJPY": 0.010, "XAUUSD": 0.30,
          "AUDUSD": 0.00012, "USDCAD": 0.00015}
TF_HOURS = {"1h": 1, "4h": 4}

STOP_ATR = 1.5
R_MULT = 2.0
MAX_BARS = 30
MIN_TRADES = 40
RESULTS_FILE = "forex_results.json"
STATE_FILE = "forex_state.json"


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


def make_demo(symbol, tf, n=3000):
    h = TF_HOURS[tf]
    rng = np.random.default_rng(sum(ord(ch) for ch in symbol))
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, 0.0015, n))
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h") - pd.Timedelta(hours=h), periods=n, freq=f"{h}h")
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * (1 + wick),
                         "low": np.minimum(open_, close) * (1 - wick), "close": close}, index=idx)


def load(symbol, tf, period):
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


def backtest(df, signals, atr_vals, spread, stop_mult=STOP_ATR, r_mult=R_MULT, max_bars=MAX_BARS):
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
        i = j + 1
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
                                         "avg_R": round(s_all["avg_R"], 3), "win_rate": round(s_all["win_rate"]),
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
        res = read_json(RESULTS_FILE)
        res.pop("tf", None), res.pop("combos", None), res.pop("generated", None)
        res.setdefault("by_tf", {})[tf] = {"generated": now, "combos": combos}
        write_json(RESULTS_FILE, res)
        if not os.path.exists(STATE_FILE):
            write_json(STATE_FILE, {})
    if notify and combos:
        order = {"PASS": 0, "INSUFFICIENT": 1, "FAIL": 2}
        ranked = sorted(combos.items(), key=lambda kv: (order.get(kv[1]["verdict"], 3), -kv[1]["oos_avg_R"]))
        icon = {"PASS": "✅", "FAIL": "❌", "INSUFFICIENT": "❔"}
        lines = [f"{icon.get(c['verdict'], '')} {k.split('|')[0]} {k.split('|')[1]}: {c['verdict']} | {c['trades']} trades, "
                 f"win {c.get('win_rate', 0)}%, avg {c['avg_R']:+.2f}R, recent {c['oos_avg_R']:+.2f}R ({c['oos_trades']})"
                 for k, c in ranked]
        msg = (f"📊 Forex reliability ({tf}, ~2y of past data)\n" + "\n".join(lines) +
               f"\n\n{len(passed)} of {len(combos)} passed. PASS = profitable in both the older and the recent part of the data. "
               "FAIL = not reliable, so the bot will not send signals for it. ❔ = too few trades to judge. "
               "Avg R = average result per trade in units of risk. Past results only: some passes are luck, so risk little.")
        send_telegram(msg)
    return combos


def run_scan(tf, pairs, notify=True, write=True):
    results, state = read_json(RESULTS_FILE), read_json(STATE_FILE)
    combos = (results.get("by_tf", {}).get(tf) or {}).get("combos") or (results.get("combos", {}) if results.get("tf") == tf else {})
    h = TF_HOURS[tf]
    now = pd.Timestamp.now(tz="UTC")
    alerts = 0
    for pair in pairs:
        try:
            df = load(TICKER[pair], tf, "60d")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {pair}: could not load data ({e.__class__.__name__})")
            continue
        if len(df) < 60:
            continue
        a = atr(df)
        bar_time = df.index[-1]
        if now - (bar_time + pd.Timedelta(hours=h)) > pd.Timedelta(hours=2 * h):
            continue
        for sname, fn in STRATEGIES.items():
            s = int(fn(df).fillna(0).iloc[-1])
            key = f"{pair}|{sname}|{tf}"
            if s == 0 or state.get(key) == str(bar_time):
                continue
            c = combos.get(f"{pair}|{sname}", {})
            verdict = c.get("verdict", "UNTESTED")
            if verdict == "FAIL":
                continue
            note = {"PASS": "RELIABLE so far", "INSUFFICIENT": "too few past trades to judge",
                    "UNTESTED": "not backtested yet"}.get(verdict, verdict)
            past = (f"\nPast {tf}: {c['trades']} trades, win {c.get('win_rate', 0)}%, avg {c['avg_R']:+.2f}R, "
                    f"recent {c['oos_avg_R']:+.2f}R") if "trades" in c and "avg_R" in c else ""
            entry, risk = float(df["close"].iloc[-1]), float(STOP_ATR * a.iloc[-1])
            dp = 3 if pair == "USDJPY" else 2 if pair == "XAUUSD" else 5
            msg = (f"{'🟢 LONG' if s == 1 else '🔴 SHORT'} {pair} ({tf}) via {sname}\n"
                   f"Ref price {entry:.{dp}f} | stop {entry - s * risk:.{dp}f} | target {entry + s * risk * R_MULT:.{dp}f}\n"
                   f"Reliability: {verdict} ({note}){past}\nNot a guarantee. Risk 1% or less and always use the stop.")
            print(msg.replace("\n", " | "))
            if notify:
                send_telegram(msg)
            state[key] = str(bar_time)
            alerts += 1
    print(f"Scan done: {alerts} new signal(s).")
    if write:
        write_json(STATE_FILE, state)
        if not os.path.exists(RESULTS_FILE):
            write_json(RESULTS_FILE, {})
    return alerts


def main():
    global load  # noqa: PLW0603
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--demo", action="store_true", help="fake data, no internet, nothing saved")
    ap.add_argument("--tf", default=os.getenv("FOREX_TF", "4h"))
    args = ap.parse_args()
    tfs = list(TF_HOURS) if args.tf.lower() == "all" else [t.strip() for t in args.tf.split(",") if t.strip()]
    bad = [t for t in tfs if t not in TF_HOURS]
    if bad or not tfs:
        print(f"Unsupported timeframe '{args.tf}', use 1h, 4h or all")
        return 1
    pairs = get_pairs()
    if args.demo:
        load = lambda symbol, tf, period: make_demo(symbol, tf)  # noqa: E731
        for tf in tfs:
            run_backtest(tf, pairs, notify=False, write=False)
        return 0
    if not (args.scan or args.backtest):
        ap.print_help()
        return 0
    for tf in tfs:
        run_scan(tf, pairs) if args.scan else run_backtest(tf, pairs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
  
