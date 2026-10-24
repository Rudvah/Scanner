"""
Early NFT scanner -> Telegram: live free mints / viral mints + airdrop & free-mint headlines.

How it finds mints: reads public blockchain nodes (no API keys). Every ERC-721 / ERC-1155 mint is a
Transfer from the zero address. Collections with many fresh mints by many different wallets, and
accelerating, are "hot". Mint price is read from sample mint transactions (value 0 = free mint).

Safety: GoPlus NFT security check (contract-level). Optional: OPENSEA_API_KEY adds floor price/volume.

Commands:  python nft_scanner.py run [--sample DIR] [--dry]
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENSEA_API_KEY (optional)

NOT financial advice. Most free mints are worth ~0 and many mint websites are wallet drainers.
"""
import argparse, json, os, re, statistics, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
ST = HERE / "state"
F_SEEN, F_ALERTS = ST / "nft_seen.json", ST / "nft_alerts.json"

# ---- tunables ---------------------------------------------------------------------------
CHAINS = {
    "base":     dict(rpc="https://mainnet.base.org", gp="8453", ex="https://basescan.org", bpm=30, os="base"),
    "ethereum": dict(rpc="https://ethereum-rpc.publicnode.com", gp="1", ex="https://etherscan.io", bpm=5, os="ethereum"),
    "zora":     dict(rpc="https://rpc.zora.energy", gp="7777777", ex="https://explorer.zora.energy", bpm=30, os="zora"),
}
ACTIVE = ["base", "ethereum", "zora"]
WINDOW_MIN = 20            # look at the last N minutes of mints (and the N before, to measure acceleration)
MIN_MINTS = {"base": 120, "zora": 120, "ethereum": 60}   # minimum mints in the window
MIN_UNIQUE = 40            # distinct minting wallets
MIN_UNIQ_RATIO = 0.35      # unique wallets / mints (low = one wallet spamming)
MIN_GROWTH = 1.5           # mints now vs previous window
MAX_PAID_ETH = 0.01        # ignore expensive paid mints
MIN_SCORE = 55
MAX_ALERTS_PER_RUN = 4
REQUIRE_SECURITY = False   # True = skip collections GoPlus has no data on
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
ZERO32 = "0x" + "0" * 64
NEWS_FEEDS = ["https://nftplazas.com/feed/", "https://nftevening.com/feed/", "https://decrypt.co/feed",
              "https://www.theblock.co/rss.xml", "https://cointelegraph.com/rss"]
NEWS_KEYS = re.compile(r"free mint|freemint|airdrop|allowlist|whitelist|mint date|mint live|claim now|"
                       r"snapshot|points program|nft drop", re.I)

SAMPLE = None
STATS = {}

# ---- http / rpc -------------------------------------------------------------------------------
def sample(key):
    p = Path(SAMPLE) / f"{key}.json"
    return json.loads(p.read_text()) if p.exists() else None

def err(msg):
    STATS.setdefault("errors", []).append(msg[:90])

def rpc(chain, method, params, key):
    if SAMPLE:
        return sample(key)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(CHAINS[chain]["rpc"], data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": "nft-scanner/1.0"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except Exception as e:
        err(f"{chain} {method}: {e}")
        return None
    if "error" in r:
        err(f"{chain} {method}: {str(r['error'])[:60]}")
        return None
    return r.get("result")

def jget(url, key, headers=None):
    if SAMPLE:
        return sample(key)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "nft-scanner/1.0", **(headers or {})})
        return json.loads(urllib.request.urlopen(req, timeout=25).read())
    except Exception as e:
        err(f"{url.split('?')[0][8:60]}: {str(e)[:30]}")
        return None

def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default

def save(path, obj):
    ST.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, indent=1))

# ---- on-chain mint detection ------------------------------------------------------------------
def mints(chain, a, b, tag):
    """Aggregate mints per contract in blocks [a, b]. Covers ERC-721 and ERC-1155."""
    agg = {}
    def add(log, who, qty, kind):
        g = agg.setdefault(log["address"].lower(), dict(n=0, minters=set(), txs={}, kind=kind))
        g["n"] += qty
        g["minters"].add(who)
        g["txs"][log["transactionHash"]] = g["txs"].get(log["transactionHash"], 0) + qty
    l721 = rpc(chain, "eth_getLogs", [dict(fromBlock=hex(a), toBlock=hex(b), topics=[TRANSFER, ZERO32])], f"l721_{tag}_{chain}") or []
    for l in l721:
        if len(l.get("topics", [])) == 4:                       # 4 topics = ERC-721 (ERC-20 has 3)
            add(l, l["topics"][2][-40:], 1, "ERC-721")
    l1155 = rpc(chain, "eth_getLogs", [dict(fromBlock=hex(a), toBlock=hex(b), topics=[SINGLE, None, ZERO32])], f"l1155_{tag}_{chain}") or []
    for l in l1155:
        d = l.get("data", "")
        qty = int(d[66:130], 16) if len(d) >= 130 else 1
        add(l, l["topics"][3][-40:], min(qty, 50), "ERC-1155")
    return agg

def dec_str(h):
    try:
        b = bytes.fromhex(h[2:])
        if len(b) >= 96:
            off = int.from_bytes(b[:32], "big")
            ln = int.from_bytes(b[off:off + 32], "big")
            return b[off + 32:off + 32 + ln].decode("utf8", "ignore")
        return b.rstrip(b"\0").decode("utf8", "ignore")
    except Exception:
        return ""

def call(chain, addr, sel, tag):
    return rpc(chain, "eth_call", [{"to": addr, "data": sel}, "latest"], f"call_{tag}_{addr}")

def info(chain, addr):
    name = dec_str(call(chain, addr, "0x06fdde03", "name") or "0x")
    sym = dec_str(call(chain, addr, "0x95d89b41", "symbol") or "0x")
    ts = call(chain, addr, "0x18160ddd", "supply")
    supply = int(ts, 16) if ts and ts != "0x" else None
    return name or "(unnamed)", sym, supply

def mint_price(chain, g):
    """Median price per mint in wei from up to 3 sample txs; 0 = free (gas only)."""
    prices = []
    for h in list(g["txs"])[:3]:
        tx = rpc(chain, "eth_getTransactionByHash", [h], f"tx_{h}")
        if tx and tx.get("value") is not None:
            prices.append(int(tx["value"], 16) / max(g["txs"][h], 1))
    return statistics.median(prices) if prices else None

def nft_security(chain, addr):
    d = jget(f"https://api.gopluslabs.io/api/v1/nft_security/{CHAINS[chain]['gp']}?contract_addresses={addr}", f"gp_{addr}")
    r = ((d or {}).get("result") or {}).get(addr.lower())
    if not r:
        return dict(ok=None, fails=[], warns=[])
    def flag(k):
        v = r.get(k)
        if isinstance(v, dict):
            v = v.get("value", v.get("status"))
        return str(v) == "1"
    fails, warns = [], []
    if flag("malicious_nft_contract"):
        fails.append("flagged malicious")
    if flag("self_destruct"):
        fails.append("self-destruct")
    if flag("transfer_without_approval"):
        fails.append("owner can move NFTs without approval")
    if flag("privileged_burn"):
        warns.append("owner can burn your NFTs")
    if flag("restricted_approval"):
        warns.append("owner can restrict marketplace approval")
    if flag("oversupply_minting"):
        warns.append("can mint beyond max supply")
    if str(r.get("nft_open_source", "1")) == "0":
        warns.append("contract not verified")
    return dict(ok=not fails, fails=fails, warns=warns)

def opensea(chain, addr):
    key = os.environ.get("OPENSEA_API_KEY")
    if not (key or SAMPLE):
        return None
    h = {"x-api-key": key or "", "accept": "application/json"}
    d = jget(f"https://api.opensea.io/api/v2/chain/{CHAINS[chain]['os']}/contract/{addr}/nfts?limit=1", f"os_nfts_{addr}", h)
    nfts = (d or {}).get("nfts") or []
    if not nfts:
        return None
    slug = nfts[0].get("collection")
    s = jget(f"https://api.opensea.io/api/v2/collections/{slug}/stats", f"os_stats_{addr}", h) or {}
    t = s.get("total") or {}
    return dict(slug=slug, floor=t.get("floor_price"), sym=t.get("floor_price_symbol", "ETH"),
                owners=t.get("num_owners"), sales=t.get("sales"))

# ---- scoring ---------------------------------------------------------------------------------
def score(chain, g, prev_n, price_wei, sec):
    s, why, red = 0.0, [], []
    n, uniq = g["n"], len(g["minters"])
    s += 25 * min(1, n / 300)
    growth = n / max(prev_n, 1)
    s += 20 * min(1, growth / 5)
    if prev_n == 0:
        why.append("brand-new activity (nothing in prior window)")
    elif growth >= 3:
        why.append(f"mint rate {growth:.1f}x vs prior window")
    s += 20 * min(1, uniq / 150)
    ratio = uniq / max(n, 1)
    s += 10 * min(1, ratio / 0.8)
    if ratio < 0.5:
        red.append(f"many mints per wallet ({n} mints / {uniq} wallets)")
    free = price_wei == 0
    if free:
        s += 10
        why.append("FREE mint (gas only)")
    elif price_wei is not None and price_wei / 1e18 <= 0.002:
        s += 5
    s += 15 if sec["ok"] else 5
    red += sec["warns"]
    s -= min(10, 3 * len(sec["warns"]))
    return round(max(0, min(100, s)), 1), why, red
# ---- telegram ---------------------------------------------------------------------------------
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

def alert_text(chain, addr, name, sym, supply, g, prev_n, price_wei, sc, why, red, sec, osd):
    c = CHAINS[chain]
    price = ("FREE (gas only)" if price_wei == 0 else
             f"{price_wei / 1e18:.4f} ETH each" if price_wei is not None else "unknown")
    t = (f"<b>HOT MINT: {esc(name)} ({esc(sym)})</b> - {chain} - {g['kind']} - score <b>{sc}</b>/100\n"
         f"Contract (tap to copy):\n<code>{addr}</code>\n"
         f"Mint price: <b>{price}</b>\n"
         f"Last {WINDOW_MIN} min: {g['n']} mints by {len(g['minters'])} wallets (prior {WINDOW_MIN} min: {prev_n})"
         + (f" | supply so far {supply}" if supply is not None else "") + "\n")
    if osd and osd.get("floor") is not None:
        t += f"OpenSea floor {osd['floor']} {osd['sym']} | owners {osd.get('owners')} | sales {osd.get('sales')}\n"
    elif osd is None:
        t += "Floor/resale: not listed on OpenSea (yet) - nothing proves it can be sold.\n"
    t += ("Safety: " + ("contract passed GoPlus NFT checks" if sec["ok"] else "UNVERIFIED (no GoPlus data)") + "\n")
    if why:
        t += f"+ {esc('; '.join(why))}\n"
    t += f"! {esc('; '.join(red[:5]))}\n" if red else "! no contract warnings\n"
    t += (f'<a href="{c["ex"]}/address/{addr}">Explorer</a> | '
          f'<a href="https://mint.fun/{chain}/{addr}">mint.fun</a> | '
          f'<a href="https://gopluslabs.io/nft-security/{c["gp"]}/{addr}">GoPlus</a>\n'
          "<i>Free does not mean valuable: most free mints sell for nothing. The real risk is the MINT WEBSITE - "
          "use a burner wallet, find the official link on the project's X account, never sign 'setApprovalForAll' "
          "or anything you can't read.</i>")
    return t

# ---- scan -------------------------------------------------------------------------------------
def scan(now, dry):
    seen, alerts = load(F_SEEN, dict(c={}, h={})), load(F_ALERTS, [])
    cands, rej = [], {}
    for ch in ACTIVE:
        head_hex = rpc(ch, "eth_blockNumber", [], f"head_{ch}")
        if not head_hex:
            rej[f"{ch}_rpc_down"] = 1
            continue
        head = int(head_hex, 16)
        w = WINDOW_MIN * CHAINS[ch]["bpm"]
        cur, prev = mints(ch, head - w, head, f"now"), mints(ch, head - 2 * w, head - w - 1, "prev")
        STATS[f"{ch}_contracts_minting"] = len(cur)
        for addr, g in cur.items():
            k = f"{ch}:{addr}"
            if k in seen["c"]:
                continue
            uniq = len(g["minters"])
            pn = prev.get(addr, {}).get("n", 0)
            if g["n"] < MIN_MINTS[ch] or uniq < MIN_UNIQUE:
                continue
            if uniq / g["n"] < MIN_UNIQ_RATIO:
                rej["wallet_spam"] = rej.get("wallet_spam", 0) + 1
                continue
            if g["n"] < MIN_GROWTH * pn:
                rej["not_accelerating"] = rej.get("not_accelerating", 0) + 1
                continue
            price = mint_price(ch, g)
            if price is not None and price / 1e18 > MAX_PAID_ETH:
                rej["too_expensive"] = rej.get("too_expensive", 0) + 1
                continue
            sec = nft_security(ch, addr)
            if sec["ok"] is False:
                seen["c"][k] = int(now)
                rej["unsafe"] = rej.get("unsafe", 0) + 1
                continue
            if sec["ok"] is None and REQUIRE_SECURITY:
                rej["no_security_data"] = rej.get("no_security_data", 0) + 1
                continue
            sc, why, red = score(ch, g, pn, price, sec)
            if sc >= MIN_SCORE:
                cands.append((sc, ch, addr, g, pn, price, why, red, sec))
            else:
                rej["low_score"] = rej.get("low_score", 0) + 1
    cands.sort(key=lambda x: -x[0])
    for sc, ch, addr, g, pn, price, why, red, sec in cands[:MAX_ALERTS_PER_RUN]:
        name, sym, supply = info(ch, addr)
        send(alert_text(ch, addr, name, sym, supply, g, pn, price, sc, why, red, sec, opensea(ch, addr)), dry)
        seen["c"][f"{ch}:{addr}"] = int(now)
        alerts.append(dict(chain=ch, addr=addr, name=name, t=int(now), supply=supply, free=(price == 0),
                           price=price, score=sc))
    STATS.update(rejected=rej, alerts=len(cands[:MAX_ALERTS_PER_RUN]))
    seen["c"] = {k: v for k, v in seen["c"].items() if now - v < 7 * 86400}
    save(F_SEEN, seen)
    save(F_ALERTS, alerts[-300:])
    return seen

# ---- headlines (airdrops / free mints / allowlists) -----------------------------------------------
def headlines(now, seen, dry):
    blobs = []
    if SAMPLE:
        p = Path(SAMPLE) / "news.xml"
        blobs = [p.read_bytes()] if p.exists() else []
    else:
        for u in NEWS_FEEDS:
            try:
                blobs.append(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "nft-scanner/1.0"}), timeout=20).read())
            except Exception as e:
                err(f"feed {u[8:40]}: {str(e)[:25]}")
    new = []
    for b in blobs:
        try:
            root = ET.fromstring(b)
        except ET.ParseError:
            continue
        for it in root.iter("item"):
            t = (it.findtext("title") or "").strip()
            l = (it.findtext("link") or "").strip()
            if t and NEWS_KEYS.search(t) and t not in seen["h"]:
                seen["h"][t] = int(now)
                new.append((t, l))
    if new:
        send("<b>NFT / airdrop / free-mint headlines</b>\n" +
             "\n".join(f'- <a href="{esc(l)}">{esc(t)}</a>' for t, l in new[:6]) +
             "\n<i>Headlines are leads, not endorsements. Many 'airdrop' posts are scams: never connect a wallet to a link "
             "you didn't verify on the project's official X account.</i>", dry)
    seen["h"] = dict(sorted(seen["h"].items(), key=lambda kv: -kv[1])[:500])
    save(F_SEEN, seen)

# ---- daily scoreboard -----------------------------------------------------------------------------
def report(now, dry):
    alerts = load(F_ALERTS, [])
    done = [a for a in alerts if now - a["t"] >= 24 * 3600][-40:]
    if not done:
        return send("<b>NFT scanner daily report</b>\nNo alerts older than 24h to judge yet.\n"
                    f"Last scan: {esc(json.dumps(STATS)[:400])}", dry)
    cont, floors, ups = 0, 0, 0
    for a in done:
        ts = call(a["chain"], a["addr"], "0x18160ddd", "supply")
        now_supply = int(ts, 16) if ts and ts != "0x" else None
        if a.get("supply") and now_supply and now_supply >= 1.2 * a["supply"]:
            cont += 1
        o = opensea(a["chain"], a["addr"])
        if o and o.get("floor") is not None:
            floors += 1
            paid = (a.get("price") or 0) / 1e18
            if float(o["floor"]) > paid:
                ups += 1
    n = len(done)
    free = sum(1 for a in done if a.get("free"))
    t = (f"<b>NFT scanner daily report</b> (honest scoreboard)\nAlerts judged (24h+ old): {n} ({free} were free mints)\n"
         f"Still being minted 24h later (+20% supply): {cont} ({cont / n:.0%})\n")
    if floors:
        t += f"Have an OpenSea floor above mint price: {ups} of {floors} listed\nNot listed / no floor: {n - floors}\n"
    else:
        t += "Resale value not measured (add OPENSEA_API_KEY to see floors).\n"
    send(t + "<i>If few alerts keep minting or hold a floor, treat this as entertainment, not income.</i>", dry)

# ---- main ---------------------------------------------------------------------------------------------
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
        return report(now, a.dry)
    seen = scan(now, a.dry)
    headlines(now, seen, a.dry)
    t = datetime.now(timezone.utc)
    if t.hour == 7 and t.minute < 30:
        report(now, a.dry)
    print("nft:", json.dumps(STATS)[:700])
    if STATS.get("errors") and not any(k.endswith("_contracts_minting") for k in STATS):
        send("<b>NFT scanner:</b> data sources failing: " + esc("; ".join(STATS["errors"][:3])), a.dry)

if __name__ == "__main__":
    main()
  
