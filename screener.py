"""
Early-stage token screener + Web3 digest -> Telegram.
Sources: DefiLlama (protocols, raises, fees), CoinGecko (markets), RSS news.
Commands:
  python screener.py run [--sample DIR] [--dry]
  python screener.py track
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
NOT financial advice. A high score means "worth researching", never "buy".
Unlock schedules are not in free APIs: check tokenomist.ai manually.
"""
import argparse, json, os, re, sys, time, statistics, urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).parent
STATE = HERE / "state" / "picks.json"

MCAP_MIN, MCAP_MAX = 3_000_000, 250_000_000
MIN_TVL = 1_000_000
MAX_AGE_DAYS = 900
TOP_N = 8
TIER1 = {
    "a16z", "andreessen horowitz", "paradigm", "coinbase ventures", "binance labs", "polychain",
    "multicoin", "pantera", "sequoia", "dragonfly", "framework", "hashkey", "okx ventures",
    "jump crypto", "galaxy", "delphi", "variant", "electric capital", "placeholder", "lightspeed",
    "founders fund", "ribbit", "robot ventures", "animoca", "bain capital crypto", "yzi labs",
    "circle ventures", "ark invest", "blockchain capital", "spartan", "mechanism", "hack vc",
}
NEWS_FEEDS = [
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
]
NEWS_KEYS = re.compile(r"airdrop|points program|testnet|incentivi[sz]ed|raises \$|funding round|seed round|"
                       r"series [ab]\b|token launch|tge\b|listing", re.I)

STATS = {}

def get(url, timeout=30, retries=3):
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "web3-screener/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            if getattr(e, "code", None) == 429:
                time.sleep(25)
            if k == retries - 1:
                print(f"[warn] {url}: {e}", file=sys.stderr)
                STATS.setdefault("errors", []).append(f"{url.split('?')[0].split('//')[-1][:45]}: {str(e)[:40]}")
                return None
            time.sleep(2 * (k + 1))

def get_json(url, sample=None, name=None):
    if sample:
        p = Path(sample) / f"{name}.json"
        return json.loads(p.read_text()) if p.exists() else None
    raw = get(url)
    return json.loads(raw) if raw else None

def load_all(sample=None):
    protos = get_json("https://api.llama.fi/protocols", sample, "protocols") or []
    raises = (get_json("https://api.llama.fi/raises", sample, "raises") or {}).get("raises", [])
    fees = (get_json("https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true",
                     sample, "fees") or {}).get("protocols", [])
    return protos, raises, fees

def gecko_markets(ids, sample=None):
    if sample:
        return {m["id"]: m for m in (get_json("", sample, "markets") or [])}
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 100):
        chunk = ",".join(ids[i:i + 100])
        url = ("https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&per_page=250"
               "&price_change_percentage=7d,30d&ids=" + urllib.parse.quote(chunk))
        for m in get_json(url) or []:
            out[m["id"]] = m
        time.sleep(2.5)
    return out

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def investors_of(r):
    inv = list(r.get("leadInvestors") or []) + list(r.get("otherInvestors") or [])
    return [i for i in inv if i]

def vc_profile(raise_rows):
    total = sum((r.get("amount") or 0) for r in raise_rows)
    names = {i for r in raise_rows for i in investors_of(r)}
    t1 = sorted(i for i in names if any(t in i.lower() for t in TIER1))
    leads = {i for r in raise_rows for i in (r.get("leadInvestors") or [])}
    last = max((r.get("date") or 0 for r in raise_rows), default=0)
    return dict(total_m=total, n_inv=len(names), tier1=t1, n_leads=len(leads), last_ts=last,
                rounds=len(raise_rows))

def score(p, gk, vc, fee30):
    s, why, red = 0, [], []
    mcap = gk.get("market_cap") or p.get("mcap") or 0
    fdv = gk.get("fully_diluted_valuation") or 0
    tvl = p.get("tvl") or 0

    if vc:
        pts = min(15, len(vc["tier1"]) * 5) + min(8, vc["total_m"] / 5) + (4 if vc["n_leads"] >= 2 else 0)
        pts = min(30, pts + (3 if vc["rounds"] >= 2 else 0))
        s += pts
        if vc["tier1"]:
            why.append(f"VC: {', '.join(vc['tier1'][:3])}" + (f" +{len(vc['tier1'])-3}" if len(vc["tier1"]) > 3 else ""))
        why.append(f"raised ${vc['total_m']:.1f}M over {vc['rounds']} round(s)")
        if not vc["tier1"]:
            red.append("no tier-1 investor")
        if vc["last_ts"] and datetime.fromtimestamp(vc["last_ts"], timezone.utc) < datetime.now(timezone.utc) - timedelta(days=900):
            red.append("last raise >2.5y ago")
    else:
        red.append("NO VC/raise data found")

    if fdv and mcap:
        ratio = mcap / fdv
        s += 20 * min(1, ratio / 0.6)
        why.append(f"float {ratio:.0%} of FDV")
        if ratio < 0.25:
            red.append(f"only {ratio:.0%} circulating -> heavy unlock overhang")
        if fdv / max(mcap, 1) > 5:
            red.append("FDV >5x mcap")
    else:
        red.append("no FDV data")

    if mcap and tvl:
        r = tvl / mcap
        s += min(15, 15 * r / 1.0)
        why.append(f"TVL ${tvl/1e6:.1f}M (TVL/mcap {r:.2f})")
        if r < 0.1:
            red.append("TVL tiny vs market cap")

    if fee30:
        s += min(15, 15 * (fee30 * 12 / max(mcap, 1)) / 0.2)
        why.append(f"30d fees ${fee30/1e3:.0f}k")
    else:
        red.append("no fee/revenue data")

    g7, g30 = p.get("change_7d"), p.get("change_1m")
    if g30 is not None:
        s += max(0, min(10, g30 / 3))
        why.append(f"TVL 30d {g30:+.0f}%")
        if g30 < -30:
            red.append(f"TVL down {g30:.0f}% in 30d")
    if g7 is not None:
        s += max(0, min(5, g7 / 4))
        if g7 < -25:
            red.append(f"TVL down {g7:.0f}% in 7d")

    pc30 = gk.get("price_change_percentage_30d_in_currency")
    if pc30 is not None:
        if pc30 > 150:
            red.append(f"price already +{pc30:.0f}% in 30d (chasing risk)")
        else:
            s += 5 if pc30 > -20 else 0

    if mcap < 5_000_000:
        red.append("very low mcap -> illiquid/manipulable")
    if gk.get("total_volume") and mcap and gk["total_volume"] / mcap < 0.01:
        red.append("volume <1% of mcap -> hard to exit")
    return round(min(100, s), 1), why, red
def screen(protos, raises, fees, gk, now=None):
    now = now or time.time()
    by_raise = {}
    for r in raises:
        key = norm(r.get("name"))
        by_raise.setdefault(key, []).append(r)
        if r.get("defillamaId"):
            by_raise.setdefault("id:" + str(r["defillamaId"]), []).append(r)
    fee_by = {}
    for f in fees:
        for k in (norm(f.get("name")), norm(f.get("slug")), "id:" + str(f.get("defillamaId"))):
            fee_by[k] = f.get("total30d") or 0
    out = []
    STATS["gecko_matched"] = STATS["in_window"] = 0
    for p in protos:
        gid = p.get("gecko_id")
        if not gid or gid not in gk:
            continue
        STATS["gecko_matched"] += 1
        m = gk[gid]
        mcap = m.get("market_cap") or 0
        if not (MCAP_MIN <= mcap <= MCAP_MAX) or (p.get("tvl") or 0) < MIN_TVL:
            continue
        STATS["in_window"] += 1
        listed = p.get("listedAt")
        if listed and (now - listed) / 86400 > MAX_AGE_DAYS:
            continue
        if (p.get("category") or "").lower() in {"cex", "chain"}:
            continue
        rr = by_raise.get("id:" + str(p.get("id"))) or by_raise.get(norm(p.get("name"))) or []
        vc = vc_profile(rr) if rr else None
        fee30 = fee_by.get(norm(p.get("name"))) or fee_by.get(norm(p.get("slug"))) or fee_by.get("id:" + str(p.get("id"))) or 0
        sc, why, red = score(p, m, vc, fee30)
        out.append(dict(id=gid, name=p["name"], symbol=(p.get("symbol") or "").upper(), cat=p.get("category"),
                        chains=(p.get("chains") or [])[:3], mcap=mcap, fdv=m.get("fully_diluted_valuation"),
                        price=m.get("current_price"), score=sc, why=why, red=red, url=p.get("url"),
                        slug=p.get("slug"), vc=bool(vc)))
    out.sort(key=lambda x: (-x["score"]))
    return out

def early_projects(raises, tokens_known, days=14, now=None):
    now = now or time.time()
    res = []
    for r in raises:
        d = r.get("date") or 0
        if now - d > days * 86400:
            continue
        if norm(r.get("name")) in tokens_known:
            continue
        res.append(r)
    res.sort(key=lambda r: -(r.get("amount") or 0))
    return res[:8]

def news(sample=None, limit=8):
    items = []
    if sample:
        p = Path(sample) / "news.xml"
        blobs = [p.read_bytes()] if p.exists() else []
    else:
        blobs = [b for b in (get(u, 20, 2) for u in NEWS_FEEDS) if b]
    seen = set()
    for b in blobs:
        try:
            root = ET.fromstring(b)
        except ET.ParseError:
            continue
        for it in root.iter("item"):
            t = (it.findtext("title") or "").strip()
            l = (it.findtext("link") or "").strip()
            if t and NEWS_KEYS.search(t) and t not in seen:
                seen.add(t)
                items.append((t, l))
    return items[:limit]

def esc(s):
    return (str(s) or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def money(x):
    if not x:
        return "n/a"
    return f"${x/1e9:.2f}B" if x >= 1e9 else f"${x/1e6:.1f}M" if x >= 1e6 else f"${x/1e3:.0f}k"

def build_digest(top, early, headlines):
    d = datetime.now(timezone.utc).strftime("%d %b %Y")
    parts = [f"<b>Web3 early-stage digest - {d}</b>\n<i>Research leads, not advice. Check unlock schedules + liquidity before acting.</i>"]
    parts.append("\n<b>1) Top screened tokens (VC-backed, low mcap)</b>")
    if not top:
        parts.append("Nothing passed the filters today.")
    for i, t in enumerate(top, 1):
        link = f"https://defillama.com/protocol/{t['slug']}"
        parts.append(
            f"\n<b>{i}. {esc(t['name'])} (${esc(t['symbol'])})</b> - score <b>{t['score']}</b>/100\n"
            f"{esc(t['cat'] or '?')} | {esc(', '.join(t['chains']))}\n"
            f"mcap {money(t['mcap'])} | FDV {money(t['fdv'])}\n"
            f"+ {esc('; '.join(t['why'][:4]))}\n"
            + (f"! {esc('; '.join(t['red'][:4]))}\n" if t["red"] else "! none flagged\n")
            + f"<a href=\"{link}\">DefiLlama</a>")
    parts.append("\n<b>2) Newly funded, no token yet (airdrop/points watch)</b>")
    if not early:
        parts.append("None in the last 14 days.")
    for r in early:
        inv = ", ".join((r.get("leadInvestors") or [])[:3]) or "undisclosed"
        parts.append(f"- <b>{esc(r.get('name'))}</b> {esc(r.get('round') or '')} "
                     f"{money((r.get('amount') or 0) * 1e6)} | {esc(r.get('category') or r.get('sector') or '')} | lead: {esc(inv)}")
    parts.append("\n<b>3) Headlines: airdrops, points, testnets, raises</b>")
    for t, l in headlines:
        parts.append(f"- <a href=\"{esc(l)}\">{esc(t)}</a>")
    if not headlines:
        parts.append("Nothing matched today.")
    st = STATS
    parts.append(f"\n<i>Diagnostics: protocols {st.get('protocols')} | raises {st.get('raises')} | fees {st.get('fees')} | "
                 f"CoinGecko markets {st.get('markets')} | matched {st.get('gecko_matched')} | in mcap window {st.get('in_window')} | "
                 f"with VC data {st.get('with_vc')}</i>")
    for e in st.get("errors", [])[:4]:
        parts.append(f"<i>! source error: {esc(e)}</i>")
    return "\n".join(parts)

def chunks(text, n=3900):
    cur = ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > n:
            yield cur
            cur = ""
        cur += block + "\n\n"
    if cur.strip():
        yield cur

def send(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    for c in chunks(text):
        data = urllib.parse.urlencode({"chat_id": chat, "text": c, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=30)
        time.sleep(1)

def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else []

def save_state(s):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=1))

def log_run(top, universe):
    s = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if any(r["date"] == today for r in s):
        return
    s.append(dict(date=today, picks={t["id"]: t["price"] for t in top if t["price"]},
                  universe={t["id"]: t["price"] for t in universe if t["price"]}))
    save_state(s[-120:])

def track(min_days=14, sample=None):
    s = load_state()
    ids = {i for r in s for i in r["universe"]}
    now = gecko_markets(ids, sample) if ids else {}
    rows = []
    for r in s:
        age = (datetime.now(timezone.utc) - datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)).days
        if age < min_days:
            continue
        def ret(book):
            v = [(now[i]["current_price"] / p - 1) for i, p in book.items() if i in now and now[i].get("current_price") and p]
            return statistics.mean(v) if v else None
        a, b = ret(r["picks"]), ret(r["universe"])
        if a is not None and b is not None:
            rows.append((r["date"], age, a, b))
    if not rows:
        return f"Tracker: no runs older than {min_days} days yet."
    lines = ["<b>Outcome tracker</b> (picks vs. all screened tokens, same dates)"]
    for d, age, a, b in rows[-10:]:
        lines.append(f"{d} ({age}d): picks {a:+.0%} vs universe {b:+.0%}  ->  {'beat' if a > b else 'lagged'}")
    beat = sum(1 for *_, a, b in rows if a > b)
    lines.append(f"Beat the universe in {beat}/{len(rows)} runs. Small sample = not proof.")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "track"])
    ap.add_argument("--sample")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    if a.cmd == "track":
        msg = track(sample=a.sample)
        print(msg) if a.dry else send(msg)
        return

    protos, raises, fees = load_all(a.sample)
    if not protos:
        sys.exit("DefiLlama returned nothing; aborting (no digest sent).")
    ids = {p["gecko_id"] for p in protos if p.get("gecko_id") and 1e6 <= (p.get("tvl") or 0)}
    gk = gecko_markets(ids, a.sample)
    STATS.update(protocols=len(protos), raises=len(raises), fees=len(fees), markets=len(gk))
    ranked = screen(protos, raises, fees, gk)
    STATS["with_vc"] = sum(1 for t in ranked if t["vc"])
    top = [t for t in ranked if t["vc"]][:TOP_N]
    tokens_known = {norm(p.get("name")) for p in protos if p.get("gecko_id")}
    msg = build_digest(top, early_projects(raises, tokens_known), news(a.sample))
    log_run(top, ranked)
    if a.dry:
        print(re.sub(r"<[^>]+>", "", msg.replace("</a>", "")))
    else:
        send(msg)
        if datetime.now(timezone.utc).weekday() == 0:
            send(track())

if __name__ == "__main__":
    main()
