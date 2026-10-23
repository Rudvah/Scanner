"""
Meme coin early scanner + smart-wallet tracker -> Telegram.

Discovery : GeckoTerminal new pools + DexScreener latest profiles
Market    : DexScreener (liquidity, txns, price change, socials, boosts)
Safety    : RugCheck (Solana), GoPlus (Base/Ethereum/BSC)
Wallets   : Helius (Solana) - optional, needs HELIUS_API_KEY and a wallets.txt file

Commands:
  python meme_scanner.py run [--sample DIR] [--dry]

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, HELIUS_API_KEY (optional)
NOT financial advice. Most new meme coins go to zero. Passing the checks means
"not an obvious scam", never "safe" and never "will pump".
"""
import argparse, json, os, re, statistics, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ST = HERE / "state"
F_SEEN, F_ALERTS, F_WALLET = ST / "meme_seen.json", ST / "meme_alerts.json", ST / "meme_wallets.json"

# ---- tunables ---------------------------------------------------------------------------
CHAINS = ["solana", "base"]          # add "ethereum", "bsc" if you want
GT_NET = {"solana": "solana", "base": "base", "ethereum": "eth", "bsc": "bsc"}
GOPLUS_ID = {"ethereum": "1", "bsc": "56", "base": "8453"}
MIN_AGE_MIN, MAX_AGE_MIN = 5, 720    # pool age window (minutes)
MIN_LIQ = 8_000                      # USD liquidity
MAX_MCAP = 3_000_000                 # skip if already big
MAX_M5_PUMP = 60                     # skip if +% in last 5 min (spike = chasing)
MAX_H1_PUMP = 150                    # skip if +% in last hour (already pumped)
MIN_H1_CHANGE = -35                  # skip if dumping
MIN_H1_TXNS = 40
MIN_BUY_RATIO, MAX_BUY_RATIO = 0.50, 0.85
MAX_VOL_LIQ_H1 = 20                  # hourly volume / liquidity above this smells like wash trading
MIN_SCORE = 60
MAX_ALERTS_PER_RUN = 5
REQUIRE_SECURITY = True              # no verified security data -> no alert
WALLET_MAX_AGE_MIN = 45              # only alert on wallet buys this fresh
IGNORE_MINTS = {"So11111111111111111111111111111111111111112",   # wSOL
                "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
BURN = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}

SAMPLE = None
STATS = {}

# ---- http -------------------------------------------------------------------------------
def jget(url, key, timeout=25):
    if SAMPLE:
        p = Path(SAMPLE) / f"{key}.json"
        return json.loads(p.read_text()) if p.exists() else None
    for k in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "meme-scanner/1.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            if getattr(e, "code", None) == 429:
                time.sleep(15)
            elif k == 1 or getattr(e, "code", None) in (400, 401, 402, 403, 404):
                STATS.setdefault("errors", []).append(f"{url.split('?')[0].split('//')[-1][:50]}: {str(e)[:30]}")
                return None
            time.sleep(2)

def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def save(path, obj):
    ST.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=1))

# ---- discovery + market data --------------------------------------------------------------
def discover():
    addrs = set()
    for ch in CHAINS:
        d = jget(f"https://api.geckoterminal.com/api/v2/networks/{GT_NET[ch]}/new_pools?page=1", f"gt_{ch}")
        for it in (d or {}).get("data", []):
            bid = ((((it.get("relationships") or {}).get("base_token") or {}).get("data")) or {}).get("id", "")
            if "_" in bid:
                a = bid.split("_", 1)[1]
                addrs.add((ch, a if ch == "solana" else a.lower()))
        time.sleep(0.5)
    for it in jget("https://api.dexscreener.com/token-profiles/latest/v1", "profiles") or []:
        if it.get("chainId") in CHAINS and it.get("tokenAddress"):
            a = it["tokenAddress"]
            addrs.add((it["chainId"], a if it["chainId"] == "solana" else a.lower()))
    return addrs

def liq_of(p):
    return ((p.get("liquidity") or {}).get("usd")) or 0

def dex_pairs(addrs):
    wanted = {a.lower() for _, a in addrs}
    lst = sorted({a for _, a in addrs})
    out = {}
    for i in range(0, len(lst), 30):
        d = jget("https://api.dexscreener.com/latest/dex/tokens/" + ",".join(lst[i:i + 30]), f"pairs_{i // 30}")
        for p in (d or {}).get("pairs") or []:
            b = (p.get("baseToken") or {}).get("address", "")
            if p.get("chainId") not in CHAINS or b.lower() not in wanted:
                continue
            key = (p["chainId"], b)
            if key not in out or liq_of(p) > liq_of(out[key]):
                out[key] = p
        time.sleep(0.4)
    return out

def prefilter(p, now):
    """Return a reject reason, or None if the pair is a candidate."""
    created = p.get("pairCreatedAt")
    if not created:
        return "no_age"
    age = (now * 1000 - created) / 60000
    if not (MIN_AGE_MIN <= age <= MAX_AGE_MIN):
        return "age"
    if liq_of(p) < MIN_LIQ:
        return "liquidity"
    mcap = p.get("marketCap") or p.get("fdv") or 0
    if mcap > MAX_MCAP:
        return "mcap_big"
    pc = p.get("priceChange") or {}
    if (pc.get("m5") or 0) > MAX_M5_PUMP or (pc.get("h1") or 0) > MAX_H1_PUMP:
        return "already_pumped"
    if (pc.get("h1") or 0) < MIN_H1_CHANGE:
        return "dumping"
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    if buys + sells < MIN_H1_TXNS:
        return "low_activity"
    ratio = buys / max(buys + sells, 1)
    if not (MIN_BUY_RATIO <= ratio <= MAX_BUY_RATIO):
        return "buy_ratio"
    if ((p.get("volume") or {}).get("h1") or 0) / max(liq_of(p), 1) > MAX_VOL_LIQ_H1:
        return "wash_suspect"
    return None

# ---- safety ---------------------------------------------------------------------------------
def sec_solana(addr):
    d = jget(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report/summary", f"sec_{addr}")
    if not d:
        return dict(ok=None, fails=[], warns=[], facts=[], lp_locked=None)
    risks = d.get("risks") or []
    fails = [r.get("name", "risk") for r in risks if str(r.get("level")).lower() == "danger"]
    warns = [r.get("name", "risk") for r in risks if str(r.get("level")).lower() == "warn"]
    lp = d.get("lpLockedPct")
    facts = [f"LP locked/burned {lp:.0f}%"] if isinstance(lp, (int, float)) else []
    return dict(ok=not fails, fails=fails, warns=warns, facts=facts,
                lp_locked=(lp / 100 if isinstance(lp, (int, float)) else None))

def sec_evm(chain, addr):
    d = jget(f"https://api.gopluslabs.io/api/v1/token_security/{GOPLUS_ID[chain]}?contract_addresses={addr}", f"sec_{addr}")
    r = ((d or {}).get("result") or {}).get(addr.lower())
    if not r:
        return dict(ok=None, fails=[], warns=[], facts=[], lp_locked=None)
    f = lambda k: str(r.get(k, "0")) == "1"
    num = lambda k: float(r.get(k) or 0)
    fails, warns, facts = [], [], []
    if f("is_honeypot"):
        fails.append("HONEYPOT")
    if f("cannot_sell_all"):
        fails.append("cannot sell all")
    st_, bt_ = num("sell_tax"), num("buy_tax")
    if st_ > .10 or bt_ > .10:
        fails.append(f"tax buy {bt_:.0%}/sell {st_:.0%}")
    elif st_ > .05 or bt_ > .05:
        warns.append(f"tax buy {bt_:.0%}/sell {st_:.0%}")
    owner = (r.get("owner_address") or "").lower()
    renounced = owner in BURN or owner == ""
    if f("hidden_owner") or f("can_take_back_ownership"):
        fails.append("hidden/recoverable owner")
    if f("is_mintable") and not renounced:
        fails.append("owner can mint")
    if (f("transfer_pausable") or f("is_blacklisted") or f("trading_cooldown") or f("slippage_modifiable")) and not renounced:
        fails.append("owner can pause/blacklist/change tax")
    if str(r.get("is_open_source", "1")) == "0":
        warns.append("contract not verified")
    if f("is_proxy"):
        warns.append("upgradeable proxy")
    pcts = sorted((float(h.get("percent") or 0) for h in (r.get("holders") or [])
                   if str(h.get("is_contract")) != "1" and str(h.get("is_locked")) != "1"), reverse=True)
    top1, top10 = (pcts[0] if pcts else 0), sum(pcts[:10])
    facts.append(f"top holder {top1:.0%}, top10 {top10:.0%}")
    if top1 > .15 or top10 > .70:
        fails.append(f"concentrated holders ({top1:.0%} / {top10:.0%})")
    elif top1 > .08 or top10 > .50:
        warns.append(f"holders concentrated ({top1:.0%} / {top10:.0%})")
    locked = 0.0
    for h in r.get("lp_holders") or []:
        if str(h.get("is_locked")) == "1" or (h.get("address") or "").lower() in BURN:
            locked += float(h.get("percent") or 0)
    if r.get("lp_holders"):
        facts.append(f"LP locked/burned {locked:.0%}")
    return dict(ok=not fails, fails=fails, warns=warns, facts=facts,
                lp_locked=(locked if r.get("lp_holders") else None))

def security(chain, addr):
    return sec_solana(addr) if chain == "solana" else sec_evm(chain, addr)

def copycat(p):
    sym = (p.get("baseToken") or {}).get("symbol", "")
    if not sym:
        return None
    d = jget("https://api.dexscreener.com/latest/dex/search?q=" + urllib.parse.quote(sym), "search_" + re.sub(r"\W", "", sym))
    for q in (d or {}).get("pairs") or []:
        qb = q.get("baseToken") or {}
        if (q.get("chainId") == p.get("chainId") and qb.get("address") != p["baseToken"]["address"]
                and qb.get("symbol", "").lower() == sym.lower() and liq_of(q) > 5 * liq_of(p)
                and (q.get("pairCreatedAt") or 0) < p["pairCreatedAt"] - 86_400_000):
            return qb.get("name") or sym
    return None

# ---- scoring --------------------------------------------------------------------------------
def score(p, sec, boosted, clone, now):
    s, why, red = 0.0, [], []
    tx = (p.get("txns") or {})
    h1, m5 = tx.get("h1") or {}, tx.get("m5") or {}
    n = h1.get("buys", 0) + h1.get("sells", 0)
    ratio = h1.get("buys", 0) / max(n, 1)
    s += 12 * min(1, max(0, (ratio - 0.5) / 0.2))
    n5 = m5.get("buys", 0) + m5.get("sells", 0)
    accel = (n5 * 12) / max(n, 1)
    s += 10 * min(1, accel / 1.5)
    if accel >= 1.2:
        why.append("activity accelerating")
    liq = liq_of(p)
    vl = ((p.get("volume") or {}).get("h1") or 0) / max(liq, 1)
    s += 8 if 0.5 <= vl <= 8 else 3
    mcap = p.get("marketCap") or p.get("fdv") or 0
    s += 8 * min(1, liq / 40_000)
    s += 7 * min(1, (liq / mcap if mcap else 0) / 0.15)
    s += 30 if sec["ok"] else 10
    if sec["lp_locked"] is not None and sec["lp_locked"] < 0.5:
        s -= 15
        red.append("LP not locked/burned")
    s -= min(12, 4 * len(sec["warns"]))
    info = p.get("info") or {}
    socials = {x.get("type") for x in info.get("socials") or []} | ({"website"} if info.get("websites") else set())
    s += min(10, 4 * len(socials & {"twitter", "telegram", "website"}))
    if not socials:
        red.append("no socials listed")
    age = (now * 1000 - p["pairCreatedAt"]) / 60000
    s += 15 if age <= 60 else 11 if age <= 180 else 7 if age <= 360 else 3
    if boosted:
        s -= 10
        red.append("paid boost (promoted)")
    if clone:
        s -= 15
        red.append(f"possible copycat of '{clone}'")
    red += sec["warns"]
    return round(max(0, min(100, s)), 1), why, red
# ---- telegram -------------------------------------------------------------------------------
def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send(text, dry=False):
    if dry:
        print(re.sub(r"<[^>]+>", "", text), "\n" + "-" * 40)
        return
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    data = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=30)
    time.sleep(1)

def money(x):
    return f"${x/1e6:.2f}M" if x >= 1e6 else f"${x/1e3:.1f}k" if x >= 1e3 else f"${x:.0f}"

def links(chain, addr, p=None):
    out = []
    if p and p.get("url"):
        out.append(f'<a href="{esc(p["url"])}">DexScreener</a>')
    if chain == "solana":
        out.append(f'<a href="https://rugcheck.xyz/tokens/{addr}">RugCheck</a>')
        out.append(f'<a href="https://app.bubblemaps.io/sol/token/{addr}">Bubblemaps</a>')
    else:
        out.append(f'<a href="https://gopluslabs.io/token-security/{GOPLUS_ID.get(chain, "")}/{addr}">GoPlus</a>')
        out.append(f'<a href="https://app.bubblemaps.io/{chain}/token/{addr}">Bubblemaps</a>')
    return " | ".join(out)

def alert_text(p, sc, why, red, sec, now):
    b = p["baseToken"]
    age = (now * 1000 - p["pairCreatedAt"]) / 60000
    h1 = (p.get("txns") or {}).get("h1") or {}
    pc = p.get("priceChange") or {}
    return (f"<b>EARLY MEME: {esc(b.get('name'))} (${esc(b.get('symbol'))})</b> - {esc(p['chainId'])} - score <b>{sc}</b>/100\n"
            f"age {age:.0f} min | mcap {money(p.get('marketCap') or p.get('fdv') or 0)} | liq {money(liq_of(p))}\n"
            f"1h: {pc.get('h1', 0):+.0f}% | buys {h1.get('buys', 0)} / sells {h1.get('sells', 0)} | 5m: {pc.get('m5', 0):+.0f}%\n"
            f"Checks passed: {'; '.join(sec['facts']) or 'no honeypot/mint/freeze danger flags'}\n"
            + (f"+ {esc('; '.join(why))}\n" if why else "")
            + (f"! {esc('; '.join(red[:5]))}\n" if red else "! no warnings\n")
            + links(p["chainId"], b["address"], p)
            + "\n<i>Rules: size so a total loss is tiny, take your initial back at 2x, never add to a loser. "
              "Check Bubblemaps for bundled/cluster wallets before buying.</i>")

# ---- scan -----------------------------------------------------------------------------------
def scan(now, dry):
    seen, alerts = load(F_SEEN, {}), load(F_ALERTS, [])
    addrs = discover()
    pairs = dex_pairs(addrs)
    rej = {}
    cands = []
    for (chain, addr), p in pairs.items():
        if addr in seen:
            continue
        why_not = prefilter(p, now)
        if why_not:
            rej[why_not] = rej.get(why_not, 0) + 1
            continue
        sec = security(chain, addr)
        if sec["ok"] is False:
            seen[addr] = dict(t=int(now), r="unsafe", why=sec["fails"][:2])
            rej["unsafe"] = rej.get("unsafe", 0) + 1
            continue
        if sec["ok"] is None and REQUIRE_SECURITY:
            rej["no_security_data"] = rej.get("no_security_data", 0) + 1
            continue
        boosted = ((p.get("boosts") or {}).get("active") or 0) > 0
        sc, why, red = score(p, sec, boosted, copycat(p), now)
        if sec["ok"] is None:
            red.append("SECURITY UNVERIFIED")
        if sc >= MIN_SCORE:
            cands.append((sc, p, why, red, sec))
        else:
            rej["low_score"] = rej.get("low_score", 0) + 1
    cands.sort(key=lambda x: -x[0])
    for sc, p, why, red, sec in cands[:MAX_ALERTS_PER_RUN]:
        send(alert_text(p, sc, why, red, sec, now), dry)
        addr = p["baseToken"]["address"]
        seen[addr] = dict(t=int(now), r="alerted")
        alerts.append(dict(addr=addr, chain=p["chainId"], symbol=p["baseToken"].get("symbol"), t=int(now),
                           price=float(p.get("priceUsd") or 0), mcap=p.get("marketCap") or p.get("fdv") or 0,
                           liq=liq_of(p), score=sc))
    STATS.update(discovered=len(addrs), pairs=len(pairs), rejected=rej, alerts=len(cands[:MAX_ALERTS_PER_RUN]))
    seen = {k: v for k, v in seen.items() if now - v.get("t", 0) < 14 * 86400}
    save(F_SEEN, seen)
    save(F_ALERTS, alerts[-500:])
    print("scan:", json.dumps(STATS)[:600])

# ---- outcome tracker ------------------------------------------------------------------------
def track_report(now, dry):
    alerts = load(F_ALERTS, [])
    live = [a for a in alerts if now - a["t"] < 4 * 86400]
    cur = dex_pairs({(a["chain"], a["addr"]) for a in live}) if live else {}
    for a in live:
        p = cur.get((a["chain"], a["addr"]))
        if not p or not a["price"]:
            if a["t"] < now - 6 * 3600:
                a["dead"] = True
            continue
        mult = float(p.get("priceUsd") or 0) / a["price"]
        a["last"] = round(mult, 3)
        a["max"] = round(max(a.get("max", 1), mult), 3)
        if liq_of(p) < 2000 or mult < 0.15:
            a["dead"] = True
    save(F_ALERTS, alerts)
    done = [a for a in alerts if now - a["t"] >= 6 * 3600]
    if not done:
        return send("<b>Meme scanner daily report</b>\nNo alerts old enough to judge yet (needs 6h+).\n"
                    f"Last scan: {esc(json.dumps(STATS)[:500])}", dry)
    lasts = [0.0 if a.get("dead") else a.get("last", 0.0) for a in done]
    n = len(done)
    two_x = sum(1 for a in done if a.get("max", 1) >= 2)
    dead = sum(1 for a in done if a.get("dead"))
    up = sum(1 for x in lasts if x > 1)
    send("<b>Meme scanner daily report</b> (honest scoreboard)\n"
         f"Alerts judged (6h+ old): {n}\n"
         f"Reached 2x at any sampled point: {two_x} ({two_x/n:.0%})\n"
         f"Currently above entry: {up} ({up/n:.0%})\n"
         f"Dead/rugged (<0.15x or no liquidity): {dead} ({dead/n:.0%})\n"
         f"Median multiple now: {statistics.median(lasts):.2f}x\n"
         "<i>Prices sampled once a day, so real peaks are higher than 'reached 2x' shows. "
         "If most alerts are below entry, do not trade this live.</i>", dry)

# ---- smart wallets (Solana, via Helius) -----------------------------------------------------
def wallets_run(now, dry):
    wf = HERE / "wallets.txt"
    key = os.environ.get("HELIUS_API_KEY")
    if not wf.exists() or not (key or SAMPLE):
        return
    st = load(F_WALLET, dict(seen=[], buys={}))
    seen = set(st["seen"])
    labels = {}
    for line in wf.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            a, _, lab = line.partition(",")
            labels[a.strip()] = lab.strip() or a.strip()[:6]
    fresh = []
    for w in labels:
        txs = jget(f"https://api.helius.xyz/v0/addresses/{w}/transactions?api-key={key}&limit=25", f"helius_{w}") or []
        for tx in txs:
            sig = tx.get("signature")
            if not sig or sig in seen:
                continue
            seen.add(sig)
            if tx.get("type") != "SWAP":
                continue
            ts = tx.get("timestamp") or 0
            for tt in tx.get("tokenTransfers") or []:
                m = tt.get("mint")
                if tt.get("toUserAccount") == w and m and m not in IGNORE_MINTS:
                    st["buys"].setdefault(m, {})[w] = ts
                    if now - ts <= WALLET_MAX_AGE_MIN * 60:
                        fresh.append((m, w, ts))
        time.sleep(0.3)
    for m in list(st["buys"]):
        st["buys"][m] = {w: t for w, t in st["buys"][m].items() if now - t < 86400}
        if not st["buys"][m]:
            del st["buys"][m]
    done = set()
    for m, w, ts in fresh:
        if m in done:
            continue
        done.add(m)
        group = st["buys"].get(m, {})
        pairs = dex_pairs({("solana", m)})
        p = pairs.get(("solana", m))
        sec = sec_solana(m)
        head = "WALLET CLUSTER" if len(group) >= 2 else "WALLET BUY"
        who = ", ".join(esc(labels.get(x, x[:6])) for x in group)
        line = (f"<b>{head}: {len(group)} tracked wallet(s) bought</b>\n{who}\n"
                f"token <code>{m}</code>\n")
        if p:
            pc = p.get("priceChange") or {}
            line += (f"{esc(p['baseToken'].get('symbol'))} | mcap {money(p.get('marketCap') or p.get('fdv') or 0)} | "
                     f"liq {money(liq_of(p))} | 1h {pc.get('h1', 0):+.0f}%\n")
        if sec["ok"] is False:
            line += f"<b>DANGER:</b> {esc('; '.join(sec['fails'][:3]))}\n"
        elif sec["ok"] is None:
            line += "security: unverified\n"
        else:
            line += "no RugCheck danger flags\n"
        line += links("solana", m, p) + ("\n<i>Cluster = stronger signal, but wallets can be insiders or copy-bots. Verify.</i>" if len(group) >= 2 else "")
        send(line, dry)
    st["seen"] = list(seen)[-3000:]
    save(F_WALLET, st)

# ---- main -----------------------------------------------------------------------------------
def main():
    global SAMPLE
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("--sample")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    SAMPLE = a.sample
    now = time.time()
    if a.cmd == "report":
        return track_report(now, a.dry)
    scan(now, a.dry)
    wallets_run(now, a.dry)
    t = datetime.now(timezone.utc)
    if t.hour == 7 and t.minute < 30:
        track_report(now, a.dry)
    if STATS.get("errors") and STATS.get("pairs", 0) == 0:
        send("<b>Meme scanner:</b> data sources failing: " + esc("; ".join(STATS["errors"][:3])), a.dry)

if __name__ == "__main__":
    main()
