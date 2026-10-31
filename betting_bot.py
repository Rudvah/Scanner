"""Sports betting scanner (alert-only: it never places bets).  Data: The Odds API (free key).
  --scan   SURE BETS (arbitrage) and VALUE BETS (a price above the fair price from Pinnacle, or a consensus of 4+ books).
  --fair   plain-language fair odds sheet: result, goals over/under, double chance, draw-no-bet, with the minimum odds worth taking.
  --dry    print only, nothing sent or saved.
Setup: ODDS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
Optional: BET_SPORTS, BET_REGIONS (scan, default eu,uk), BET_FAIR_REGIONS (fair sheet, default eu),
BET_MARKETS (default h2h,totals), BET_HOURS (default 48), BANKROLL (default 100).
CREDITS: a run costs about sports x regions x markets. Scan = 4x2x2 = 16, fair sheet = 4x1x2 = 8."""
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
    "fair_regions": os.getenv("BET_FAIR_REGIONS", "eu"),
    "markets": os.getenv("BET_MARKETS", "h2h,totals"),
    "hours": float(os.getenv("BET_HOURS", "48")),
    "bankroll": float(os.getenv("BANKROLL", "100")),
    "min_edge": 0.04, "min_edge_consensus": 0.06, "max_edge": 0.20,
    "min_arb": 0.005, "max_arb": 0.12,
    "kelly_frac": 0.25, "max_stake_pct": 0.05,
    "take_margin": 1.05,
    "sheet_events": 12, "reserve": 20,
}


def devig(prices):
    imp = {k: 1.0 / v for k, v in prices.items()}
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


def get_events(sport, key, regions):
    r = requests.get(f"{API}/sports/{sport}/odds", timeout=30, params={
        "apiKey": key, "regions": regions, "markets": CFG["markets"], "oddsFormat": "decimal",
        "dateFormat": "iso"})
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


def parse_instances(ev):
    inst = {}
    for b in ev.get("bookmakers", []):
        for m in b.get("markets", []):
            mk = m.get("key")
            if mk not in ("h2h", "totals"):
                continue
            by_point = {}
            for o in m.get("outcomes", []):
                try:
                    price = float(o.get("price"))
                except (TypeError, ValueError):
                    continue
                if price > 1.0:
                    by_point.setdefault(o.get("point") if mk == "totals" else None, {})[o["name"]] = price
            for pt, prices in by_point.items():
                if len(prices) >= 2:
                    inst.setdefault((mk, pt), {})[b["key"]] = {"title": b.get("title", b["key"]), "prices": prices}
    return inst


def kickoff(ev):
    t = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
    return t, (t + timedelta(hours=1)).strftime("%a %d %b, %H:%M WAT")


def upcoming(ev, now):
    t, _ = kickoff(ev)
    return now < t <= now + timedelta(hours=CFG["hours"])


def is_soccer(ev):
    return ev["sport_key"].startswith("soccer")


def icon(ev):
    return "⚽" if is_soccer(ev) else "🏀" if ev["sport_key"].startswith("basketball") else "🎯"


def label(ev, market, point, outcome):
    if market == "h2h":
        return "Draw" if outcome == "Draw" else f"{outcome} to win"
    return f"{outcome} {point:g} {'goals' if is_soccer(ev) else 'points'}"


def analyse(events, now):
    arbs, values, sheet = [], [], []
    for ev in events:
        if not upcoming(ev, now):
            continue
        title = f"{ev['home_team']} vs {ev['away_team']}"
        when = kickoff(ev)[1]
        rec = {"ev": ev, "event": title, "when": when, "kick": kickoff(ev)[0], "h2h": None, "totals": []}
        for (mk, pt), books in parse_instances(ev).items():
            if len(books) < 2 and SHARP not in books:
                continue
            names = sorted(max((d["prices"] for d in books.values()), key=len))
            arb = find_arb(books, names) if len(books) >= 2 else None
            if arb and CFG["min_arb"] <= arb["profit"] <= CFG["max_arb"]:
                legs = {label(ev, mk, pt, n): v for n, v in arb["legs"].items()}
                arbs.append({"id": ev["id"], "key": f"{mk}|{pt}", "event": title, "when": when, "icon": icon(ev),
                             "profit": arb["profit"], "legs": legs, "total": arb["total"]})
            probs, src = fair_probs(books, names)
            if not probs:
                continue
            if mk == "h2h":
                rec["h2h"] = (probs, src)
            else:
                rec["totals"].append((pt, probs, src))
            need = CFG["min_edge"] if src == "Pinnacle" else CFG["min_edge_consensus"]
            for bk, d in books.items():
                if bk == SHARP:
                    continue
                for n in names:
                    if n not in d["prices"]:
                        continue
                    edge = d["prices"][n] * probs[n] - 1.0
                    if need <= edge <= CFG["max_edge"]:
                        values.append({"id": ev["id"], "event": title, "when": when, "icon": icon(ev),
                                       "pick": label(ev, mk, pt, n), "book": d["title"], "price": d["prices"][n],
                                       "fair": 1.0 / probs[n], "edge": edge, "src": src,
                                       "stake": kelly_stake(edge, d["prices"][n], CFG["bankroll"])})
        if rec["h2h"] or rec["totals"]:
            sheet.append(rec)
    arbs.sort(key=lambda a: -a["profit"])
    values.sort(key=lambda v: -v["edge"])
    sheet.sort(key=lambda r: r["kick"])
    return arbs, values, sheet
  

def row(text, p):
    fair = 1.0 / p
    return f"  • {text}: fair {fair:.2f} ({p * 100:.0f}% chance) → bet only at {fair * CFG['take_margin']:.2f} or higher"


def fmt_fair(rec):
    ev = rec["ev"]
    home, away = ev["home_team"], ev["away_team"]
    lines = [f"{icon(ev)} {rec['event']}", f"🕒 {rec['when']}"]
    if rec["h2h"]:
        probs, src = rec["h2h"]
        lines.append("RESULT:" if is_soccer(ev) else "WINNER:")
        for n in (home, "Draw", away):
            if n in probs:
                lines.append(row(label(ev, "h2h", None, n), probs[n]))
        if is_soccer(ev) and "Draw" in probs and home in probs and away in probs:
            ph, pd_, pa = probs[home], probs["Draw"], probs[away]
            lines.append("DOUBLE CHANCE (either result wins the bet):")
            lines.append(row(f"{home} or Draw", ph + pd_))
            lines.append(row(f"{away} or Draw", pa + pd_))
            lines.append(row(f"{home} or {away} (no draw)", ph + pa))
            lines.append("DRAW NO BET (stake returned if it is a draw):")
            lines.append(row(f"{home}", ph / (ph + pa)))
            lines.append(row(f"{away}", pa / (ph + pa)))
    for pt, probs, src in sorted(rec["totals"], key=lambda t: t[0] if t[0] is not None else 0)[:2]:
        lines.append(f"TOTAL {'GOALS' if is_soccer(ev) else 'POINTS'} (line {pt:g}):")
        for n in ("Over", "Under"):
            if n in probs:
                lines.append(row(label(ev, "totals", pt, n), probs[n]))
    return "\n".join(lines)


GUIDE = ("HOW TO READ THIS\n"
         "• Fair odds = the true price once the bookmaker's built-in margin is removed.\n"
         "• \"Bet only at X or higher\" = take the bet only if your bookmaker pays X or more\n"
         "  (about 5% better than fair). If their price is lower, skip it.\n"
         "• Chance % is the probability the sharp market gives that outcome.\n"
         "• Check team news and live odds first. This is a guide, not a guarantee.")


def fmt_arb(a):
    lines = [f"💰 SURE BET (arbitrage) +{a['profit'] * 100:.1f}%", f"{a['icon']} {a['event']} · {a['when']}",
             f"Place ALL {len(a['legs'])} bets:"]
    for i, (lab, (p, _, title)) in enumerate(a["legs"].items(), 1):
        stake = CFG["bankroll"] * (1 / p) / a["total"]
        lines.append(f"  {i}) {lab} @ {p:.2f} at {title} → stake {stake:.1f}")
    lines.append(f"Total stake {CFG['bankroll']:.0f} returns about {CFG['bankroll'] / a['total']:.1f} "
                 "whichever outcome happens, if all prices still stand. Check live odds first.")
    return "\n".join(lines)


def fmt_value(v):
    return (f"🎯 VALUE BET +{v['edge'] * 100:.1f}%\n{v['icon']} {v['event']} · {v['when']}\n"
            f"Bet: {v['pick']}\nOdds: {v['price']:.2f} at {v['book']} (fair price {v['fair']:.2f}, from {v['src']})\n"
            f"Stake idea: {v['stake']:.1f} of {CFG['bankroll']:.0f} (small, cautious sizing)")


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


def collect(key, regions):
    events, remaining = [], None
    for sport in CFG["sports"]:
        if remaining is not None and float(remaining) < CFG["reserve"]:
            print(f"[warn] only {remaining} credits left, stopping to keep a reserve")
            break
        evs, rem = get_events(sport, key, regions)
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
    events, remaining = collect(key, CFG["fair_regions"] if args.fair else CFG["regions"])
    arbs, values, sheet = analyse(events, now)
    print(f"{len(events)} events fetched, credits left: {remaining}")

    if args.fair:
        body = "\n\n".join(fmt_fair(r) for r in sheet[:CFG["sheet_events"]]) or \
            "No upcoming matches with a sharp or consensus line right now."
        text = f"⚖️ FAIR ODDS SHEET (next {CFG['hours']:.0f}h, soonest first)\n\n{GUIDE}\n\n{body}"
        print(text)
        if not args.dry:
            send_telegram(text)
        return 0

    state = load_state()
    cutoff = (now - timedelta(days=3)).timestamp()
    state["seen"] = {k: t for k, t in state["seen"].items() if t > cutoff}
    msgs = []
    for a in arbs:
        k = f"arb|{a['id']}|{a['key']}|" + "|".join(f"{n}:{p:.2f}" for n, (p, _, _) in sorted(a["legs"].items()))
        if k not in state["seen"]:
            state["seen"][k] = now.timestamp()
            msgs.append(fmt_arb(a))
    for v in values[:10]:
        k = f"val|{v['id']}|{v['pick']}|{v['book']}|{v['price']:.2f}"
        if k not in state["seen"]:
            state["seen"][k] = now.timestamp()
            msgs.append(fmt_value(v))
    text = "\n\n".join(msgs) if msgs else "No new sure bets or value bets this scan."
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
  
