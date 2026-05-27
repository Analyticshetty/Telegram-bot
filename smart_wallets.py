"""
Smart Wallet Tracker — Module 1 of Free Birdeye Pack.

Checks whether any tracked winning wallets hold a given Solana token mint.
Uses public Solana RPC getTokenAccountsByOwner, parallelised across all wallets.
Results cached 5 minutes per mint — wallets don't flip holdings instantly.

Commands wired in bot.py:
  /addwallet <address> <label>   — owner only
  /listwallets                   — owner only
  /removewallet <address>        — owner only
"""

import json
import os
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

WALLETS_FILE = os.environ.get("WALLETS_FILE", "/data/smart_wallets.json")
SOLANA_RPC   = "https://api.mainnet-beta.solana.com"
CACHE_TTL    = 300   # seconds — 5 min
TIMEOUT      = 8
MAX_WORKERS  = 10    # parallel RPC calls; public RPC rate-limits ~40 req/s

_cache: dict = {}    # {mint: {"ts": float, "holders": list[dict]}}


# ---------- JSON I/O ----------

SEED_FILE = os.path.join(os.path.dirname(__file__), "smart_wallets.json")

def _read_json() -> dict:
    try:
        with open(WALLETS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        # First boot on persistent volume — seed from repo file
        try:
            with open(SEED_FILE, "r") as f:
                data = json.load(f)
            _write_json(data)
            return data
        except Exception:
            return {"version": "2026-05-25", "wallets": []}
    except Exception:
        return {"version": "2026-05-25", "wallets": []}


def _write_json(data: dict):
    with open(WALLETS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _all_wallets() -> list:
    """All entries including TODOs — used for admin writes."""
    return _read_json().get("wallets") or []


def load_wallets() -> list:
    """Active wallets only — TODOs skipped."""
    return [
        w for w in _all_wallets()
        if w.get("address") and not str(w["address"]).startswith("TODO")
    ]


# ---------- CRUD ----------

def add_wallet(address: str, label: str, source: str = "manual") -> bool:
    """Returns False if address already tracked."""
    all_w = _all_wallets()
    if any(w.get("address", "").lower() == address.lower() for w in all_w):
        return False
    all_w.append({
        "address":   address,
        "label":     label,
        "source":    source,
        "added":     time.strftime("%Y-%m-%d"),
        "win_count": None,
    })
    data = _read_json()
    data["wallets"] = all_w
    _write_json(data)
    _cache.clear()   # invalidate so next check uses updated list
    return True


def remove_wallet(address: str) -> bool:
    """Returns False if address not found."""
    all_w = _all_wallets()
    new_w = [w for w in all_w if w.get("address", "").lower() != address.lower()]
    if len(new_w) == len(all_w):
        return False
    data = _read_json()
    data["wallets"] = new_w
    _write_json(data)
    _cache.clear()
    return True


# ---------- RPC CHECK ----------

def _wallet_holds_token(wallet_address: str, mint: str) -> bool:
    """Single RPC call: does this wallet hold any amount of `mint`?"""
    try:
        r = requests.post(
            SOLANA_RPC,
            json={
                "jsonrpc": "2.0",
                "id":      1,
                "method":  "getTokenAccountsByOwner",
                "params":  [
                    wallet_address,
                    {"mint": mint},
                    {"encoding": "jsonParsed", "commitment": "confirmed"},
                ],
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return False
        accounts = (r.json().get("result") or {}).get("value") or []
        for acct in accounts:
            info = (
                acct.get("account", {})
                    .get("data", {})
                    .get("parsed", {})
                    .get("info", {})
            )
            ui_amount = (info.get("tokenAmount") or {}).get("uiAmount") or 0
            if ui_amount > 0:
                return True
    except Exception:
        pass
    return False


def check_wallets_hold_token(mint: str) -> list:
    """
    Returns list of {address, label} dicts for tracked wallets that hold `mint`.
    Parallelised — all wallets checked concurrently up to MAX_WORKERS threads.
    Result cached CACHE_TTL seconds.
    """
    now = time.time()
    cached = _cache.get(mint)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["holders"]

    wallets = load_wallets()
    if not wallets:
        _cache[mint] = {"ts": now, "holders": []}
        return []

    holders = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(wallets))) as ex:
        future_map = {
            ex.submit(_wallet_holds_token, w["address"], mint): w
            for w in wallets
        }
        for future in as_completed(future_map):
            wallet_info = future_map[future]
            try:
                if future.result():
                    holders.append({
                        "address": wallet_info["address"],
                        "label":   wallet_info.get("label", "unknown"),
                    })
            except Exception:
                pass

    _cache[mint] = {"ts": now, "holders": holders}
    return holders


# ---------- FORMATTER ----------

def format_smart_wallet_section(mint: str, symbol: str = None) -> str:
    """
    Returns Telegram-formatted smart wallet block to append to rug-check report.
    Called from rug_check.format_report() — mint + symbol come from result["details"].
    """
    token_label = f"${symbol}" if symbol else "this token"
    active_wallets = load_wallets()
    total = len(active_wallets)

    if total == 0:
        return (
            "\n🐋 *Smart wallets:* _(none tracked yet)_\n"
            "   Add via `/addwallet <addr> <label>`"
        )

    holders = check_wallets_hold_token(mint)
    count   = len(holders)

    if count == 0:
        return f"\n🐋 *Smart wallets:* ⚪ 0 of {total} tracked hold {token_label}"

    labels = [h["label"] for h in holders[:3]]
    label_str = ", ".join(labels)
    if count > 3:
        label_str += f" +{count - 3}"

    signal = "🔥" if count >= 3 else "👀"
    return (
        f"\n🐋 *Smart wallets:* {signal} *{count} of {total}* tracked hold {token_label}\n"
        f"   ({label_str})"
    )
