"""Consensus meme scanner (Solana + BNB). Alert-only. Needs memecoin_scanner.py in the same folder."""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import memecoin_scanner as ms

STATE_FILE = "consensus_state.json"
GUARDS = {
    "strict": {"h1": 30, "h6": 80, "h24": 200},
    "normal": {"h1": 60, "h6": 150, "h24": 400},
    "loose": {"h1": 120, "h6": 300, "h24": 900},
}
SKIP_TOKENS = {
    "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x55d398326f99059ff775485246999027b3197955",
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "0xe9e7cea3dedca5984780bafc599bd69add087d56",
}
SOL_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
EVM_RE = re.compile(r"(?<![0-9a-zA-Z])0x[a-fA-F0-9]{40}(?![0-9a-zA-Z])")
PAIR_URL_RE = re.compile(r"dexscreener\.com/(solana|bsc)/([A-Za-z0-9]{20,64})", re.I)
GECKO_NET = {"solana": "solana", "bsc": "bsc"}


def load_config():
    chans = []
    for c in os.getenv("CALL_CHANNELS", "").split(","):
        c = c.strip().replace("https://t.me/s/", "").replace("https://t.me/", "").lstrip("@").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_]{4,64}", c) and c.lower() not in [x.lower() for x in chans]:
            chans.append(c)
    guard = os.getenv("GUARD", "normal").strip().lower()
    if guard not in GUARDS:
        guard = "normal"
    min_src = os.getenv("MIN_SOURCES", "").strip()
    return {
        "channels": chans,
        "chains": [c.strip().lower() for c in os.getenv("SCAN_CHAINS", "solana,bsc").split(",") if c.strip()],
        "window_h": float(os.getenv("WINDOW_HOURS", "6") or 6),
        "min_sources": int(min_src) if min_src.isdigit() else (3 if chans else 2),
        "guard": guard, "limits": GUARDS[guard],
        "min_liq": 15_000, "min_age_h": 0.2, "max_age_h": 72, "max_mcap": 5_000_000,
        "max_lookups": 40, "realert_h": 12,
        "send_skipped": os.getenv("SEND_SKIPPED", "") == "1",
    }


def fetch_channel_html(name):
    r = requests.get(f"https://t.me/s/{name}", headers=ms.HEADERS, timeout=20)
    r.raise_for_status()
    return r.text


def parse_channel(page):
    out = []
    for block in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', block)
        if not tm:
            continue
        try:
            ts = datetime.fromisoformat(tm.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        parts = re.findall(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        raw = " ".join(parts)
        raw = re.sub(r'<a [^>]*href="([^"]+)"[^>]*>', r" \1 ", raw)
        raw = re.sub(r"<br\s*/?>", " ", raw)
        text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        out.append((ts, text))
    return out


def extract(text, chains):
    pairs = {(m.group(1).lower(), m.group(2)) for m in PAIR_URL_RE.finditer(text)}
    clean = PAIR_URL_RE.sub(" ", text)
    toks = set()
    if "solana" in chains:
        toks |= {("solana", a) for a in SOL_RE.findall(clean) if a not in SKIP_TOKENS}
    if "bsc" in chains:
        toks |= {("bsc", a.lower()) for a in EVM_RE.findall(clean) if a.lower() not in SKIP_TOKENS}
    return toks, {p for p in pairs if p[0] in chains}


def gecko_trending(net):
    r = ms.http_get(f"https://api.geckoterminal.com/api/v2/networks/{GECKO_NET[net]}/trending_pools")
    out = []
    for item in (r or {}).get("data", []):
        tid = (((item.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        if "_" in tid:
            out.append(tid.split("_", 1)[1])
    return out


def dex_boosts(path):
    r = ms.http_get(ms.DEX + path)
    return [(i.get("chainId"), i.get("tokenAddress")) for i in (r if isinstance(r, list) else [])
            if i.get("chainId") and i.get("tokenAddress")]


def pair_to_token(chain, pair):
    try:
        r = ms.http_get(f"{ms.DEX}/latest/dex/pairs/{chain}/{pair}")
        p = (r.get("pairs") or [None])[0]
        return (chain, p["baseToken"]["address"]) if p else None
    except Exception:  # noqa: BLE001
        return None


def collect_mentions(cfg, now):
    found = {}
    cutoff = now - timedelta(hours=cfg["window_h"])

    def add(key, source, when):
        found.setdefault(key, {})
        if source not in found[key] or when < found[key][source]:
            found[key][source] = when

    pair_refs = []
    for name in cfg["channels"]:
        try:
            msgs = parse_channel(fetch_channel_html(name))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] channel {name}: {e.__class__.__name__}")
            continue
        for ts, text in msgs:
            if ts < cutoff:
                continue
            toks, prs = extract(text, cfg["chains"])
            for t in toks:
                add(t, f"@{name}", ts)
            pair_refs += [(p, f"@{name}", ts) for p in prs]
        time.sleep(0.5)
    for (chain, pair), src, ts in pair_refs[:15]:
        tok = pair_to_token(chain, pair)
        if tok and tok[1] not in SKIP_TOKENS:
            add(tok, src, ts)
    for net in cfg["chains"]:
        if net in GECKO_NET:
            try:
                for a in gecko_trending(net):
                    if a not in SKIP_TOKENS and a.lower() not in SKIP_TOKENS:
                        add((net, a.lower() if net == "bsc" else a), f"GeckoTerminal trending ({net})", now)
            except Exception as e:  # noqa: BLE001
                print(f"[warn] geckoterminal {net}: {e}")
    for label, path in (("DexScreener boosts", "/token-boosts/top/v1"),
                        ("DexScreener boosts", "/token-boosts/latest/v1")):
        try:
            for chain, a in dex_boosts(path):
                if chain in cfg["chains"] and a not in SKIP_TOKENS:
                    add((chain, a.lower() if chain == "bsc" else a), label, now)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {label}: {e}")
    return found


def pct(p, k):
    return ms.num(p, "priceChange", k)


def guard_check(p, cfg):
    lim = cfg["limits"]
    m5, h1, h6, h24 = pct(p, "m5"), pct(p, "h1"), pct(p, "h6"), pct(p, "h24")
    liq, mcap = ms.num(p, "liquidity", "usd"), ms.num(p, "marketCap") or ms.num(p, "fdv")
    age = ms.age_hours(p)
    late = []
    if h1 > lim["h1"]:
        late.append(f"+{h1:.0f}% in 1h")
    if h6 > lim["h6"]:
        late.append(f"+{h6:.0f}% in 6h")
    if h24 > lim["h24"]:
        late.append(f"+{h24:.0f}% in 24h")
    if late:
        return "LATE", ["already pumped: " + ", ".join(late)]
    bad = []
    if liq < cfg["min_liq"]:
        bad.append(f"liquidity only ${liq:,.0f}")
    if age is None or age < cfg["min_age_h"]:
        bad.append("too new to judge")
    elif age > cfg["max_age_h"]:
        bad.append(f"older than {cfg['max_age_h']}h")
    if mcap and mcap > cfg["max_mcap"]:
        bad.append(f"market cap ${mcap:,.0f} too big")
    if m5 < -12 or h1 < -25:
        bad.append("price is dropping")
    b1, s1 = ms.num(p, "txns", "h1", "buys"), ms.num(p, "txns", "h1", "sells")
    if s1 > 1.5 * max(b1, 1) and s1 > 30:
        bad.append("sellers dominate")
    return ("REJECT", bad) if bad else ("OK", [])


def entry_zone(p):
    h1, h6 = pct(p, "h1"), pct(p, "h6")
    price = float(p.get("priceUsd") or 0)
    if h1 <= 25 and h6 <= 60:
        return "🟢 EARLY (has not run yet)", price * 1.10, "Max entry price: above this, skip it.", "max entry"
    return "🟡 WARMING (already moving)", price * 0.92, "Do not chase: wait for a dip to this price, skip if it keeps running.", "wait for a dip to"


def score(p, n_sources, status):
    s = min(n_sources, 5) * 12
    s += {"PASS": 20, "WARN": 8, "UNKNOWN": 5}.get(status, 0)
    s += 15 if pct(p, "h1") <= 25 and pct(p, "h6") <= 60 else 5
    b1, s1 = ms.num(p, "txns", "h1", "buys"), ms.num(p, "txns", "h1", "sells")
    s += min(10, 10 * b1 / max(b1 + s1, 1) if b1 + s1 else 0)
    s += min(10, ms.num(p, "liquidity", "usd") / 10_000)
    return min(100, round(s))


def fmt_price(x):
    return f"${x:.10f}".rstrip("0").rstrip(".") if x < 0.01 else f"${x:,.4f}".rstrip("0").rstrip(".")


def fmt_alert(p, chain, addr, sources, status, notes, now):
    base = p.get("baseToken") or {}
    sym = base.get("symbol", "?")
    price = float(p.get("priceUsd") or 0)
    zone, level, advice, word = entry_zone(p)
    first = min(sources.values())
    mins = max(1, int((now - first).total_seconds() // 60))
    age = ms.age_hours(p)
    lines = [
        f"🔥 <b>CONSENSUS PICK: ${html.escape(sym)}</b> ({chain})",
        f"Flagged by <b>{len(sources)} sources</b>: " + html.escape(", ".join(sorted(sources))),
        f"First flagged about {mins} min ago",
        "",
        f"ENTRY: {zone}",
        f"Gain so far: 5m {pct(p, 'm5'):+.0f}% · 1h {pct(p, 'h1'):+.0f}% · 6h {pct(p, 'h6'):+.0f}% · 24h {pct(p, 'h24'):+.0f}%",
        f"Price now {fmt_price(price)} → {word} {fmt_price(level)}",
        advice,
        "",
        f"Liquidity ${ms.num(p, 'liquidity', 'usd'):,.0f} · MCap ${(ms.num(p, 'marketCap') or ms.num(p, 'fdv')):,.0f}"
        + (f" · age {age:.1f}h" if age is not None else ""),
        f"Last 1h trades: {ms.num(p, 'txns', 'h1', 'buys'):.0f} buys / {ms.num(p, 'txns', 'h1', 'sells'):.0f} sells",
        f"Security: {status}" + (" (" + html.escape("; ".join(notes[:3])) + ")" if notes else ""),
        f"Score {score(p, len(sources), status)}/100",
        "",
        "Contract (tap to copy):",
        f"<code>{html.escape(addr)}</code>",
        html.escape(p.get("url") or f"https://dexscreener.com/{chain}/{addr}"),
        "",
        "Test with a tiny amount first and confirm you can sell. Crowded coins can reverse fast.",
    ]
    return "\n".join(lines)


def send_html(text):
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[info] Telegram not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", timeout=15, json={
            "chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.status_code != 200:
            print(f"[warn] telegram HTTP {r.status_code}: {r.text[:200]}")
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
    found = collect_mentions(cfg, now)
    state["alerted"] = {k: t for k, t in state["alerted"].items() if t > (now - timedelta(days=3)).timestamp()}
    ranked = sorted(((k, s) for k, s in found.items() if len(s) >= cfg["min_sources"]),
                    key=lambda kv: -len(kv[1]))
    print(f"{len(found)} tokens seen, {len(ranked)} with {cfg['min_sources']}+ sources "
          f"(guard {cfg['guard']}, channels {len(cfg['channels'])})")
    alerts, skipped = [], []
    for (chain, addr), sources in ranked[:cfg["max_lookups"]]:
        key = f"{chain}:{addr}"
        if now.timestamp() - state["alerted"].get(key, 0) < cfg["realert_h"] * 3600:
            continue
        p = ms.lookup_pair(chain, addr)
        if not p or p.get("chainId") != chain:
            continue
        sym = (p.get("baseToken") or {}).get("symbol", "?")
        verdict, why = guard_check(p, cfg)
        if verdict != "OK":
            print(f"[skip] {sym} ({chain}) {len(sources)} sources: {'; '.join(why)}")
            if verdict == "LATE":
                skipped.append(f"{sym} ({chain}): {why[0].replace('already pumped: ', '')}")
            continue
        status, notes = ms.check_security(chain, addr)
        if status == "FAIL":
            print(f"[skip] {sym} ({chain}) failed security: {'; '.join(notes[:3])}")
            continue
        alerts.append(fmt_alert(p, chain, addr, sources, status, notes, now))
        state["alerted"][key] = now.timestamp()
        time.sleep(0.3)
    for a in alerts:
        print(a, "\n")
    if not dry:
        for a in alerts:
            send_html(a)
        if skipped and cfg["send_skipped"]:
            send_html("⛔ Skipped as too late (already pumped):\n" + "\n".join(skipped[:10]))
    print(f"{len(alerts)} alert(s), {len(skipped)} skipped as too late")
    return alerts, skipped


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
