#!/usr/bin/env python3
"""
Web3 radar (alert-only, does NOT trade). One Telegram digest with:
  1. Early-stage, VC-backed TOKENS, ranked 0-100 with red flags
  2. Newly funded projects with NO token found yet (airdrop / points candidates)
  3. Money-relevant Web3 news headlines (airdrops, listings, funding, mainnets, hacks)

Data: DefiLlama (funding rounds, protocols, fees), CoinGecko (market data), public crypto RSS.
Run:  python radar.py --digest     send the digest (needs TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
      python radar.py --dry        print it only: no Telegram, nothing saved
Optional: COINGECKO_API_KEY (free demo key) if CoinGecko rate-limits you.

The 0-100 score is a RULE OF THUMB I designed, not a tested predictor. VC backing lowers the
risk of a scam; it does not make a token go up (many VC-backed tokens fall hard after launch).
Always check token unlocks, tokenomics and the team yourself.
"""
import argparse
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

LLAMA = "https://api.llama.fi"
GECKO = "https://api.coingecko.com/api/v3"
FEEDS = [("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
         ("Cointelegraph", "https://cointelegraph.com/rss"),
         ("Decrypt", "https://decrypt.co/feed")]
STATE_FILE = "radar_state.json"

CFG = {"lookback_days": 365,   # funding rounds older than this are ignored
       "new_days": 45,          # "newly funded" window for no-token projects
       "min_mcap": 3e6, "max_mcap": 300e6,   # early-stage = small market cap
       "min_raised_no_t1": 10.0,             # $M raised needed if no top-tier investor
       "top_n": 8, "tokenless_n": 6, "news_n": 8, "news_hours": 36}

# Investor names (lowercase fragments) treated as top-tier. Edit freely.
T1_KEYS = ["a16z", "andreessen", "paradigm", "polychain", "pantera", "multicoin", "sequoia",
           "coinbase ventures", "binance labs", "yzi labs", "framework ventures", "dragonfly",
           "hack vc", "delphi", "jump", "galaxy", "variant", "electric capital", "lightspeed",
           "founders fund", "animoca", "spartan", "hashkey", "okx ventures", "bain capital",
           "1kx", "consensys", "placeholder", "blockchain capital", "fenbushi", "tiger global", "ribbit"]
NEWS_KEYS = ["airdrop", "token launch", "tge", "listing", "lists", "to list", "unlock", "raises",
             "raised", "funding", "mainnet", "snapshot", "points program", "staking", "binance",
             "coinbase", "etf", "hack", "exploit", "rug", "testnet", "launchpad"]


# ---------------------------------------------------------------- helpers
def http_get(url, params=None, text=False, retries=3):
    headers = {"User-Agent": "web3-radar/1.0"}
    if "coingecko" in url and os.getenv("COINGECKO_API_KEY"):
        headers["x-cg-demo-api-key"] = os.getenv("COINGECKO_API_KEY")
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 429:
                last = "rate limited"
                time.sleep(6 * (i + 1))
                continue
            r.raise_for_status()
            return r.text if text else r.json()
        except Exception as e:  # noqa: BLE001
            last = e.__class__.__name__
            time.sleep(1 + i)
    raise RuntimeError(f"GET failed ({last}): {url.split('?')[0]}")


def f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def money(x):
    x = f(x)
    return f"${x / 1e9:.1f}B" if x >= 1e9 else f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x / 1e3:.0f}K" if x >= 1e3 else f"${x:.0f}"


def is_t1(name):
    n = str(name).lower()
    return any(k in n for k in T1_KEYS)


def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[info] Telegram not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    ok, chunk = True, ""
    parts = []
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3800:
            parts.append(chunk)
            chunk = ""
        chunk += line + "\n"
    parts.append(chunk)
    for part in parts:
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              json={"chat_id": chat, "text": part, "disable_web_page_preview": True}, timeout=15)
            if r.status_code != 200:
                print(f"[warn] telegram HTTP {r.status_code}: {r.text[:200]}")
                ok = False
        except Exception as e:  # noqa: BLE001
            print(f"[warn] telegram failed ({e.__class__.__name__})")
            ok = False
    return ok


def load_state():
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
        st = st if isinstance(st, dict) else {}
    except (FileNotFoundError, ValueError):
        st = {}
    for k, v in (("picks", {}), ("seen_news", []), ("seen_raises", [])):
        st.setdefault(k, v)
    return st


# ---------------------------------------------------------------- funding rounds -> projects
def group_projects(raises, now, lookback_days):
    projects = {}
    for r in raises:
        ts = f(r.get("date"))
        if ts <= 0 or (now - ts) / 86400 > lookback_days:
            continue
        name = str(r.get("name") or "?")
        key = str(r.get("defillamaId") or name).lower()
        leads = [str(x) for x in (r.get("leadInvestors") or [])]
        others = [str(x) for x in (r.get("otherInvestors") or [])]
        p = projects.setdefault(key, {"key": key, "name": name, "llama_id": str(r.get("defillamaId") or ""),
                                      "total": 0.0, "t1": set(), "lead_t1": False, "leads": [],
                                      "rounds": [], "last": 0.0, "sector": r.get("category") or r.get("sector") or ""})
        p["total"] += f(r.get("amount"))
        p["t1"] |= {i for i in leads + others if is_t1(i)}
        p["lead_t1"] = p["lead_t1"] or any(is_t1(i) for i in leads)
        p["leads"] += [i for i in leads if i not in p["leads"]]
        p["rounds"].append(str(r.get("round") or "?"))
        p["last"] = max(p["last"], ts)
    return projects


def attach_protocols(projects, protocols):
    by_id = {str(x.get("id")): x for x in protocols}
    by_name = {str(x.get("name", "")).lower(): x for x in protocols}
    for p in projects.values():
        p["proto"] = (by_id.get(p["llama_id"]) if p["llama_id"] else None) or by_name.get(p["name"].lower())


def load_fees():
    try:
        data = http_get(LLAMA + "/overview/fees", params={"excludeTotalDataChart": "true",
                                                           "excludeTotalDataChartBreakdown": "true"})
    except Exception as e:  # noqa: BLE001
        print(f"[warn] fees data unavailable ({e})")
        return {}
    out = {}
    for x in data.get("protocols", []) or []:
        v = f(x.get("total30d"))
        out[str(x.get("defillamaId"))] = v
        out[str(x.get("name", "")).lower()] = v
    return out


def load_market(ids):
    out = {}
    ids = sorted(set(i for i in ids if i))
    for i in range(0, len(ids), 80):
        chunk = ids[i:i + 80]
        try:
            rows = http_get(GECKO + "/coins/markets", params={"vs_currency": "usd", "ids": ",".join(chunk),
                                                              "per_page": 100, "price_change_percentage": "7d,30d"})
        except Exception as e:  # noqa: BLE001
            print(f"[warn] market data unavailable for a batch ({e})")
            continue
        for row in rows or []:
            out[row.get("id")] = row
        time.sleep(2.5)
    return out


# ---------------------------------------------------------------- scoring (rule of thumb, untested)
def score(p, md, fees30, now):
    proto, flags = p.get("proto") or {}, []
    # VC backing (max 30)
    v = 18 if p["lead_t1"] else 10 if p["t1"] else 0
    v += 8 if p["total"] >= 50 else 6 if p["total"] >= 20 else 4 if p["total"] >= 5 else 2 if p["total"] >= 1 else 0
    v += 4 if len(p["t1"]) >= 3 else 0
    if not p["t1"]:
        flags.append("no top-tier investor")
    # traction (max 25)
    tvl, ch30 = f(proto.get("tvl")), f(proto.get("change_1m"))
    t = 10 if tvl >= 50e6 else 8 if tvl >= 10e6 else 5 if tvl >= 2e6 else 3 if tvl >= 1e6 else 0
    if tvl <= 0:
        flags.append("no TVL data")
    t += 8 if ch30 > 20 else 5 if ch30 > 5 else 3 if ch30 >= 0 else 0
    if ch30 < -20:
        t -= 5
        flags.append(f"TVL down {abs(ch30):.0f}% in 30d")
    t += 7 if fees30 >= 100e3 else 4 if fees30 >= 10e3 else 0
    # valuation (max 25)
    mcap, fdv = f(md.get("market_cap")), f(md.get("fully_diluted_valuation"))
    val = 0
    if mcap > 0 and tvl > 0:
        mt = mcap / tvl
        val += 12 if mt <= 0.5 else 8 if mt <= 1.5 else 4 if mt <= 4 else 0
        if mt > 10:
            flags.append(f"market cap is {mt:.0f}x TVL")
    if mcap > 0 and fdv > 0:
        r = fdv / mcap
        val += 13 if r <= 1.5 else 9 if r <= 2.5 else 4 if r <= 5 else 0
        if r > 5:
            flags.append(f"FDV is {r:.0f}x market cap: heavy future unlocks")
    else:
        flags.append("FDV unknown")
    # liquidity (max 10)
    turn = f(md.get("total_volume")) / mcap if mcap > 0 else 0
    liq = 10 if 0.03 <= turn <= 1 else 5 if 0.01 <= turn < 0.03 else 0
    if turn < 0.01:
        flags.append("thin trading")
    # freshness of last funding (max 10)
    age = (now - p["last"]) / 86400
    fresh = 10 if age <= 120 else 6 if age <= 270 else 3
    total = min(v, 30) + max(0, min(t, 25)) + min(val, 25) + liq + fresh
    return round(total), flags


# ---------------------------------------------------------------- news
def fetch_news(state, now):
    items = []
    for src, url in FEEDS:
        try:
            root = ET.fromstring(http_get(url, text=True))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] feed {src} unavailable ({e.__class__.__name__})")
            continue
        for it in root.iter("item"):
            title, link = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()
            try:
                ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
            except (TypeError, ValueError):
                continue
            low = title.lower()
            if (title and link and now - ts <= CFG["news_hours"] * 3600 and link not in state["seen_news"]
                    and any(k in low for k in NEWS_KEYS)):
                items.append({"src": src, "title": title, "link": link, "ts": ts})
    items.sort(key=lambda x: -x["ts"])
    out, seen = [], set()
    for it in items:  # the same story often appears in several feeds
        k = (it["link"], " ".join(it["title"].lower().split()))
        if k[0] in seen or k[1] in seen:
            continue
        seen.update(k)
        out.append(it)
    return out[:CFG["news_n"]]


# ---------------------------------------------------------------- digest
def build_digest(state, now):
    raises = (http_get(LLAMA + "/raises") or {}).get("raises", [])
    protocols = http_get(LLAMA + "/protocols") or []
    projects = group_projects(raises, now, CFG["lookback_days"])
    attach_protocols(projects, protocols)
    fees = load_fees()
    withtok = {k: p for k, p in projects.items() if (p.get("proto") or {}).get("gecko_id")}
    market = load_market([p["proto"]["gecko_id"] for p in withtok.values()])

    ranked = []
    for p in withtok.values():
        md = market.get(p["proto"]["gecko_id"])
        if not md:
            continue
        mcap = f(md.get("market_cap"))
        if not (CFG["min_mcap"] <= mcap <= CFG["max_mcap"]):
            continue
        if not p["t1"] and p["total"] < CFG["min_raised_no_t1"]:
            continue
        fees30 = fees.get(p["llama_id"], fees.get(p["name"].lower(), 0.0))
        sc, flags = score(p, md, fees30, now)
        ranked.append((sc, flags, p, md, fees30))
    ranked.sort(key=lambda x: -x[0])
    ranked = ranked[:CFG["top_n"]]

    today = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    lines = [f"🧭 Web3 radar {today}", "",
             "EARLY-STAGE VC-BACKED TOKENS (score = rule of thumb, not a prediction)"]
    if not ranked:
        lines.append("None passed the filters today (or market data was unavailable).")
    for n, (sc, flags, p, md, fees30) in enumerate(ranked, 1):
        tvl, ch30 = f(p["proto"].get("tvl")), f(p["proto"].get("change_1m"))
        mcap, fdv = f(md.get("market_cap")), f(md.get("fully_diluted_valuation"))
        sym = str(md.get("symbol") or "?").upper()
        price = f(md.get("current_price"))
        pick = state["picks"].get(p["key"])
        since = ""
        if pick and pick.get("price") and price:
            since = f" | {(price / pick['price'] - 1) * 100:+.0f}% since first listed {pick['date']}"
        elif not pick and price:
            state["picks"][p["key"]] = {"name": p["name"], "date": today, "price": price}
        lines.append(f"{n}) {p['name']} ({sym}) · score {sc}{since}")
        lines.append(f"   mcap {money(mcap)} · FDV {fdv / mcap:.1f}x · TVL {money(tvl)} ({ch30:+.0f}% 30d)"
                     if mcap and fdv else f"   mcap {money(mcap)} · TVL {money(tvl)} ({ch30:+.0f}% 30d)")
        inv = ", ".join(sorted(p["t1"])[:3]) or ", ".join(p["leads"][:3]) or "n/a"
        lines.append(f"   raised {money(p['total'] * 1e6)} ({', '.join(dict.fromkeys(p['rounds']))}) · {inv}")
        if flags:
            lines.append("   ⚠ " + "; ".join(flags))

    fresh = []
    for p in projects.values():
        if (now - p["last"]) / 86400 <= CFG["new_days"] and not (p.get("proto") or {}).get("gecko_id"):
            if p["t1"] or p["total"] >= CFG["min_raised_no_t1"] / 2:
                fresh.append(p)
    fresh.sort(key=lambda x: (-len(x["t1"]), -x["total"]))
    lines += ["", "NEWLY FUNDED, NO TOKEN FOUND YET (watch for points / airdrop programs)"]
    if not fresh:
        lines.append("Nothing new.")
    for p in fresh[:CFG["tokenless_n"]]:
        tag = "NEW · " if p["key"] not in state["seen_raises"] else ""
        state["seen_raises"].append(p["key"]) if p["key"] not in state["seen_raises"] else None
        inv = ", ".join(sorted(p["t1"])[:3]) or ", ".join(p["leads"][:3]) or "n/a"
        lines.append(f"• {tag}{p['name']} · {money(p['total'] * 1e6)} {p['rounds'][-1]} · {inv}"
                     + (f" · {p['sector']}" if p["sector"] else ""))

    news = fetch_news(state, now)
    lines += ["", "NEWS WORTH A LOOK"]
    if not news:
        lines.append("No new headlines matched.")
    for it in news:
        lines.append(f"• {it['title']} ({it['src']})\n  {it['link']}")
        state["seen_news"].append(it["link"])
    state["seen_news"] = state["seen_news"][-400:]
    state["seen_raises"] = state["seen_raises"][-400:]
    lines += ["", "Not financial advice. Before buying, check unlock schedules and tokenomics",
              "(CryptoRank / token.unlocks.app), the team, and whether you can exit your position."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true", help="send the digest to Telegram")
    ap.add_argument("--dry", action="store_true", help="print only: no Telegram, nothing saved")
    args = ap.parse_args()
    if not (args.digest or args.dry):
        ap.print_help()
        return 0
    state = load_state()
    text = build_digest(state, time.time())
    print(text)
    if args.digest:
        send_telegram(text)
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
