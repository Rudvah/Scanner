#!/usr/bin/env python3
"""
Sports betting scanner (alert-only: it never places bets).  Data: The Odds API (free key).

  --scan   finds (1) ARBITRAGE: back every outcome at the best price across bookmakers so the
               total return beats the total stake, and (2) VALUE BETS: a bookmaker's price is
               higher than the fair price implied by the sharp line (Pinnacle, margin removed;
               falls back to a consensus of 4+ books). Sends Telegram alerts.
  --fair   sends a "fair odds sheet" for upcoming matches: the fair price per outcome and the
               minimum odds worth taking (+5%). Use it to check a bookmaker the API doesn't
               cover (Bet9ja, SportyBet, ...): if your bookmaker offers at least that price, it
               is a value bet by the same logic.
  --dry    print only, nothing sent or saved (add after --scan / --fair).

Setup: ODDS_API_KEY (free key at the-odds-api.com), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Optional: BET_SPORTS (comma list of sport keys), BET_REGIONS (default "eu,uk"), BET_HOURS
(default 48), BANKROLL (default 100, in your own currency units).

CREDITS: each request costs about 1 credit per region per market. A run costs
(number of sports x number of regions). The free plan has a small monthly budget, so keep
sports few and runs rare. The script stops when the remaining credits get low.

HONEST LIMITS: arbitrage gives roughly 1-5% and bookmakers limit or close accounts that do it;
odds move fast, so a daily scan mostly finds stale prices; fair odds from a devigged line are
an estimate, not a certainty. Many edges here are errors or old prices: check the live odds
before betting and never stake money you cannot afford to lose.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from statistics import median

import requests

API = "https://api.the-odds-api.com/v4"
STATE_FILE = "bet_state.json"
SHARP = "pinnacle"
CFG = {
    "sports": [s.strip() for s in os.getenv(
        "BET_SPORTS", "soccer_epl,soccer_uefa_champs_league,soccer_spain_la_liga,basketball_nba").split(",") if s.strip()],
    "regions": os.getenv("BET_REGIONS", "eu,uk"),
    "hours": float(os.getenv("BET_HOURS", "48")),
    "bankroll": float(os.getenv("BANKROLL", "100")),
    "min_edge": 0.04,        # value bet: price beats fair price by 4% or more
    "min_edge_consensus": 0.06,
    "max_edge": 0.20,        # bigger "edges" are almost always errors or stale prices
    "min_arb": 0.005, "max_arb": 0.12,
    "kelly_frac": 0.25, "max_stake_pct": 0.05,
    "reserve": 20,           # stop when fewer credits than this remain
}


# ---------------------------------------------------------------- maths
def implied(prices):
    return {k: 1.0 / v for k, v in prices.items()}


def devig(prices):
    """Remove the bookmaker margin proportionally -> fair probabilities."""
    imp = implied(prices)
    tot = sum(imp.values())
    return {k: v / tot for k, v in imp.items()}


def find_arb(books, names):
    best = {}
    for bk, d in books.items():
        for n in names:
            if n in d["prices"] and (n not in best or d["prices"][n] > best[n][0]):
                best[n] = (d["prices"][n], bk, d["title"])
    if len(best) < len(names):
        return None
    total = sum(1.0 / p for p, _, _ in best.values())
    if total >= 1:
        return None
    return {"profit": 1.0 / total - 1.0, "legs": best, "total": total}


def fair_probs(books, names):
    """Return (probs, source). Sharp line first, otherwise a consensus of 4+ books."""
    sharp = books.get(SHARP)
    if sharp and all(n in sharp["prices"] for n in names):
        return devig({n: sharp["prices"][n] for n in names}), "Pinnacle"
    full = [devig({n: d["prices"][n] for n in names}) for d in books.values()
            if all(n in d["prices"] for n in names)]
    if len(full) < 4:
        return None, ""
    med = {n: median(p[n] for p in full) for n in names}
    tot = sum(med.values())
    return {n: v / tot for n, v in med.items()}, "consensus"


def kelly_stake(edge, price, bankroll):
    frac = min(CFG["kelly_frac"] * edge / (price - 1.0), CFG["max_stake_pct"])
    return max(frac, 0.0) * bankroll


# ---------------------------------------------------------------- data
def get_events(sport, key):
    r = requests.get(f"{API}/sports/{sport}/odds", timeout=30, params={
        "apiKey": key, "regions": CFG["regions"], "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"})
    remaining = r.headers.get("x-requests-remaining")
    if r.status_code == 401:
        raise SystemExit("The Odds API rejected the key (401). Check the ODDS_API_KEY secret.")
    if r.status_code in (404, 422):
        print(f"[info] {sport}: not available right now (out of season?)")
        return [], remaining
    if r.status_code == 429:
        print(f"[warn] {sport}: rate or credit limit reached (429)")
        return None, remaining
    r.raise_for_status()
    return r.json(), remaining


def parse_books(ev):
    books = {}
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            if m.get("key") != "h2h":
                continue
            prices = {o["name"]: float(o["price"]) for o in m.get("outcomes", [])
                      if o.get("price") and float(o["price"]) > 1.0}
            if len(prices) >= 2:
                books[b["key"]] = {"title": b.get("title", b["key"]), "prices": prices}
    return books


def kickoff(ev):
    t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
    return t, (t + timedelta(hours=1)).strftime("%a %d %b %H:%M WAT")


def upcoming(ev, now):
    t, _ = kickoff(ev)
    return now < t <= now + timedelta(hours=CFG["hours"])


# ---------------------------------------------------------------- analysis
def analyse(events, now):
    arbs, values, fair = [], [], []
    for ev in events:
        if not upcoming(ev, now):
            continue
        books = parse_books(ev)
        if len(books) < 2:
            continue
        names = sorted(max((d["prices"] for d in books.values()), key=len))
        title = f"{ev['home_team']} vs {ev['away_team']}"
        when = kickoff(ev)[1]
        arb = find_arb(books, names)
        if arb and CFG["min_arb"] <= arb["profit"] <= CFG["max_arb"]:
            arbs.append({"id": ev["id"], "event": title, "when": when, "sport": ev["sport_key"], **arb})
        probs, src = fair_probs(books, names)
        if not probs:
            continue
        fair.append({"event": title, "when": when, "sport": ev["sport_key"], "probs": probs, "src": src})
        need = CFG["min_edge"] if src == "Pinnacle" else CFG["min_edge_consensus"]
        for bk, d in books.items():
            if bk == SHARP:
                continue
            for n in names:
                if n not in d["prices"]:
                    continue
                edge = d["prices"][n] * probs[n] - 1.0
                if need <= edge <= CFG["max_edge"]:
                    values.append({"id": ev["id"], "event": title, "when": when, "pick": n, "book": d["title"],
                                   "price": d["prices"][n], "fair": 1.0 / probs[n], "edge": edge, "src": src,
                                   "stake": kelly_stake(edge, d["prices"][n], CFG["bankroll"])})
    arbs.sort(key=lambda a: -a["profit"])
    values.sort(key=lambda v: -v["edge"])
    return arbs, values, fair


# ---------------------------------------------------------------- messages
def fmt_arb(a):
    lines = [f"💰 ARBITRAGE +{a['profit'] * 100:.1f}% · {a['event']} · {a['when']}"]
    for n, (p, _, title) in a["legs"].items():
        stake = CFG["bankroll"] * (1 / p) / a["total"]
        lines.append(f"   {n}: {p:.2f} at {title} → stake {stake:.1f} of {CFG['bankroll']:.0f}")
    lines.append(f"   Guaranteed return {CFG['bankroll'] / a['total']:.1f} if every price still stands. "
                 "Check live odds first.")
    return "\n".join(lines)


def fmt_value(v):
    return (f"🎯 VALUE +{v['edge'] * 100:.1f}% · {v['event']} · {v['when']}\n"
            f"   {v['pick']} @ {v['price']:.2f} at {v['book']} (fair {v['fair']:.2f}, {v['src']})\n"
            f"   Suggested stake ≈ {v['stake']:.1f} of {CFG['bankroll']:.0f} (quarter-Kelly, capped at 5%)")


def fmt_fair(f):
    parts = []
    for n, p in f["probs"].items():
        fair_odds = 1.0 / p
        parts.append(f"{n} fair {fair_odds:.2f} → take ≥ {fair_odds * 1.05:.2f}")
    return f"⚽ {f['event']} · {f['when']} ({f['src']})\n   " + "\n   ".join(parts)


def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[info] Telegram not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    ok, chunk, parts = True, "", []
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
            ok = ok and r.status_code == 200
        except Exception as e:  # noqa: BLE001
            print(f"[warn] telegram failed ({e.__class__.__name__})")
            ok = False
    return ok


def load_state():
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {"seen": {}}
    except (FileNotFoundError, ValueError):
        return {"seen": {}}


# ---------------------------------------------------------------- run
def collect(key):
    events, remaining = [], None
    for sport in CFG["sports"]:
        if remaining is not None and float(remaining) < CFG["reserve"]:
            print(f"[warn] only {remaining} credits left, stopping to keep a reserve")
            break
        evs, rem = get_events(sport, key)
        remaining = rem if rem is not None else remaining
        if evs:
            events += evs
    return events, remaining


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--fair", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    if not (args.scan or args.fair):
        ap.print_help()
        return 0
    key = os.getenv("ODDS_API_KEY")
    if not key:
        print("ODDS_API_KEY is missing. Get a free key at the-odds-api.com and add it as a secret.")
        return 1
    now = datetime.now(timezone.utc)
    events, remaining = collect(key)
    arbs, values, fair = analyse(events, now)
    print(f"{len(events)} events fetched, credits left: {remaining}")

    if args.fair:
        text = "⚖️ Fair odds sheet (take a price only if it is at or above the 'take' number)\n\n" + \
               ("\n".join(fmt_fair(f) for f in fair[:25]) or "No matches with a sharp or consensus line.")
        text += "\n\nFair odds are estimates with the margin removed. Check team news before betting."
        print(text)
        if not args.dry:
            send_telegram(text)
        return 0

    state = load_state()
    cutoff = (now - timedelta(days=3)).timestamp()
    state["seen"] = {k: t for k, t in state["seen"].items() if t > cutoff}
    msgs = []
    for a in arbs:
        k = f"arb|{a['id']}|" + "|".join(f"{n}:{p:.2f}" for n, (p, _, _) in sorted(a["legs"].items()))
        if k not in state["seen"]:
            state["seen"][k] = now.timestamp()
            msgs.append(fmt_arb(a))
    for v in values[:10]:
        k = f"val|{v['id']}|{v['pick']}|{v['book']}|{v['price']:.2f}"
        if k not in state["seen"]:
            state["seen"][k] = now.timestamp()
            msgs.append(fmt_value(v))
    text = "\n\n".join(msgs) if msgs else "No new arbitrage or value bets this scan."
    text += f"\n\nCredits left: {remaining}. Odds move fast: verify before betting. Bookmakers may limit winners."
    print(text)
    if not args.dry:
        if msgs:
            send_telegram(text)
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
