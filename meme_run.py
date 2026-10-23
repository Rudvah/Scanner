import meme_scanner as m

def links(chain, addr, p=None):
    chart = []
    if p and p.get("url"):
        chart.append(f'<a href="{m.esc(p["url"])}">DexScreener</a>')
    if chain == "solana":
        chart.append(f'<a href="https://rugcheck.xyz/tokens/{addr}">RugCheck</a>')
        chart.append(f'<a href="https://app.bubblemaps.io/sol/token/{addr}">Bubblemaps</a>')
        buy = [f'<a href="https://jup.ag/swap/SOL-{addr}">Jupiter</a>',
               f'<a href="https://raydium.io/swap/?inputMint=sol&outputMint={addr}">Raydium</a>',
               f'<a href="https://birdeye.so/token/{addr}?chain=solana">Birdeye</a>']
    else:
        chart.append(f'<a href="https://gopluslabs.io/token-security/{m.GOPLUS_ID.get(chain, "")}/{addr}">GoPlus</a>')
        chart.append(f'<a href="https://app.bubblemaps.io/{chain}/token/{addr}">Bubblemaps</a>')
        if chain == "bsc":
            buy = [f'<a href="https://pancakeswap.finance/swap?chain=bsc&outputCurrency={addr}">PancakeSwap</a>']
        else:
            buy = [f'<a href="https://app.uniswap.org/swap?chain={chain}&outputCurrency={addr}">Uniswap</a>']
    return "Check: " + " | ".join(chart) + "\nBuy: " + " | ".join(buy)

def alert_text(p, sc, why, red, sec, now):
    b = p["baseToken"]
    age = (now * 1000 - p["pairCreatedAt"]) / 60000
    h1 = (p.get("txns") or {}).get("h1") or {}
    pc = p.get("priceChange") or {}
    return (f"<b>EARLY MEME: {m.esc(b.get('name'))} (${m.esc(b.get('symbol'))})</b> - {m.esc(p['chainId'])} - score <b>{sc}</b>/100\n"
            f"Contract (tap to copy):\n<code>{m.esc(b['address'])}</code>\n"
            f"age {age:.0f} min | mcap {m.money(p.get('marketCap') or p.get('fdv') or 0)} | liq {m.money(m.liq_of(p))}\n"
            f"1h: {pc.get('h1', 0):+.0f}% | buys {h1.get('buys', 0)} / sells {h1.get('sells', 0)} | 5m: {pc.get('m5', 0):+.0f}%\n"
            f"Checks passed: {'; '.join(sec['facts']) or 'no honeypot/mint/freeze danger flags'}\n"
            + (f"+ {m.esc('; '.join(why))}\n" if why else "")
            + (f"! {m.esc('; '.join(red[:5]))}\n" if red else "! no warnings\n")
            + links(p["chainId"], b["address"], p)
            + "\n<i>Fake copies of popular names exist: only buy the contract above. Test with a tiny amount first and "
              "confirm you can sell it. Take your initial back at 2x. Check Bubblemaps for bundled wallets.</i>")

m.links = links
m.alert_text = alert_text
m.main()
