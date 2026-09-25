"""Outcome-validation, fee-accounting, and lifecycle tests (no network).

Merged from the Codex review with all real addresses neutralized:
every address here is freshly generated with Wallet.create() and means
nothing. These tests cover the merged behavior — validated_outcome,
Hook return-string decoding, fee-only failure accounting, pending
reservations surviving age-based pruning, and the payload/resume
lifecycle hooks (bind_payload / pending_for_proposal).
"""
import os
import sys
import time
import tempfile
import types
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import xahau_common as C

try:
    from xrpl.wallet import Wallet
except Exception:  # pragma: no cover - xrpl-py is a hard dependency
    Wallet = None

ACCT = Wallet.create().classic_address if Wallet else "r" + "x" * 25
HOOK_ACCT = Wallet.create().classic_address if Wallet else "r" + "y" * 25

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name}")


def txid(s):
    return s * 64


def validated(success=True, result="tesSUCCESS", mismatch=False):
    """Build a validated ledger tx dict from a proposed tx."""
    tx = {"Account": ACCT, "Destination": HOOK_ACCT, "Amount": "100",
          "Fee": "15000", "Sequence": 3, "TransactionType": "Payment"}
    ledger = dict(tx)
    if mismatch:
        ledger["Amount"] = "101"
    ledger["validated"] = True
    ledger["meta"] = {"TransactionResult": result,
                      "delivered_amount": "100"}
    return tx, ledger


def run():
    tx, ledger = validated()
    state, result, problems = C.validated_outcome(tx, ledger)
    check("validated_outcome success", state == "success" and not problems)

    tx, ledger = validated(mismatch=True)
    state, result, problems = C.validated_outcome(tx, ledger)
    check("validated_outcome mismatch", state == "mismatch" and problems)

    tx, ledger = validated(success=False, result="tecHOOK_REJECTED")
    state, result, problems = C.validated_outcome(tx, ledger)
    check("validated_outcome failed", state == "failed" and result == "tecHOOK_REJECTED")

    tx, ledger = validated()
    ledger["validated"] = False
    state, result, problems = C.validated_outcome(tx, ledger)
    check("validated_outcome pending", state == "pending")

    # Fee-only spend accounting for a validated failure.
    with tempfile.TemporaryDirectory() as tmp:
        tracker = C.SpentTracker(os.path.join(tmp, "spent.json"),
                                 os.path.join(tmp, "spent.lock"))
        rid = tracker.try_reserve({"XAH": C.drops_to_xah("100000")},
                                  {"spend_limits": {"XAH": {"per_tx": "10",
                                                            "per_day": "100"}}})[1]
        check("reservation recorded", rid is not None)
        tracker.bind_reservation(rid, txid("c"), 900)
        tracker.fail_tx(txid("c"), "15000")
        totals = tracker._totals(tracker._load()["entries"])
        check("failure keeps fee only",
              totals["XAH"] == C.drops_to_xah("15000"))

    # Hook return strings decode cleanly, empty rows ignored.
    message = "NFT :: Error :: Hook rejects 0.1 XAH, expected 36 XAH."
    ledger = {"meta": {"HookExecutions": [
        {"HookExecution": {"HookAccount": HOOK_ACCT,
                           "HookReturnString": message.encode().hex() + "00"}},
        {"HookExecution": {"HookAccount": HOOK_ACCT, "HookReturnString": ""}}]}}
    decoded = C.hook_messages(ledger)
    check("hook return string decoded",
          decoded == [(HOOK_ACCT, message)])

    # Pending reservations survive age-based pruning.
    with tempfile.TemporaryDirectory() as tmp:
        tracker = C.SpentTracker(os.path.join(tmp, "spent.json"),
                                 os.path.join(tmp, "spent.lock"))
        old = {"rid": "old1", "ts": int(time.time()) - 90000,
               "asset": "XAH", "amount": "1.0", "status": "pending",
               "tx_hash": None, "last_ledger": None}
        pruned = C.SpentTracker._prune([old], int(time.time()))
        check("pending reservations never expire", pruned == [old])
        old_done = dict(old); old_done["status"] = "confirmed"
        pruned = C.SpentTracker._prune([old_done], int(time.time()))
        check("old confirmed entries still prune", pruned == [])

    # Payload lifecycle: bind a uuid to a reservation, find it by proposal.
    with tempfile.TemporaryDirectory() as tmp:
        tracker = C.SpentTracker(os.path.join(tmp, "spent.json"),
                                 os.path.join(tmp, "spent.lock"))
        proposal_hash = "ab" * 32
        _, rid = tracker.try_reserve(
            {"XAH": Decimal("1.0")},
            {"spend_limits": {"XAH": {"per_tx": "10", "per_day": "100"}}})
        tracker.bind_payload(rid, proposal_hash, "test-uuid")
        pending = tracker.pending_for_proposal(proposal_hash)
        check("bind_payload + pending_for_proposal",
              len(pending) == 1 and pending[0]["payload_uuid"] == "test-uuid")

    # bind_reservation does not confuse two proposals with the same uuid.
    with tempfile.TemporaryDirectory() as tmp:
        tracker = C.SpentTracker(os.path.join(tmp, "spent.json"),
                                 os.path.join(tmp, "spent.lock"))
        limits = {"spend_limits": {"XAH": {"per_tx": "10", "per_day": "100"}}}
        _, first = tracker.try_reserve({"XAH": Decimal("1.0")}, limits)
        _, second = tracker.try_reserve({"XAH": Decimal("1.0")}, limits)
        tracker.bind_payload(first, "ab" * 32, "test-uuid")
        tracker.bind_payload(second, "cd" * 32, "test-uuid")
        tracker.bind_reservation(second, txid("d"), 900)
        pending = tracker.pending_for_proposal("ab" * 32)
        check("rid scoping on shared uuid",
              len(pending) == 1 and pending[0]["rid"] == first
              and pending[0]["tx_hash"] is None)

    # XAH amount precision: more than six decimals is rejected.
    check("xah_to_drops rejects 7 decimals", _xah_precise())
    check("xah_to_drops accepts 6 decimals",
          C.xah_to_drops(__import__("decimal").Decimal("1.000001")) == "1000001")

    # Proposal hash prefixes must be at least 12 hex chars.
    try:
        C.load_proposal("abc")
        check("short proposal prefix rejected", False)
    except SystemExit:
        check("short proposal prefix rejected", True)

    # Real base58 address validation.
    check("generated address validates", C.is_valid_classic_address(ACCT))
    check("garbage address rejected", not C.is_valid_classic_address("not-an-address"))

    # Env override for the data directory (test isolation).
    check("XAHAU_DATA_DIR respected",
          os.environ.get("XAHAU_DATA_DIR") is None
          or str(C.XAHAU_DIR) == os.environ["XAHAU_DATA_DIR"])


def _xah_precise():
    from decimal import Decimal
    try:
        C.xah_to_drops(Decimal("0.0000001"))
        return False
    except SystemExit:
        return True


if __name__ == "__main__":
    run()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
