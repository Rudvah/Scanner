"""Free NFT mint finder (alert-only: it never mints or signs anything).
Sources: Magic Eden launchpad (Solana), OpenSea drops (EVM chains, free API key), public Telegram channels.
Settings (env): NFT_CHAINS (default "all"), NFT_CHANNELS, OPENSEA_API_KEY, MAGICEDEN_API_KEY,
HOURS_AHEAD (default 24), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID."""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

STATE_FILE = "nft_state.json"
HEADERS = {"User-Agent": "nft-free-mint-finder/1.0"}
GOPLUS_IDS = {"ethereum": "1", "bsc": "56", "base": "8453", "polygon": "137", "arbitrum": "42161", "optimism": "10"}
CHAIN_WORDS = {"solana": "solana", "sol": "solana", "ethereum": "ethereum", "eth": "ethereum", "bnb": "bsc", "bsc": "bsc",
               "base": "base", "polygon": "polygon", "arbitrum": "arbitrum", "optimism": "optimism"}
OPENSEA_CHAINS = {"ethereum": "ethereum", "eth": "ethereum", "matic": "polygon", "polygon": "polygon", "base": "base",
                  "arbitrum": "arbitrum", "optimism": "optimism", "bsc": "bsc", "bnb": "bsc", "solana": "solana"}
KNOWN_SITES = ("opensea.io", "magiceden.io", "magiceden.us", "mint.fun", "zora.co", "highlight.xyz", "manifold.xyz",
               "tensor.trade", "launchmynft.io", "crossmint.com", "fair.xyz", "scatter.art", "four.meme")
FREE_RE = re.compile(r"free\s*-?\s*mint|free to mint|mint\s*(price)?\s*[:=]?\s*free|0\s*(eth|sol|bnb)\s*mint|"
                     r"mint\s*(price)?\s*[:=]?\s*0\s*(eth|sol|bnb)\b", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
DANGER_KEYS = ("malicious_nft_contract", "self_destruct", "transfer_without_approval")
WARN_KEYS = ("oversupply_minting", "restricted_approval", "privileged_burn", "privileged_minting")


def load_config():
    chains = os.getenv("NFT_CHAINS", "all").strip().lower()
    wanted = None if chains in ("", "all") else {CHAIN_WORDS.get(c.strip(), c.strip()) for c in chains.split(",")}
    chans = []
    for c in os.getenv("NFT_CHANNELS", "").split(","):
        c = c.strip().replace("https://t.me/s/", "").replace("https://t.me/", "").lstrip("@").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_]{4,64}", c) and c.lower() not in [x.lower() for x in chans]:
            chans.append(c)
    return {"chains": wanted, "channels": chans, "hours_ahead": float(os.getenv("HOURS_AHEAD", "24") or 24),
            "opensea_key": os.getenv("OPENSEA_API_KEY", "").strip(), "me_key": os.getenv("MAGICEDEN_API_KEY", "").strip()}


def get_json(url, params=None, headers=None):
    last = None
    for i in range(3):
        try:
            r = requests.get(url, params=params, headers={**HEADERS, **(headers or {})}, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                last = "rate limited"
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e.__class__.__name__
            time.sleep(1 + i)
    raise RuntimeError(f"GET failed ({last})")


def to_float(x):
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, dict):
        for k in ("amount", "value", "price", "eth", "native"):
            if k in x:
                return to_float(x[k])
        return None
    if isinstance(x, str):
        if x.strip().lower() == "free":
            return 0.0
        m = re.match(r"\s*([0-9]*\.?[0-9]+)", x)
        return float(m.group(1)) if m else None
    return None


def parse_time(x):
    if isinstance(x, (int, float)) and x > 0:
        return datetime.fromtimestamp(x / 1000 if x > 1e11 else x, timezone.utc)
    if isinstance(x, str) and x:
        try:
            return datetime.fromisoformat(x.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def magiceden_drops(cfg):
    headers = {"Authorization": f"Bearer {cfg['me_key']}"} if cfg["me_key"] else None
    data = get_json("https://api-mainnet.magiceden.dev/v2/launchpad/collections", {"offset": 0, "limit": 100}, headers)
    out = []
    for c in data if isinstance(data, list) else []:
        sym = c.get("symbol")
        if not sym:
            continue
        out.append({"id": f"solana:me:{sym}", "name": c.get("name") or sym, "chain": "solana", "price": to_float(c.get("price")),
                    "currency": "SOL", "supply": c.get("size"), "start": parse_time(c.get("launchDatetime")), "end": None,
                    "link": f"https://magiceden.io/launchpad/{sym}", "alt": [f"https://magiceden.io/marketplace/{sym}"],
                    "contract": None, "source": "Magic Eden launchpad"})
    return out


def opensea_drops(cfg):
    out = []
    for kind in ("upcoming", "featured"):
        data = get_json("https://api.opensea.io/api/v2/drops", {"type": kind, "limit": 50}, {"x-api-key": cfg["opensea_key"]})
        for d in (data or {}).get("drops", []):
            slug = d.get("collection_slug") or d.get("slug")
            chain = OPENSEA_CHAINS.get(str(d.get("chain", "")).lower())
            if not slug or not chain:
                continue
            stages = d.get("stages") or []
            prices = [to_float(s.get("price")) for s in stages if isinstance(s, dict)]
            prices = [p for p in prices if p is not None]
            price = to_float(d.get("mint_price"))
            if price is None and prices:
                price = min(prices)
            starts = [parse_time(s.get("start_time")) for s in stages if isinstance(s, dict)]
            start = parse_time(d.get("start_time")) or min([t for t in starts if t], default=None)
            out.append({"id": f"{chain}:os:{slug}", "name": d.get("collection_name") or slug, "chain": chain, "price": price,
                        "currency": {"bsc": "BNB", "polygon": "POL", "solana": "SOL"}.get(chain, "ETH"),
                        "supply": d.get("total_supply") or d.get("supply"), "start": start, "end": parse_time(d.get("end_time")),
                        "link": d.get("drop_url") or f"https://opensea.io/collection/{slug}/overview",
                        "contract": d.get("contract_address"), "source": "OpenSea drops"})
    return out


def fetch_channel_html(name):
    r = requests.get(f"https://t.me/s/{name}", headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def parse_channel(page):
    out = []
    for block in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not tm:
            continue
        ts = parse_time(tm.group(1))
        parts = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        raw = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>', r" \1 ", " ".join(parts))
        out.append((ts, html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<br\s*/?>", " ", raw)))))
    return out


def guess_chain(text):
    t = text.lower()
    for word, chain in CHAIN_WORDS.items():
        if re.search(rf"(?<![a-z]){word}(?![a-z])", t):
            return chain
    if "magiceden.io" in t or "tensor.trade" in t:
        return "solana"
    return "unknown"


def channel_drops(cfg, now):
    out = []
    for name in cfg["channels"]:
        try:
            msgs = parse_channel(fetch_channel_html(name))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] channel {name}: {e.__class__.__name__}")
            continue
        for ts, text in msgs:
            if not ts or ts < now - timedelta(hours=12) or not FREE_RE.search(text):
                continue
            links = [u.rstrip(".,") for u in URL_RE.findall(text) if "t.me/" not in u][:3]
            if not links:
                continue
            first = text.strip().split("\n")[0][:80]
            out.append({"id": f"chan:{name}:{links[0]}", "name": first or links[0], "chain": guess_chain(text), "price": 0.0,
                        "currency": "", "supply": None, "start": None, "end": None, "link": links[0], "links": links,
                        "contract": None, "source": f"channel @{name} (unverified)", "snippet": " ".join(text.split())[:220],
                        "posted": ts})
        time.sleep(0.5)
    return out


def nft_security(chain, contract):
    cid = GOPLUS_IDS.get(chain)
    if not cid or not contract or not re.fullmatch(r"0x[a-fA-F0-9]{40}", contract):
        return "UNKNOWN", ["no contract check available"]
    try:
        r = get_json(f"https://api.gopluslabs.io/api/v1/nft_security/{cid}", {"contract_addresses": contract.lower()})
        res = ((r or {}).get("result") or {}).get(contract.lower()) or {}
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", [f"check failed ({e.__class__.__name__})"]
    if not res:
        return "UNKNOWN", ["contract not in the security database"]

    def on(k):
        v = res.get(k)
        v = v.get("value") if isinstance(v, dict) else v
        return str(v) == "1"
    danger = [k.replace("_", " ") for k in DANGER_KEYS if on(k)]
    warn = [k.replace("_", " ") for k in WARN_KEYS if on(k)]
    return ("FAIL", danger + warn) if danger else ("WARN", warn) if warn else ("PASS", [])


def domain_note(link):
    m = re.match(r"https?://([^/]+)", link or "")
    host = (m.group(1).lower() if m else "").removeprefix("www.")
    if any(host == s or host.endswith("." + s) for s in KNOWN_SITES):
        return "known mint platform", True
    return f"UNKNOWN site ({host or '?'}): high phishing risk", False


def is_free(d):
    return d["price"] is not None and d["price"] <= 0


def wanted(d, cfg, now):
    if cfg["chains"] is not None and d["chain"] not in cfg["chains"] and d["chain"] != "unknown":
        return False
    if d["start"] and d["start"] > now + timedelta(hours=cfg["hours_ahead"]):
        return False
    if d.get("end") and d["end"] < now:
        return False
    if d["start"] and not d.get("end") and d["start"] < now - timedelta(hours=24):
        return False
    return is_free(d)


def when_text(d, now):
    wat = timezone(timedelta(hours=1))
    if not d["start"]:
        return "start time unknown: open the link to check" if not d.get("posted") else \
            f"posted {int((now - d['posted']).total_seconds() // 60)} min ago, check if it is still live"
    mins = int((d["start"] - now).total_seconds() // 60)
    if mins <= 0:
        return "LIVE now" + (f" (ends {d['end'].astimezone(wat):%a %d %b %H:%M} WAT)" if d.get("end") else "")
    h, m = divmod(mins, 60)
    return f"starts in {h}h {m}m ({d['start'].astimezone(wat):%a %d %b %H:%M} WAT)"


def fmt_alert(d, status, notes, now):
    site, known = domain_note(d["link"])
    lines = [f"🎁 <b>FREE MINT: {html.escape(str(d['name']))}</b> ({d['chain']})",
             "Price: FREE (you only pay network fee)" + (f" · Supply {int(d['supply']):,}" if isinstance(d.get("supply"), (int, float)) else ""),
             f"When: {when_text(d, now)}",
             f"Source: {html.escape(d['source'])}",
             f"Contract check: {status}" + (" (" + html.escape("; ".join(notes[:3])) + ")" if notes else ""),
             f"Site: {html.escape(site)}"]
    if d.get("snippet"):
        lines.append("Post: " + html.escape(d["snippet"]))
    lines.append(html.escape(d["link"]))
    for extra in (d.get("alt") or []):
        lines.append("Backup link: " + html.escape(extra))
    if d["source"].startswith("Magic Eden"):
        lines.append("If a link says 404, open magiceden.io and search the collection name above.")
    lines += ["", "Before you mint: use a separate wallet with little money in it, open only the project's official link, "
              "and read the transaction preview. A mint should never ask permission to move all your tokens or NFTs. "
              "Free does not mean valuable: most free mints are worth nothing."]
    return "\n".join(lines)


def send_html(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[info] Telegram not set up")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15, json={
            "chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"[warn] telegram failed ({e.__class__.__name__})")
        return False


def load_state():
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) and "alerted" in st else {"alerted": {}}
    except (FileNotFoundError, ValueError):
        return {"alerted": {}}


def run(cfg, state, now, dry=False):
    drops = []
    sources = [("Magic Eden", lambda: magiceden_drops(cfg), cfg["chains"] is None or "solana" in cfg["chains"]),
               ("OpenSea", lambda: opensea_drops(cfg), bool(cfg["opensea_key"])),
               ("channels", lambda: channel_drops(cfg, now), bool(cfg["channels"]))]
    for name, fn, enabled in sources:
        if not enabled:
            print(f"[info] {name}: off" + (" (add OPENSEA_API_KEY)" if name == "OpenSea" else ""))
            continue
        try:
            got = fn()
            print(f"[info] {name}: {len(got)} drops")
            drops += got
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {name}: {e}")
    state["alerted"] = {k: t for k, t in state["alerted"].items() if t > (now - timedelta(days=7)).timestamp()}
    alerts = []
    for d in drops:
        if d["id"] in state["alerted"] or not wanted(d, cfg, now):
            continue
        status, notes = nft_security(d["chain"], d.get("contract"))
        if status == "FAIL":
            print(f"[skip] {d['name']}: {'; '.join(notes)}")
            state["alerted"][d["id"]] = now.timestamp()
            continue
        alerts.append(fmt_alert(d, status, notes, now))
        state["alerted"][d["id"]] = now.timestamp()
        if len(alerts) >= 8:
            break
    for a in alerts:
        print(a, "\n")
        if not dry:
            send_html(a)
    print(f"{len(alerts)} free mint alert(s)")
    return alerts


def main():
    dry = "--dry" in sys.argv
    cfg = load_config()
    now = datetime.now(timezone.utc)
    state = load_state()
    run(cfg, state, now, dry)
    if not dry:
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
