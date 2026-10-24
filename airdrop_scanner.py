"""
Airdrop spotter -> Telegram (daily).

1) POTENTIAL AIRDROPS: protocols on DefiLlama that hold real value (TVL) but have NO token yet.
   Ranked by size, growth, VC backing, age and category. Shows the official site + X account.
2) TOKEN LAUNCH WATCH: a protocol we saw without a token now has one -> claim/airdrop may be live.
3) EARLY TOKENS: the existing screener's picks (VC-backed, early-stage, passed all checks).
4) Airdrop / points / testnet headlines.

Needs screener.py in the same folder. Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
NOT financial advice. Airdrops are never guaranteed, farming costs time/gas, and fake claim sites are common.
"""
import argparse, json, math, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import screener as S

HERE = Path(__file__).parent
STATE = HERE / "state" / "airdrop_seen.json"
SAMPLE = None

MIN_TVL = 2_000_000
MAX_AGE_MONTHS = 36
MAX_TVL_DROP_7D = 40
LOOKUPS = 60               # how many top candidates get a funding lookup
TOP_N = 8
RESHOW_DAYS = 14           # don't repeat a candidate for this long
SKIP_CATS = {"cex", "chain", "cefi", "bridge aggregator"}
LIKELY_CATS = {"dexs", "dex aggregator", "lending", "derivatives", "perps", "liquid staking", "restaking",
               "liquid restaking", "yield", "yield aggregator", "bridge", "cdp", "rwa", "basis trading",
               "options", "prediction market", "launchpad", "farm"}
AIR_KEYS = re.compile(r"airdrop|points program|retroactive|testnet|incentivi[sz]ed|snapshot|claim|\bTGE\b|token launch", re.I)

def jget(url, key):
    if SAMPLE:
        p = Path(SAMPLE) / f"{key}.json"
        return json.loads(p.read_text()) if p.exists() else None
    raw = S.get(url, 40, 2)
    return json.loads(raw) if raw else None

def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def detail(p):
    """Funding rounds for one protocol from DefiLlama's protocol data."""
    if SAMPLE:
        d = jget("", f"raises_{p['slug']}")
        return d or []
    raw = S.get(f"https://api.llama.fi/protocol/{p['slug']}", 40, 1, quiet=True)
    time.sleep(0.2)
    try:
        return json.loads(raw).get("raises") or [] if raw else []
    except Exception:
        return []

def months_since(ts, now):
    return (now - ts) / 2_592_000 if ts else None

def air_score(p, vc, now):
    s, why, red = 0.0, [], []
    tvl = p.get("tvl") or 0
    s += max(0, min(25, 25 * math.log10(max(tvl, 1) / 1e6) / 2))
    why.append(f"TVL {S.money(tvl)}")
    g30, g7 = p.get("change_1m"), p.get("change_7d")
    if g30 is not None:
        s += max(0, min(15, g30 / 3))
        why.append(f"30d TVL {g30:+.0f}%")
    if g7 is not None and g7 < -25:
        s -= 10
        red.append(f"TVL down {g7:.0f}% in 7d")
    if vc:
        s += min(15, 6 * len(vc["tier1"])) + min(10, vc["total_m"] / 5)
        why.append(f"raised ${vc['total_m']:.1f}M" + (f" ({', '.join(vc['tier1'][:3])})" if vc["tier1"] else ""))
        if not vc["tier1"]:
            red.append("no tier-1 investor")
    else:
        red.append("no funding data found (could be unfunded or anonymous)")
    age = months_since(p.get("listedAt"), now)
    if age is None:
        s += 3
    else:
        s += 10 if age <= 12 else 7 if age <= 24 else 3 if age <= 36 else 0
        why.append(f"listed {age:.0f} months ago")
    if (p.get("category") or "").lower() in LIKELY_CATS:
        s += 5
    try:
        if int(p.get("audits") or 0) >= 1:
            s += 5
        else:
            red.append("no audit listed")
    except ValueError:
        pass
    return round(max(0, min(100, s)), 1), why, red

def find_candidates(protos, now):
    out = []
    for p in protos:
        if p.get("gecko_id") or p.get("symbol") not in (None, "", "-"):
            continue
        if (p.get("category") or "").lower() in SKIP_CATS or (p.get("tvl") or 0) < MIN_TVL:
            continue
        if (p.get("change_7d") or 0) < -MAX_TVL_DROP_7D:
            continue
        age = months_since(p.get("listedAt"), now)
        if age is not None and age > MAX_AGE_MONTHS:
            continue
        out.append(p)
    out.sort(key=lambda p: -(p.get("tvl") or 0))
    return out

def headlines(state, now):
    blobs = []
    if SAMPLE:
        f = Path(SAMPLE) / "news.xml"
        blobs = [f.read_bytes()] if f.exists() else []
    else:
        blobs = [b for b in (S.get(u, 20, 2, quiet=True) for u in S.NEWS_FEEDS) if b]
    new = []
    for b in blobs:
        try:
            root = ET.fromstring(b)
        except ET.ParseError:
            continue
        for it in root.iter("item"):
            t = (it.findtext("title") or "").strip()
            if t and AIR_KEYS.search(t) and t not in state["headlines"]:
                state["headlines"][t] = int(now)
                new.append((t, (it.findtext("link") or "").strip()))
    state["headlines"] = dict(sorted(state["headlines"].items(), key=lambda kv: -kv[1])[:400])
    return new[:6]

def early_tokens(protos):
    """Reuse the existing screener: VC-backed, early-stage, low-cap tokens that passed its checks."""
    ids = {p["gecko_id"] for p in protos if p.get("gecko_id") and 1e6 <= (p.get("tvl") or 0)}
    gk = S.gecko_markets(ids, SAMPLE)
    fees = (jget("https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true", "fees") or {}).get("protocols", [])
    ranked = S.screen(protos, [], fees, gk, detail=detail)
    return [t for t in ranked if t["vc"]][:5]

def x_link(p):
    t = (p.get("twitter") or "").lstrip("@")
    return f'<a href="https://x.com/{t}">X</a>' if t else None

def build(cands, launched, tokens, news):
    e = S.esc
    parts = ["<b>Airdrop spotter</b>\n<i>Leads, not promises. Airdrops are never guaranteed, farming costs time and gas, "
             "and fake claim sites are everywhere. Only use links from the project's official X account.</i>"]
    if launched:
        parts.append("\n<b>TOKEN LAUNCH WATCH (these had no token before)</b>")
        for p in launched:
            sym = p.get("symbol") or "?"
            parts.append(f"- <b>{e(p['name'])}</b> now has a token (${e(sym)}). Check their official X for an airdrop/claim "
                         f"announcement. Site: {e(p.get('url') or 'n/a')}")
    parts.append("\n<b>Potential airdrops (real usage, no token yet)</b>")
    if not cands:
        parts.append("No new candidates today (already shown within the last 14 days or none passed filters).")
    for i, (sc, p, why, red) in enumerate(cands, 1):
        site, x = p.get("url"), x_link(p)
        links = [f'<a href="{e(site)}">Website</a>' if site else None, x,
                 f'<a href="https://defillama.com/protocol/{e(p["slug"])}">DefiLlama</a>']
        parts.append(f"\n<b>{i}. {e(p['name'])}</b> - score <b>{sc}</b>/100\n"
                     f"{e(p.get('category') or '?')} | {e(', '.join((p.get('chains') or [])[:3]))}\n"
                     f"+ {e('; '.join(why))}\n"
                     + (f"! {e('; '.join(red[:3]))}\n" if red else "! no flags\n")
                     + " | ".join(l for l in links if l))
    if cands:
        parts.append("\n<i>How people qualify: use the product with a separate wallet the way a real user would, then watch the "
                     "project's official X/Discord/docs for a points or airdrop programme. Don't pay anyone for 'guaranteed' spots.</i>")
    if tokens:
        parts.append("\n<b>Early tokens that passed validation (VC-backed, early-stage, low mcap)</b>")
        for t in tokens:
            parts.append(f"- <b>{e(t['name'])} (${e(t['symbol'])})</b> score {t['score']} | mcap {S.money(t['mcap'])} | "
                         f"{e('; '.join(t['why'][:3]))}" + (f" | ! {e('; '.join(t['red'][:2]))}" if t["red"] else "") +
                         f' | <a href="https://defillama.com/protocol/{e(t["slug"])}">DefiLlama</a>')
    if news:
        parts.append("\n<b>Airdrop / points / testnet headlines</b>")
        parts += [f'- <a href="{e(l)}">{e(t)}</a>' for t, l in news]
    return "\n".join(parts)

def send(text, dry):
    if dry:
        print(re.sub(r"<[^>]+>", "", text))
        return
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    for c in S.chunks(text):
        data = urllib.parse.urlencode({"chat_id": chat, "text": c, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=30)
        time.sleep(1)

def main():
    global SAMPLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    SAMPLE = a.sample
    now = time.time()
    protos = jget("https://api.llama.fi/protocols", "protocols") or []
    if not protos:
        sys.exit("DefiLlama returned nothing; no message sent.")
    st = load(STATE, dict(shown={}, tokenless=[], headlines={}))
    by_slug = {p.get("slug"): p for p in protos}

    launched = [by_slug[s] for s in st["tokenless"] if s in by_slug and by_slug[s].get("gecko_id")]
    pool = find_candidates(protos, now)
    st["tokenless"] = [p["slug"] for p in pool[:400]]

    scored = []
    fresh = [p for p in pool if now - st["shown"].get(p["slug"], 0) > RESHOW_DAYS * 86400]
    for p in fresh[:LOOKUPS]:
        rr = detail(p)
        vc = S.vc_profile(rr) if rr else None
        sc, why, red = air_score(p, vc, now)
        scored.append((sc, p, why, red))
    scored.sort(key=lambda x: -x[0])
    cands = scored[:TOP_N]
    for sc, p, *_ in cands:
        st["shown"][p["slug"]] = int(now)
    st["shown"] = {k: v for k, v in st["shown"].items() if now - v < 60 * 86400}

    try:
        tokens = early_tokens(protos)
    except Exception as e:
        print("early_tokens failed:", e, file=sys.stderr)
        tokens = []
    send(build(cands, launched, tokens, headlines(st, now)), a.dry)
    if not a.dry or not SAMPLE:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(st, indent=1))
    print(f"airdrop: pool={len(pool)} lookups={min(len(fresh), LOOKUPS)} shown={len(cands)} launched={len(launched)}", file=sys.stderr)

if __name__ == "__main__":
    main()
