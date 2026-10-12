"""
Cloud watcher - runs once per invocation (GitHub Actions calls it every 5 min).

Alerts to Telegram when:
  1. ogantd buys or sells any coin (sells flagged)
  2. a coin's market cap crosses a threshold (BORDR above $7M)
  3. you send "mcap" or "status" to the bot (answered on the next run, <= 5 min)

State (what was already seen / already fired) is kept in state.json so the
same event is never reported twice.

Secrets come from environment variables: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json, os, re, sys, time, requests

RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# ------------------------------------------------------------------ config --
WALLETS = {
    "215nhcAHjQQGgwpQSJQ7zR26etbjjtVdW74NLzwEgQjP": "ogantd",
}
# Per wallet: which coins to report. None = every coin. Otherwise a set of mints.
WATCH = {
    "ogantd": None,                                                   # every coin he trades
}
KNOWN = {
    "6Mix12LiHrQFojaQEnfPUC65Qkwd6X4Y5Qg93oFbordr": "BORDR",          # Bordrless (the real one; 17 fakes share the ticker)
}
# Market-cap alerts. mint None = resolve by symbol on DexScreener at run time.
MC_ALERTS = [
    {"symbol": "BORDR", "mint": "6Mix12LiHrQFojaQEnfPUC65Qkwd6X4Y5Qg93oFbordr", "above": 7_000_000, "rearm_below": 6_000_000},
]

# ------------------------------------------------------------------ helpers --
def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"last_sig": {}, "fired": {}, "mints": {}}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"), indent=1)


def tg(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True}, timeout=15)
    print("telegram:", r.status_code, text.splitlines()[0][:80])


def rpc(method, params):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=25)
    r.raise_for_status()
    out = r.json()
    if "error" in out:
        raise RuntimeError(out["error"])
    return out["result"]


MAX_TX_VERSION = 1

def get_tx(sig):
    global MAX_TX_VERSION
    for _ in range(3):
        try:
            return rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                               "maxSupportedTransactionVersion": MAX_TX_VERSION}])
        except RuntimeError as e:
            m = re.search(r"maxSupportedTransactionVersion[^0-9]*(\d+)", str(e))
            if m and int(m.group(1)) > MAX_TX_VERSION:
                MAX_TX_VERSION = int(m.group(1)); continue
            raise
    return None


def fmt_usd(x):
    if x is None: return "n/a"
    if x >= 1e9: return f"${x/1e9:.2f}B"
    if x >= 1e6: return f"${x/1e6:.2f}M"
    if x >= 1e3: return f"${x/1e3:.0f}K"
    return f"${x:,.0f}"


def dex_token(mint):
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=15).json()
    pairs = r.get("pairs") or []
    if not pairs: return None
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    mc = best.get("marketCap") or best.get("fdv")
    return {"mc": float(mc) if mc else None, "price": float(best.get("priceUsd") or 0),
            "symbol": best.get("baseToken", {}).get("symbol")}


def resolve_mint(symbol, state):
    """Find a coin's mint by ticker (Solana, highest liquidity). Cached in state."""
    if symbol in state["mints"]:
        return state["mints"][symbol]
    r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={symbol}", timeout=15).json()
    cands = [p for p in (r.get("pairs") or []) if p.get("chainId") == "solana"
             and (p.get("baseToken", {}).get("symbol") or "").upper() == symbol.upper()]
    if not cands:
        return None
    best = max(cands, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    mint = best["baseToken"]["address"]
    state["mints"][symbol] = mint
    KNOWN[mint] = symbol
    return mint


def coin_name(mint):
    return KNOWN.get(mint) or (mint[:6] + "...")


# ------------------------------------------------------------- wallet check --
def describe(sig, wallet, who):
    tx = get_tx(sig)
    if not tx or tx.get("meta") is None:
        return None
    meta = tx["meta"]
    keys = tx["transaction"]["message"]["accountKeys"]
    idx = next((i for i, k in enumerate(keys) if k["pubkey"] == wallet), None)
    sol = (meta["postBalances"][idx] - meta["preBalances"][idx]) / 1e9 if idx is not None else 0.0

    def bal(entries):
        return {b["mint"]: float(b["uiTokenAmount"]["uiAmount"] or 0) for b in entries if b.get("owner") == wallet}

    pre, post = bal(meta.get("preTokenBalances", [])), bal(meta.get("postTokenBalances", []))
    watch = WATCH.get(who)
    events = []
    for mint in set(pre) | set(post):
        d = post.get(mint, 0) - pre.get(mint, 0)
        if abs(d) < 1e-9 or (watch is not None and mint not in watch):
            continue
        side = "BUY " if d > 0 else "SELL"
        if d > 0 and sol > -0.01:      # tokens arrived but no SOL spent = airdrop/spam, not a buy
            continue
        m = None
        try: m = dex_token(mint)
        except Exception: pass
        name = (m or {}).get("symbol") or coin_name(mint)
        mc = f", MC {fmt_usd(m['mc'])}" if m and m.get("mc") else ""
        events.append(f"{side} {name}: {d:+,.0f} tokens, SOL {sol:+.3f}{mc}")
    if not events:
        return None
    when = time.strftime("%H:%M UTC", time.gmtime(tx.get("blockTime") or time.time()))
    flag = "!! SELL !! " if any(e.startswith("SELL") for e in events) else ""
    return f"{flag}[{when}] {who}: " + " | ".join(events) + f"\nhttps://solscan.io/tx/{sig}"


def check_wallets(state):
    for wallet, who in WALLETS.items():
        sigs = [s["signature"] for s in rpc("getSignaturesForAddress", [wallet, {"limit": 25}])]
        if not sigs:
            continue
        last = state["last_sig"].get(wallet)
        if last is None:                       # first run: remember, don't spam history
            state["last_sig"][wallet] = sigs[0]
            print(f"{who}: initialised at {sigs[0][:8]}")
            continue
        new = []
        for s in sigs:
            if s == last: break
            new.append(s)
        for s in reversed(new):                # oldest first
            try:
                msg = describe(s, wallet, who)
                if msg: tg(msg)
            except Exception as e:
                print("describe failed:", e)
        state["last_sig"][wallet] = sigs[0]


# ------------------------------------------------------------ market caps --
def check_mcaps(state):
    for a in MC_ALERTS:
        sym = a["symbol"]
        mint = a["mint"] or resolve_mint(sym, state)
        if not mint:
            print(f"{sym}: mint not found"); continue
        m = dex_token(mint)
        if not m or not m.get("mc"):
            print(f"{sym}: no market cap"); continue
        mc = m["mc"]
        key = f"{sym}_above_{a['above']}"
        fired = state["fired"].get(key, False)
        print(f"{sym}: MC {fmt_usd(mc)} (alert above {fmt_usd(a['above'])}, fired={fired})")
        if mc >= a["above"] and not fired:
            tg(f"TARGET HIT: {sym} market cap {fmt_usd(mc)} (target {fmt_usd(a['above'])})\n"
               f"price ${m['price']:.6f}\nhttps://dexscreener.com/solana/{mint}")
            state["fired"][key] = True
        elif fired and mc < a.get("rearm_below", a["above"] * 0.85):
            state["fired"][key] = False      # dropped back: arm again for the next cross


# ---------------------------------------------------------------- commands --
def answer_commands(state):
    """Reply to 'mcap' / 'status' messages sent to the bot since the last run.
    If the Mac tracker is online it holds the Telegram connection (409 Conflict) and
    answers instantly itself, so we simply skip."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": state.get("tg_offset"), "timeout": 0}, timeout=15)
        if r.status_code == 409:
            print("commands: Mac tracker is online, it handles replies"); return
        data = r.json()
    except Exception as e:
        print("commands: getUpdates failed:", e); return
    for u in data.get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(TG_CHAT):
            continue
        text = (msg.get("text") or "").strip().lower().lstrip("/")
        cmd = text.split()[0] if text else ""
        if cmd in ("mcap", "mc", "price", "bordr"):
            lines = []
            for a in MC_ALERTS:
                m = dex_token(a["mint"])
                if m and m.get("mc"):
                    pct = 100 * m["mc"] / a["above"]
                    lines.append(f"{a['symbol']}: MC {fmt_usd(m['mc'])}, price ${m['price']:.6f}\n"
                                 f"{pct:.0f}% of the {fmt_usd(a['above'])} target (needs x{a['above']/m['mc']:.2f})")
                else:
                    lines.append(f"{a['symbol']}: market cap unavailable")
            tg("\n".join(lines) + "\n(cloud watcher, checked just now)")
        elif cmd in ("status", "alive"):
            tg(f"Cloud watcher alive. Last run {state.get('last_run', 'n/a')}. Watching ogantd + BORDR target {fmt_usd(MC_ALERTS[0]['above'])}.")
        elif cmd in ("help", "start"):
            tg("Commands: mcap (market cap), status. Replies take up to 5 min when your Mac is off.")


# ------------------------------------------------------------------- main --
if __name__ == "__main__":
    state = load_state()
    errors = []
    for step in (check_wallets, check_mcaps, answer_commands):
        try:
            step(state)
        except Exception as e:
            errors.append(f"{step.__name__}: {e}")
            print("ERROR", step.__name__, e, file=sys.stderr)
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    save_state(state)
    if "--test" in sys.argv:
        tg("Cloud watcher test OK: running on the server, laptop not needed.")
    sys.exit(0)
