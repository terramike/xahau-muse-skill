#!/usr/bin/env python3
"""Recovery-path tests — no network.

- sweep_pending: validated tec failure -> fee-only accounting via the
  SAME helper fail_tx uses; tesSUCCESS -> confirmed; txnNotFound past
  LastLedgerSequence -> released; transient/unvalidated -> stays pending.
- Corrupt spend ledger: try_reserve raises CorruptStateError (fail
  closed); reset-spend archives and resets; reset on a healthy ledger
  without --force refuses.
- Xaman 409 -> DuplicatePayloadError (distinct from other API errors).
- check_destination_hooks_live: the payload-creation hook re-check.

Run: python3 tests/test_recovery.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="xahau-recovery-test-")
os.environ["XAHAU_DATA_DIR"] = TMP

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, "/home/hatch/workspace/tools/xrpl-pkgs")
sys.path.insert(0, str(BIN))


def load(path, as_name):
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader(as_name, str(path))
    spec = importlib.util.spec_from_loader(as_name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = mod
    loader.exec_module(mod)
    return mod


C = load(BIN / "xahau_common.py", "xahau_common_recovery")
P = load(BIN / "xahau-payload", "xahau_payload_recovery")

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)


POLICY = {"spend_limits": {"XAH": {"per_tx": "1000", "per_day": "1000"},
                           "USD.rISS": {"per_tx": "1000",
                                        "per_day": "1000"}}}


def fresh_tracker():
    # wipe state between scenarios
    p = C.STATE_PATH
    if p.exists():
        p.unlink()
    for bak in p.parent.glob("state.corrupt.*.bak"):
        bak.unlink()
    return C.SpentTracker()


def seed_pending(tr, spends, txh, last_ledger=100):
    d, rid = tr.try_reserve(spends, POLICY)
    assert d == [], d
    tr.bind_reservation(rid, txh, last_ledger)
    tr.bind_payload(rid, "prophash-" + txh, "uuid-" + txh)
    return rid


def fake_rpc_factory(txdb, cur_ledger=200, transient=()):
    def rpc_fn(method, params=None):
        if method == "ledger":
            return {"ledger_index": cur_ledger}
        if method == "tx":
            h = params[0]["transaction"]
            if h in transient:
                raise RuntimeError("boom: connection reset")
            if h in txdb:
                return txdb[h]
            raise RuntimeError("node error txnNotFound: transaction not found")
        raise AssertionError(method)
    return rpc_fn


def entries_by_tx(tr, txh):
    return [e for e in tr._load()["entries"] if e.get("tx_hash") == txh]


# ---- 1. sweep: validated tec failure -> fee-only, IOUs dropped ----
tr = fresh_tracker()
seed_pending(tr, {"XAH": Decimal("5"), "USD.rISS": Decimal("10")}, "TECTX")
rpc_fn = fake_rpc_factory({
    "TECTX": {"validated": True, "Fee": "15",
              "meta": {"TransactionResult": "tecHOOK_REJECTED"}}})
tr.sweep_pending(rpc_fn)
ents = entries_by_tx(tr, "TECTX")
native = [e for e in ents if e["asset"] == "XAH"]
iou = [e for e in ents if e["asset"] == "USD.rISS"]
check("sweep tec: native entry kept as fee-only confirmed",
      len(native) == 1 and native[0]["status"] == "confirmed"
      and Decimal(native[0]["amount"]) == C.drops_to_xah("15"))
check("sweep tec: IOU entry dropped (nothing delivered)", iou == [])

# ---- 2. sweep: tesSUCCESS -> confirmed in full ----
tr = fresh_tracker()
seed_pending(tr, {"XAH": Decimal("5")}, "OKTX")
rpc_fn = fake_rpc_factory({
    "OKTX": {"validated": True, "Fee": "15",
             "meta": {"TransactionResult": "tesSUCCESS"}}})
tr.sweep_pending(rpc_fn)
ents = entries_by_tx(tr, "OKTX")
check("sweep tesSUCCESS: confirmed in full",
      len(ents) == 1 and ents[0]["status"] == "confirmed"
      and Decimal(ents[0]["amount"]) == Decimal("5"))

# ---- 3. sweep: txnNotFound past last ledger -> released ----
tr = fresh_tracker()
seed_pending(tr, {"XAH": Decimal("5")}, "GONETX")
tr.sweep_pending(fake_rpc_factory({}))
check("sweep txnNotFound past last ledger: released",
      entries_by_tx(tr, "GONETX") == [])

# ---- 4. sweep: transient error -> stays pending ----
tr = fresh_tracker()
seed_pending(tr, {"XAH": Decimal("5")}, "FLAKY")
tr.sweep_pending(fake_rpc_factory({}, transient=("FLAKY",)))
ents = entries_by_tx(tr, "FLAKY")
check("sweep transient error: stays pending",
      len(ents) == 1 and ents[0]["status"] == "pending")

# ---- 5. sweep: unvalidated tx result -> stays pending ----
tr = fresh_tracker()
seed_pending(tr, {"XAH": Decimal("5")}, "UNVAL")
rpc_fn = fake_rpc_factory({
    "UNVAL": {"validated": False, "Fee": "15",
              "meta": {"TransactionResult": "tesSUCCESS"}}})
tr.sweep_pending(rpc_fn)
ents = entries_by_tx(tr, "UNVAL")
check("sweep unvalidated: stays pending",
      len(ents) == 1 and ents[0]["status"] == "pending")

# ---- 6. sweep: last ledger not yet passed -> untouched ----
tr = fresh_tracker()
seed_pending(tr, {"XAH": Decimal("5")}, "FUTURE", last_ledger=500)
tr.sweep_pending(fake_rpc_factory({}))
ents = entries_by_tx(tr, "FUTURE")
check("sweep future last_ledger: untouched",
      len(ents) == 1 and ents[0]["status"] == "pending")

# ---- 7. corrupt ledger fails closed ----
tr = fresh_tracker()
C.STATE_PATH.write_text("{not valid json")
check("corrupt ledger: health() == 'corrupt'",
      tr.health() == "corrupt")
try:
    tr.try_reserve({"XAH": Decimal("1")}, POLICY)
    check("corrupt ledger: try_reserve raises", False)
except C.CorruptStateError:
    check("corrupt ledger: try_reserve raises CorruptStateError", True)
try:
    tr.sweep_pending(fake_rpc_factory({}))
    check("corrupt ledger: sweep_pending raises", False)
except C.CorruptStateError:
    check("corrupt ledger: sweep_pending raises CorruptStateError", True)

# ---- 8. reset-spend archives and resets ----
prev = tr.reset_ledger()
baks = list(C.STATE_PATH.parent.glob("state.corrupt.*.bak"))
check("reset-spend: reports prior state", prev == "corrupt")
check("reset-spend: corrupt file archived, not deleted",
      len(baks) == 1 and baks[0].read_text() == "{not valid json")
check("reset-spend: ledger healthy-empty afterwards", tr.health() == "ok")
d, _ = tr.try_reserve({"XAH": Decimal("1")}, POLICY)
check("reset-spend: new payloads allowed after reset", d == [])

# ---- 9. reset on healthy ledger refuses without --force ----
tr2 = fresh_tracker()
tr2.try_reserve({"XAH": Decimal("1")}, POLICY)
try:
    tr2.reset_ledger()
    check("healthy reset without force: refuses", False)
except C.CorruptStateError:
    check("healthy reset without force: refuses", True)
prev = tr2.reset_ledger(force=True)
check("healthy reset with force: works", prev == "ok"
      and tr2.health() == "ok")

# ---- 10. 409 -> DuplicatePayloadError, distinct from other errors ----
check("DuplicatePayloadError is a ValueError",
      issubclass(P.DuplicatePayloadError, ValueError))


class FakeResp:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text

    def json(self):
        return {"uuid": "fake"}


import types  # noqa: E402

fake_requests = types.ModuleType("requests")
real_requests = sys.modules.get("requests")


def with_status(code):
    fake_requests.post = lambda *a, **k: FakeResp(code)
    fake_requests.get = lambda *a, **k: FakeResp(200)
    sys.modules["requests"] = fake_requests


try:
    with_status(409)
    try:
        P.api_post("", "k", "s", {})
        check("409 raises DuplicatePayloadError", False)
    except P.DuplicatePayloadError:
        check("409 raises DuplicatePayloadError", True)
    except ValueError:
        check("409 raises DuplicatePayloadError (plain ValueError!)", False)

    with_status(400)
    try:
        P.api_post("", "k", "s", {})
        check("400 raises plain ValueError", False)
    except P.DuplicatePayloadError:
        check("400 raises plain ValueError (got Duplicate!)", False)
    except ValueError:
        check("400 raises plain ValueError", True)
finally:
    if real_requests is not None:
        sys.modules["requests"] = real_requests
    else:
        sys.modules.pop("requests", None)

# ---- 11. payload-creation hook re-check ----


def hook_rpc_factory(hooked):
    def rpc_fn(method, params=None):
        assert method == "account_objects"
        return {"account_objects": [{}] if hooked else []}
    return rpc_fn


pay = {"TransactionType": "Payment", "Destination": "rDEST"}
check("hook re-check: no hooks -> pass",
      C.check_destination_hooks_live(hook_rpc_factory(False), pay, {}) == [])
check("hook re-check: hooks, unattested -> denial",
      len(C.check_destination_hooks_live(hook_rpc_factory(True), pay, {})) == 1)
check("hook re-check: hooks, attested -> pass (warns)",
      C.check_destination_hooks_live(hook_rpc_factory(True), pay,
                                     {"allow_hooks": True}) == [])
remit = {"TransactionType": "Remit", "Destination": "rDEST"}
check("hook re-check: Remit covered",
      len(C.check_destination_hooks_live(hook_rpc_factory(True), remit,
                                         {})) == 1)
claim = {"TransactionType": "ClaimReward"}
check("hook re-check: ClaimReward untouched",
      C.check_destination_hooks_live(hook_rpc_factory(True), claim, {}) == [])


def boom_rpc(method, params=None):
    raise RuntimeError("node down")


check("hook re-check: unreadable state fails closed",
      len(C.check_destination_hooks_live(boom_rpc, pay, {})) == 1)

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} recovery tests passed")
sys.exit(1 if FAIL else 0)
