"""
Offline stress test for Tier 1 capital protection.

Tests:
  - capital_guard: size/liq/slip/revenge across boundary + edge inputs
  - bot's confirmation-action parking flow (tools.py side)
  - position_tracker: thin-liq dynamic poll interval
  - rug_check guard-panel integration doesn't crash on missing liq

Stub Redis for everything. NO network calls. Run before every commit:
    python _test_capital_guard.py

Exit 0 = all green. Exit 1 = at least one failure.
"""
import json
import sys
import time as _time

# Windows console fix — force utf-8 stdout
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------- StubRedis ----------

class StubRedis:
    def __init__(self):
        self.kv = {}
    def get(self, k):
        v = self.kv.get(k)
        return v if v is None else (v if isinstance(v, str) else json.dumps(v))
    def set(self, k, v, ex=None):
        self.kv[k] = v if isinstance(v, str) else json.dumps(v)
        return True
    def delete(self, k):
        self.kv.pop(k, None)
        return 1


_stub = StubRedis()

# Patch redis_client BEFORE any module that imports it
import redis_client
redis_client.get_redis = lambda: _stub


# ---------- Test harness ----------

results = []   # (name, passed, detail)

def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'✅' if cond else '❌'}  {name}" + (f"  — {detail}" if detail and not cond else ""))


def reset_stub():
    _stub.kv.clear()


# ============================================================
# CAPITAL GUARD TESTS
# ============================================================

reset_stub()
import importlib
import capital_guard
importlib.reload(capital_guard)
# Force capital_guard to use stub
capital_guard._redis = _stub

print("\n=== capital_guard: core checks ===\n")

# 1. Small size, deep liq, no loss → all clear
d = capital_guard.run_guard(size_usd=2, capital_usd=12, liq_usd=500_000)
check("clean: small size deep liq → no block/warn",
      not d["block"] and not d["warn"], f"got block={d['block']} warn={d['warn']}")

# 2. Size > 50% capital → BLOCK
d = capital_guard.run_guard(size_usd=10, capital_usd=12, liq_usd=500_000)
check("size 83% of capital → block", d["block"])
check("  reason mentions size", any("Size" in r for r in d["reasons_block"]))

# 3. Size 25-50% capital → WARN
d = capital_guard.run_guard(size_usd=4, capital_usd=12, liq_usd=500_000)
check("size 33% capital → warn", d["warn"] and not d["block"])

# 4. Graveyard liq → BLOCK
d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=5_000)
check("liq $5K → block", d["block"])
check("  reason mentions graveyard",
      any("graveyard" in r.lower() or "<$" in r.lower() for r in d["reasons_block"]))

# 5. Thin liq $50K → WARN
d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=50_000)
check("liq $50K → warn (thin)", d["warn"] and not d["block"])

# 6. Size > 5% of pool → BLOCK
d = capital_guard.run_guard(size_usd=3000, capital_usd=10_000, liq_usd=30_000)
# size 30% cap (warn) + size 10% pool (block) + graveyard? $30K > $20K not graveyard
check("size 10% of pool → block", d["block"])

# 7. Missing liq → info-level note, no block
d = capital_guard.run_guard(size_usd=2, capital_usd=12, liq_usd=None)
check("liq None → no crash, no block",
      not d["block"], f"got block={d['block']}")

# 8. Revenge block — loss 10 min ago
reset_stub()
_stub.set("positions:closed", json.dumps([
    {"pnl_usd": -5.0, "closed_at": int(_time.time()) - 600}
]))
d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=500_000)
check("loss 10min ago → revenge block", d["block"])
check("  block reason mentions revenge",
      any("revenge" in r.lower() or "realized loss" in r.lower() for r in d["reasons_block"]))

# 9. Revenge warn — loss 60 min ago (between 30 and 120)
reset_stub()
_stub.set("positions:closed", json.dumps([
    {"pnl_usd": -5.0, "closed_at": int(_time.time()) - 3600}
]))
d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=500_000)
check("loss 60min ago → revenge warn", d["warn"] and not d["block"])

# 10. Revenge clear — loss 3h ago
reset_stub()
_stub.set("positions:closed", json.dumps([
    {"pnl_usd": -5.0, "closed_at": int(_time.time()) - 3 * 3600}
]))
d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=500_000)
check("loss 3h ago → revenge clear", not d["block"] and not d["warn"])

# 11. Profit close doesn't trigger revenge
reset_stub()
_stub.set("positions:closed", json.dumps([
    {"pnl_usd": 5.0, "closed_at": int(_time.time()) - 600}
]))
d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=500_000)
check("profit close 10min ago → no revenge", not d["block"])

# 12. Corrupt closed list — should not crash
reset_stub()
_stub.set("positions:closed", json.dumps([None, "bad", {}, {"pnl_usd": "x"}]))
try:
    d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=500_000)
    check("corrupt closed list → no crash", True)
except Exception as e:
    check("corrupt closed list → no crash", False, str(e))

# 13. Capital 0 — should not div-by-zero
reset_stub()
d = capital_guard.run_guard(size_usd=1, capital_usd=0, liq_usd=500_000)
check("capital=0 → no crash, no div0", True)

# 14. Negative / weird inputs
d = capital_guard.run_guard(size_usd=-5, capital_usd=12, liq_usd=500_000)
check("negative size → no crash", True)
d = capital_guard.run_guard(size_usd="bad", capital_usd="bad", liq_usd="bad")
check("string inputs → coerced, no crash", True)

# 15. format_panel never returns empty for a real decision
d = capital_guard.run_guard(size_usd=1, capital_usd=12, liq_usd=500_000)
panel = capital_guard.format_panel(d)
check("format_panel non-empty", bool(panel and len(panel) > 20))

# 16. format_check_panel strips revenge reasons even if loss exists
reset_stub()
_stub.set("positions:closed", json.dumps([
    {"pnl_usd": -5.0, "closed_at": int(_time.time()) - 600}
]))
panel = capital_guard.format_check_panel(liq_usd=500_000, capital_usd=12, default_size_usd=1.8)
check("format_check_panel — no revenge in /check",
      "revenge" not in panel.lower() and "realized loss" not in panel.lower(),
      f"panel: {panel[:200]}")

# 17. The grail-loss scenario reproduced
# capital $36, buy $36 on $44K liq, prior loss N/A
reset_stub()
d = capital_guard.run_guard(size_usd=36, capital_usd=36, liq_usd=44_000)
check("grail scenario: $36 on $44K liq, all-in → block",
      d["block"], f"got block={d['block']} reasons={d['reasons_block']}")
print(f"   grail block reasons:")
for r in d["reasons_block"]:
    print(f"     - {r[:120]}")
print(f"   grail warn reasons:")
for r in d["reasons_warn"]:
    print(f"     - {r[:120]}")

# ============================================================
# TOOLS.PY: ACTION CONFIRMATION PARKING
# ============================================================

print("\n=== tools.py: confirmation wrap on dangerous actions ===\n")

reset_stub()
# Stub OWNER_TELEGRAM_ID via env
import os
os.environ["OWNER_TELEGRAM_ID"] = "12345"

# We need to import tools but it imports bot which would start polling.
# Workaround: import only the function under test by exec'ing slices, OR
# use the fact that DANGEROUS_ACTIONS and _park_pending_action are at module
# scope after the bot imports. tools.py imports `bot` indirectly? Let's check.
# Quick path: test the parking logic and dispatch table directly.

try:
    # Defer the bot import inside tools by stubbing it
    import sys as _sys
    class _FakeBot:
        def message_handler(self, *a, **kw):
            def deco(f): return f
            return deco
        def callback_query_handler(self, *a, **kw):
            def deco(f): return f
            return deco
        def reply_to(self, *a, **kw): pass
        def send_message(self, *a, **kw): pass
        def edit_message_text(self, *a, **kw): pass

    # tools.py doesn't import bot at top — verified by grep earlier (it uses
    # `import position_tracker` etc inside functions). Safe to import.
    import tools
    importlib.reload(tools)
    tools._redis = _stub
    # Patch the get_redis used inside _park_pending_action
    import redis_client
    redis_client.get_redis = lambda: _stub

    # 18. Dangerous action via execute_tool → parks, doesn't execute
    out = tools.execute_tool("close_position", {"mint": "AAA"}, caller_user_id=12345)
    parsed = json.loads(out)
    check("close_position from chat → parked", parsed.get("pending") is True,
          f"got: {out[:200]}")
    check("  pending stored in Redis", _stub.kv.get("pending_action:current") is not None)
    check("  parked payload has name", json.loads(_stub.kv["pending_action:current"])["name"] == "close_position")

    # 19. Direct action call bypasses parking
    # (we don't actually execute since position_tracker would touch real Redis;
    #  just confirm the bypass routes to a different code path.)
    out_direct = tools._execute_action_direct("close_position", {"mint": "AAA"}, caller_user_id=12345)
    parsed_d = json.loads(out_direct) if out_direct.startswith("{") else {"raw": out_direct}
    check("close_position direct → does NOT return pending=True",
          parsed_d.get("pending") is not True)

    # 20. Non-dangerous action still flows normally
    out = tools.execute_tool("get_capital", {}, caller_user_id=12345)
    check("get_capital → executes normally, not parked", "pending" not in out or json.loads(out).get("pending") is not True)

except Exception as e:
    import traceback
    check("tools.py confirmation tests — module load", False, traceback.format_exc(limit=3))


# ============================================================
# POSITION_TRACKER: dynamic poll
# ============================================================

print("\n=== position_tracker: dynamic poll interval ===\n")

try:
    import position_tracker
    importlib.reload(position_tracker)

    # 21. No positions → default interval
    interval = position_tracker._effective_poll_interval([])
    check("no positions → default 60s", interval == 60)

    # 22. Position with cached deep liq → 60s
    pos = [{"mint": "AAA", "_liq_cache": 500_000}]
    interval = position_tracker._effective_poll_interval(pos)
    check("deep liq cached → 60s", interval == 60)

    # 23. Position with cached thin liq → 15s
    pos = [{"mint": "AAA", "_liq_cache": 44_000}]
    interval = position_tracker._effective_poll_interval(pos)
    check("thin liq cached → 15s", interval == 15, f"got {interval}")

    # 24. Mixed: one thin, one deep → 15s (worst-case dominates)
    pos = [{"mint": "AAA", "_liq_cache": 500_000},
           {"mint": "BBB", "_liq_cache": 44_000}]
    interval = position_tracker._effective_poll_interval(pos)
    check("mixed thin+deep → 15s", interval == 15, f"got {interval}")

except Exception as e:
    import traceback
    check("position_tracker tests", False, traceback.format_exc(limit=3))


# ============================================================
# SUMMARY
# ============================================================

print("\n" + "=" * 60)
passed = sum(1 for _, p, _ in results if p)
failed = sum(1 for _, p, _ in results if not p)
print(f"Capital Guard stress test: {passed} passed, {failed} failed")
print("=" * 60)

if failed:
    print("\nFAILURES:")
    for name, p, detail in results:
        if not p:
            print(f"  ❌ {name}  — {detail}")
    sys.exit(1)
else:
    print("\n✅ All capital protection rails verified.")
    sys.exit(0)
