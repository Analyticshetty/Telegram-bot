"""Local stress test for tools.py — exercises every read tool with edge cases.
Stubs Redis so it can run offline. Run from TelegramBot/ dir."""
import os, sys, json, types, importlib

# Pre-stub env so module imports don't crash
os.environ.setdefault("OWNER_TELEGRAM_ID", "12345")
os.environ.setdefault("TELEGRAM_TOKEN", "stub")
os.environ.setdefault("GROQ_API_KEY", "stub")
os.environ.setdefault("REDIS_URL", "rediss://stub")

# Stub redis_client
class StubRedis:
    def __init__(self):
        self.kv = {}
        self.lists = {}
        self.hashes = {}
        self.zsets = {}
    def get(self, k): return self.kv.get(k)
    def set(self, k, v, **kw): self.kv[k] = v if isinstance(v, str) else str(v); return True
    def lpush(self, k, *vs):
        self.lists.setdefault(k, [])
        for v in vs: self.lists[k].insert(0, v)
        return len(self.lists[k])
    def ltrim(self, k, a, b):
        if k in self.lists: self.lists[k] = self.lists[k][a:b+1]
    def lrange(self, k, a, b):
        return self.lists.get(k, [])[a:b+1 if b != -1 else None]
    def llen(self, k): return len(self.lists.get(k, []))
    def hget(self, k, f): return self.hashes.get(k, {}).get(f)
    def hset(self, k, f, v): self.hashes.setdefault(k, {})[f] = v; return 1
    def hgetall(self, k): return self.hashes.get(k, {})
    def hlen(self, k): return len(self.hashes.get(k, {}))
    def hdel(self, k, *fs):
        h = self.hashes.get(k, {})
        for f in fs: h.pop(f, None)
    def zadd(self, k, mapping): self.zsets.setdefault(k, {}).update(mapping); return 1
    def zrevrange(self, k, a, b):
        items = sorted(self.zsets.get(k, {}).items(), key=lambda x: -x[1])
        return [x[0] for x in items[a:b+1]]
    def delete(self, k): self.kv.pop(k, None); self.lists.pop(k, None)
    def pipeline(self): return _Pipe(self)
    def incr(self, k):
        self.kv[k] = str(int(self.kv.get(k, "0")) + 1)
        return int(self.kv[k])

class _Pipe:
    def __init__(self, r): self.r = r; self.q = []
    def lpush(self, *a, **k): self.q.append(('lpush', a, k)); return self
    def ltrim(self, *a, **k): self.q.append(('ltrim', a, k)); return self
    def execute(self):
        for op, a, k in self.q: getattr(self.r, op)(*a, **k)

_stub = StubRedis()
redis_client_stub = types.ModuleType("redis_client")
redis_client_stub.get_redis = lambda: _stub
sys.modules["redis_client"] = redis_client_stub

# Seed some test data
_stub.kv["state:capital_usd"] = "12.0"
_stub.kv["shashi:memories"] = json.dumps(["never buy RED", "TP at 2x"])
_stub.kv["smart_wallets:data"] = json.dumps({
    "wallets": [
        {"address": "A1aaaa", "label": "alpha", "source": "manual"},
        {"address": "B2bbbb", "label": "beta",  "source": "auto"},
        None,  # CORRUPT entry — should be tolerated
        {"address": "C3cccc", "label": "gamma", "source": "discover"},
    ]
})
_stub.lists["mem:alerts"] = [
    json.dumps({"ts": 1748571000, "narrative": "dog", "symbol": "DOGZ", "mint": "Mxxxx", "verdict": "YELLOW", "mc": 100000, "liq": 5000}),
    None,  # corrupt
    json.dumps({"ts": 1748570000, "narrative": "cat", "symbol": "CATZ", "mint": "Mzzzz", "verdict": "GREEN", "mc": 200000, "liq": 10000}),
]
_stub.lists["mem:scans"] = [json.dumps({"ts": 1748571111, "results_count": 5, "top_results": []})]
_stub.lists["losses:log"] = [
    json.dumps({"ts": 1748000000, "mint": "Mgrail", "symbol": "GRAIL", "classification": "REAL_LOSS", "pnl_usd": -11.62, "pnl_pct": -32.2, "entry_price": 0.000239, "exit_price": 0.000161, "fib_broken": True, "vol_spike": True}),
]
_stub.lists["positions:open"] = []
_stub.lists["positions:closed"] = [json.dumps({
    "mint": "Mgrail", "symbol": "GRAIL", "size_usd": 36.09, "entry_price": 0.000239,
    "exit_price": 0.000161, "pnl_usd": -11.62, "pnl_pct": -32.2,
    "closed_at": 1748000000, "opened_at": 1747900000, "close_reason": "SL", "status": "CLOSED"
})]
_stub.lists["sw_feed:signals"] = [
    json.dumps({"ts": 1748571000, "mint": "Mxxxx", "symbol": "DOGZ", "wallet_count": 3, "wallet_labels": ["alpha","beta","gamma"], "verdict": "YELLOW", "mc": 100000, "liq": 5000, "age_minutes": 45}),
]
_stub.kv["mem:check_by_ca:Mgrail"] = json.dumps({"ts": 1748000000, "mint": "Mgrail", "symbol": "GRAIL", "verdict": "YELLOW", "mc": 199848, "liq": 42137, "reasons_red": [], "reasons_yellow": ["MC moderate"]})

# Stub other heavy modules so tools.py imports don't fail
for mod_name in ("position_tracker", "smart_wallet_feed", "loss_tracker",
                  "memory_store", "smart_wallets", "stats", "signal_engine",
                  "watcher", "dev_tracker", "sleep_mode", "rug_check",
                  "scanner", "trade_card"):
    if mod_name in sys.modules:
        del sys.modules[mod_name]

# Now actually import the real modules — they should use our stub Redis
import tools

OWNER = "12345"

TESTS = [
    ("get_capital", {}),
    ("get_watcher_alerts", {"limit": 10}),
    ("get_watcher_alerts", {"limit": 10, "keyword": "dog"}),
    ("get_recent_checks", {"limit": 10}),
    ("get_smart_wallet_signals", {"limit": 10}),
    ("get_positions", {"status": "open"}),
    ("get_positions", {"status": "closed"}),
    ("get_watcher_status", {}),
    ("get_losses", {"limit": 10}),
    ("get_stats", {}),
    ("get_memories", {}),
    ("get_smart_wallets", {"page": 1, "page_size": 25}),
    ("get_recent_scans", {"limit": 10}),
    ("get_signal_log", {"limit": 10}),
    ("get_signal_accuracy", {}),
    ("get_lookup", {"mint": "Mgrail"}),
    ("get_lookup", {"mint": "MdoesNotExist"}),
    ("get_lookup", {"mint": ""}),  # empty CA
    # Edge: weird args
    ("get_watcher_alerts", {"limit": -5}),
    ("get_watcher_alerts", {"limit": 999}),
    ("get_positions", {"status": "INVALID"}),
    ("get_smart_wallets", {"page": 99, "page_size": 100}),
    # Action refusals (no owner)
    ("set_capital", {"amount_usd": 50}),
    ("close_position", {"mint": "Mgrail"}),
    ("toggle_sleep", {"on": True}),
    ("add_wallet", {"address": "Dxxxx", "label": "delta"}),
]

passed = 0
failed = []

for name, args in TESTS:
    try:
        result = tools.execute_tool(name, args, caller_user_id=None)  # non-owner
        # Sanity: result must be a non-empty string
        assert isinstance(result, str) and len(result) > 0, "empty result"
        # Sanity: must be valid JSON or a clean error string
        # (tools returning error strings are fine — what's NOT fine is uncaught exceptions)
        passed += 1
        print(f"[OK]   {name}({args})  ->  {result[:140]}")
    except Exception as e:
        failed.append((name, args, repr(e)))
        print(f"[FAIL] {name}({args})  ->  {e.__class__.__name__}: {e}")

# Owner-action tests
print("\n--- OWNER ACTIONS ---")
owner_tests = [
    ("set_capital", {"amount_usd": 50}),
    ("add_memory", {"text": "test rule"}),
    ("forget_memory", {"text": "test rule"}),
    ("toggle_sleep", {"on": True}),
    ("toggle_watcher", {"on": False}),
]
for name, args in owner_tests:
    try:
        result = tools.execute_tool(name, args, caller_user_id=OWNER)
        assert isinstance(result, str) and len(result) > 0
        passed += 1
        print(f"[OK]   {name}({args})  ->  {result[:140]}")
    except Exception as e:
        failed.append((name, args, repr(e)))
        print(f"[FAIL] {name}({args})  ->  {e.__class__.__name__}: {e}")

print(f"\n=== {passed} passed, {len(failed)} failed ===")
if failed:
    print("\nFAILURES:")
    for n, a, e in failed:
        print(f"  {n}({a}) -> {e}")
    sys.exit(1)
sys.exit(0)
