#!/usr/bin/env python3
"""P1 adversarial logic tests — no network. Run: python3 tests/test_p1.py

Covers: envelope hash-binding (tamper -> reject), envelope invariants
(account/NetworkID/action mismatch, future created_at), policy gates
(network lock, TTL, tx-type allowlist, field schemas, amounts, spend
limits incl. rolling-24h, destination allowlist, fee cap), spend-tracker
atomicity, and canonical-encoding determinism.

Note: xrpl-py 5.2.0's binary codec does NOT know Xahau-only tx types —
envelopes bind the canonical-JSON digest instead (see xahau_common).
"""
import importlib.util
import os
os.environ.setdefault("XAHAU_DATA_DIR",
                       __import__("tempfile").mkdtemp(prefix="xahau-test-"))
import json
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
sys.path.insert(0, "/home/hatch/workspace/tools/xrpl-pkgs")


def load(path, as_name):
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader(as_name, str(path))
    spec = importlib.util.spec_from_loader(as_name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = mod
    loader.exec_module(mod)
    return mod


C = load(BIN / "xahau_common.py", "xahau_common")

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def raises(fn):
    try:
        fn()
    except C.ProposalError:
        return True
    except SystemExit:
        return True
    return False


from xrpl.wallet import Wallet
ACCT = Wallet.create().classic_address
DEST = Wallet.create().classic_address
ISS = Wallet.create().classic_address


def pay_tx(**kw):
    tx = {"Account": ACCT, "TransactionType": "Payment",
          "Destination": DEST, "Amount": C.xah_to_drops(Decimal("5")),
          "Fee": "10", "Sequence": 1, "LastLedgerSequence": 100,
          "NetworkID": 21338}
    tx.update(kw)
    return tx


def policy(**kw):
    p = {"policy_version": C.POLICY_VERSION,
         "network_lock": "xahau-testnet",
         "max_fee_drops": 10000,
         "spend_limits": {"XAH": {"per_tx": "25", "per_day": "100"}},
         "destination_allowlist": [],
         "proposal_ttl_seconds": 86400,
         "allowed_tx_types": list(C.DEFAULT_ALLOWED_TX_TYPES)}
    p.update(kw)
    return p


def resign(path, **overrides):
    """Rewrite envelope fields and re-sign the hash (simulates a
    legitimately-built envelope, e.g. an old proposal — NOT tampering)."""
    e = json.loads(path.read_text())
    e.update(overrides)
    core = {k: e[k] for k in C.ENVELOPE_HASH_KEYS}
    e["proposal_hash"] = __import__("hashlib").sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    path.write_text(json.dumps(e, indent=2))
    path.rename(path.parent / (e["proposal_hash"] + ".json"))
    return path.parent / (e["proposal_hash"] + ".json")


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    C.XAHAU_DIR = tmp
    C.PROPOSALS_DIR = tmp / "proposals"
    C.POLICY_PATH = tmp / "policy.json"
    C.STATE_PATH = tmp / "state.json"
    C.STATE_LOCK_PATH = tmp / "state.lock"
    C.AUDIT_PATH = tmp / "audit.log"

    # ---- envelope hash-binding ----
    h, path = C.save_proposal(pay_tx(), "xahau-testnet", ACCT, "send")
    _backup = path.read_bytes()  # tamper tests must not corrupt the original

    def tampers(mutate):
        """Write mutated bytes to the real path, verify, then restore."""
        e = json.loads(_backup)
        mutate(e)
        path.write_text(json.dumps(e))
        try:
            C.verify_proposal(json.loads(path.read_text()), path)
            return False
        except C.ProposalError:
            return True
        finally:
            path.write_bytes(_backup)

    prop = json.loads(path.read_text())
    check("envelope roundtrip verifies",
          C.verify_proposal(prop, path)["Amount"] == C.xah_to_drops(Decimal("5")))

    check("tampered Amount rejected",
          tampers(lambda e: e["tx"].__setitem__(
              "Amount", C.xah_to_drops(Decimal("5000")))))

    def _net(e):
        e["network"] = "xahau-mainnet"
    check("tampered network rejected", tampers(_net))

    def bad_filename():
        p2 = tmp / "proposals" / ("0" * 64 + ".json")
        p2.write_bytes(_backup)
        C.verify_proposal(json.loads(p2.read_text()), p2)
    check("filename/hash mismatch rejected", raises(bad_filename))

    def _fmt(e):
        e["format"] = "xahau-proposal/0"
    check("wrong envelope format rejected", tampers(_fmt))

    def _pv(e):
        e["policy_version"] = 99
    check("stale policy version rejected", tampers(_pv))

    # ---- envelope invariants ----
    good = json.loads(path.read_text())
    C.verify_envelope_invariants(good, good["tx"], 21338)
    check("invariants pass on good envelope", True)

    def future_ts():
        e = dict(good); e["created_at"] = int(time.time()) + 99999
        C.verify_envelope_invariants(e, e["tx"], 21338)
    check("future created_at rejected", raises(future_ts))

    def no_netid():
        e = json.loads(json.dumps(good)); del e["tx"]["NetworkID"]
        C.verify_envelope_invariants(e, e["tx"], 21338)
    check("missing NetworkID rejected", raises(no_netid))

    def wrong_netid():
        e = json.loads(json.dumps(good)); e["tx"]["NetworkID"] = 21337
        C.verify_envelope_invariants(e, e["tx"], 21338)
    check("wrong NetworkID rejected", raises(wrong_netid))

    def wrong_account():
        e = json.loads(json.dumps(good)); e["account"] = DEST
        C.verify_envelope_invariants(e, e["tx"], 21338)
    check("envelope/tx account mismatch rejected", raises(wrong_account))

    def wrong_action():
        e = json.loads(json.dumps(good)); e["action"] = "claim"
        C.verify_envelope_invariants(e, e["tx"], 21338)
    check("action/type mismatch rejected", raises(wrong_action))

    def sig_material():
        e = json.loads(json.dumps(good))
        e["tx"]["TxnSignature"] = "00" * 64
        C.verify_envelope_invariants(e, e["tx"], 21338)
    check("signature material in proposal rejected", raises(sig_material))

    # ---- policy gates ----
    pol = policy(destination_allowlist=[
        {"address": DEST, "destination_tag": None}])
    check("good proposal passes policy", C.policy_check(good, good["tx"], pol) == [])

    e_main = json.loads(json.dumps(good)); e_main["network"] = "xahau-mainnet"
    check("mainnet proposal vs testnet lock denied",
          any("network_lock" in d for d in C.policy_check(e_main, e_main["tx"], pol)))

    old_path = resign(tmp / "proposals" / (h + ".json"),
                      created_at=int(time.time()) - 90001)
    old = json.loads(old_path.read_text())
    check("expired proposal denied",
          any("TTL" in d for d in C.policy_check(old, old["tx"], pol)))

    esc = pay_tx(TransactionType="EscrowCreate")
    check("disabled tx type (Escrow) rejected",
          any("allowlist" in d for d in C.validate_tx_shape(esc, pol["allowed_tx_types"])))

    smug = pay_tx(); smug["Paths"] = []
    check("smuggled Paths field rejected",
          any("Paths" in d for d in C.validate_tx_shape(smug, pol["allowed_tx_types"])))

    mint = {"Account": ACCT, "TransactionType": "URITokenMint",
            "URI": "697066733A2F2F74657374", "Fee": "10", "Sequence": 1,
            "LastLedgerSequence": 100, "NetworkID": 21338}
    wide = pol["allowed_tx_types"] + ["URITokenMint"]
    check("URITokenMint shape accepted",
          C.validate_tx_shape(mint, wide) == [])
    mint_bad = dict(mint); mint_bad["Bogus"] = 1
    check("URITokenMint + bogus field rejected",
          C.validate_tx_shape(mint_bad, wide) != [])
    check("URITokenMint now implemented: in default allowlist",
          "URITokenMint" in C.DEFAULT_ALLOWED_TX_TYPES
          and C.validate_tx_shape(mint, C.DEFAULT_ALLOWED_TX_TYPES) == [])
    check("narrowed default allowlist refuses unimplemented tx types",
          "NFTokenMint" not in C.DEFAULT_ALLOWED_TX_TYPES
          and any("allowlist" in d
                  for d in C.validate_tx_shape(
                      dict(mint, TransactionType="NFTokenMint"),
                      C.DEFAULT_ALLOWED_TX_TYPES)))

    nan_tx = pay_tx(Amount="NaN")
    check("NaN amount rejected", C.validate_amounts(nan_tx) != [])

    iou_tx = pay_tx(Amount={"currency": "USD", "issuer": ISS, "value": "10"})
    check("unlisted IOU asset fail-closed",
          any("no spend limit" in d for d in C.policy_check(good, iou_tx, pol)))

    fee_tx = pay_tx(Fee="99999999")
    check("fee over max_fee_drops denied",
          any("max_fee_drops" in d for d in C.policy_check(good, fee_tx, pol)))

    pol_empty = policy()
    check("empty destination allowlist blocks payments",
          any("allowlist is empty" in d
              for d in C.policy_check(good, good["tx"], pol_empty)))

    pol_tag = policy(destination_allowlist=[
        {"address": DEST, "destination_tag": 7}])
    check("wrong destination tag denied",
          any("not allowlisted" in d
              for d in C.policy_check(good, good["tx"], pol_tag)))
    tagged = pay_tx(DestinationTag=7)
    e_tag = json.loads(json.dumps(good)); e_tag["tx"] = tagged
    check("correct destination tag passes",
          C.policy_check(e_tag, tagged, pol_tag) == [])

    # ---- spend tracker ----
    tr = C.SpentTracker()
    pol_roll = policy(spend_limits={"XAH": {"per_tx": "1000",
                                            "per_day": "100"}})
    d, rid = tr.try_reserve({"XAH": Decimal("60")}, pol_roll)
    check("reserve 60 XAH ok", d == [] and rid)
    d2, _ = tr.try_reserve({"XAH": Decimal("50")}, pol_roll)
    check("rolling-24h: 60+50 > 100 denied",
          any("per-day" in x for x in d2))
    d3, rid3 = tr.try_reserve({"XAH": Decimal("40")}, pol_roll)
    check("rolling-24h: 60+40 <= 100 ok", d3 == [])
    d4, _ = tr.try_reserve({"XAH": Decimal("26")}, pol)
    check("per-tx limit enforced", any("per-tx" in x for x in d4))
    d5, _ = tr.try_reserve({"FOO.rXYZ": Decimal("1")}, pol_roll)
    check("unlisted asset reservation fail-closed",
          any("no spend limit" in x for x in d5))
    tr.release_reservation(rid3)
    d6, _ = tr.try_reserve({"XAH": Decimal("40")}, pol_roll)
    check("released reservation frees budget", d6 == [])

    # ---- spend derivation ----
    s = C.tx_spends(pay_tx())
    check("Payment spends amount+fee",
          s == {"XAH": Decimal("5") + C.drops_to_xah("10")})
    claim = {"Account": ACCT, "TransactionType": "ClaimReward", "Fee": "10",
             "Sequence": 1, "LastLedgerSequence": 100, "NetworkID": 21338}
    check("ClaimReward spends fee only",
          C.tx_spends(claim) == {"XAH": C.drops_to_xah("10")})
    buy = {"Account": ACCT, "TransactionType": "URITokenBuy",
           "URITokenID": "0" * 64, "Amount": C.xah_to_drops(Decimal("2")),
           "Fee": "10", "Sequence": 1, "LastLedgerSequence": 100,
           "NetworkID": 21338}
    check("URITokenBuy spends price+fee",
          C.tx_spends(buy) == {"XAH": Decimal("2") + C.drops_to_xah("10")})

    # ---- canonical encoding determinism ----
    a = pay_tx(); b = dict(reversed(list(a.items())))
    check("canonical digest is key-order independent",
          C.tx_sha256(a) == C.tx_sha256(b))

    # ---- binary-codec sanity (classic types only) ----
    check("Payment binary-encodes",
          C.tx_binary_if_known(pay_tx()) is not None)
    oc = {"Account": ACCT, "TransactionType": "OfferCreate",
          "TakerPays": C.xah_to_drops(Decimal("1")),
          "TakerGets": {"currency": "USD", "issuer": ISS, "value": "2"},
          "Fee": "10", "Sequence": 1, "LastLedgerSequence": 100,
          "NetworkID": 21338}
    check("OfferCreate binary-encodes", C.tx_binary_if_known(oc) is not None)
    check("SetHook falls back to canonical JSON (codec lacks it)",
          C.tx_binary_if_known({"Account": ACCT, "TransactionType": "SetHook",
                                "CreateCode": "", "Fee": "10", "Sequence": 1,
                                "LastLedgerSequence": 100,
                                "NetworkID": 21338}) is None)

    # ---- missing codec dependency must be loud, never masked ----
    _saved_codec = sys.modules.pop("xrpl.core.binarycodec", None)
    sys.modules["xrpl.core.binarycodec"] = None  # forces ImportError on import
    try:
        try:
            C.tx_binary_if_known(pay_tx())
            _loud = False
        except ImportError:
            _loud = True
    finally:
        if _saved_codec is not None:
            sys.modules["xrpl.core.binarycodec"] = _saved_codec
        else:
            sys.modules.pop("xrpl.core.binarycodec", None)
    check("missing xrpl-py raises ImportError (not masked as unencodable)",
          _loud)

    # ---- Xahau fee selection: 3x median buffer; tx-specific probe ----
    check("choose_fee pays 3x the advertised median fee",
          C.choose_fee({"base_fee": "10", "median_fee": "5000"}) == 15000)
    check("choose_fee has no blind floor on a quiet network",
          C.choose_fee({"base_fee": "10", "median_fee": "1000"}) == 3000)
    check("choose_fee falls back to base fee on empty input",
          C.choose_fee({}) == 30)
    check("choose_fee scales 3x with a rising median fee",
          C.choose_fee({"base_fee": "10", "median_fee": "14189"}) == 42567)

    # ---- destination hook check (stubbed rpc: no network) ----
    _orig_rpc = C.rpc

    def _rpc_one_hook(network, method, params=None, timeout=20):
        assert method == "account_objects"
        assert params[0]["type"] == "hook"
        return {"account_objects": [{"HookHash": "00" * 32}]}
    C.rpc = _rpc_one_hook
    check("destination_hook_count counts installed hooks",
          C.destination_hook_count("xahau-testnet", DEST) == 1)

    def _rpc_no_hooks(network, method, params=None, timeout=20):
        return {"account_objects": []}
    C.rpc = _rpc_no_hooks
    check("destination_hook_count is 0 with no hooks",
          C.destination_hook_count("xahau-testnet", DEST) == 0)

    def _rpc_missing(network, method, params=None, timeout=20):
        raise RuntimeError("node error actNotFound: account not found")
    C.rpc = _rpc_missing
    check("destination_hook_count treats a missing account as 0 hooks",
          C.destination_hook_count("xahau-testnet", DEST) == 0)

    def _rpc_boom(network, method, params=None, timeout=20):
        raise RuntimeError("node error boom: exploded")
    C.rpc = _rpc_boom
    try:
        C.destination_hook_count("xahau-testnet", DEST)
        _closed = False
    except RuntimeError:
        _closed = True
    check("destination_hook_count fails closed on node errors", _closed)
    C.rpc = _orig_rpc

    # ---- hook diagnostics for tecHOOK_REJECTED (stubbed rpc: no network) ----
    _tx_hook = {"Account": ACCT, "Destination": DEST,
                "TransactionType": "Payment",
                "Amount": "100000", "Fee": "15000"}

    def _rpc_hook_diag(method, params=None):
        if method == "account_objects":
            if params[0]["account"] == DEST:
                return {"account_objects": [{"HookHash": "aa" * 32}]}
            return {"account_objects": []}
        if method == "account_tx":
            assert params[0]["account"] == DEST
            ok = {"TransactionType": "Payment", "Destination": DEST,
                  "Amount": "36000000"}
            return {"transactions": [
                {"tx": dict(ok), "meta": {"TransactionResult": "tesSUCCESS"}},
                {"tx": dict(ok), "meta": {"TransactionResult": "tesSUCCESS"}},
                {"tx": {"TransactionType": "Payment", "Destination": DEST,
                        "Amount": "100000"},
                 "meta": {"TransactionResult": "tecHOOK_REJECTED"}},
                {"tx": {"TransactionType": "Payment", "Destination": DEST,
                        "Amount": {"currency": "USD", "issuer": ISS,
                                   "value": "5"}},
                 "meta": {"TransactionResult": "tesSUCCESS"}},
            ]}
        raise AssertionError(method)

    _lines = C.hook_diagnostics(_rpc_hook_diag, _tx_hook)
    _text = "\n".join(_lines)
    check("hook diagnostics names the hook-guarded destination",
          "1 Hook(s) installed" in _text and C.short_addr(DEST) in _text)
    check("hook diagnostics reports the accepted 36 XAH amount",
          "36 XAH" in _text)
    check("hook diagnostics ignores the rejected payment itself",
          "0.1 XAH" not in _text)
    check("hook diagnostics ignores non-native amounts",
          "USD" not in _text)

    def _rpc_boom2(method, params=None):
        raise RuntimeError("node down")
    _lines2 = C.hook_diagnostics(_rpc_boom2, _tx_hook)
    check("hook diagnostics degrades gracefully when the node fails",
          any("failed" in ln for ln in _lines2))

X = load(BIN / "xahau", "xahau_cli")


def _refused(fn):
    try:
        fn()
    except SystemExit:
        return True
    return False


# ---- ClaimReward builder: Issuer is required by the ledger ----
GEN = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
claim_tx = X.build_claim(ACCT)
check("claim defaults Issuer to the genesis account",
      claim_tx.get("Issuer") == GEN)
check("claim keeps Account/TransactionType",
      claim_tx["Account"] == ACCT
      and claim_tx["TransactionType"] == "ClaimReward")
check("claim with explicit issuer keeps it",
      X.build_claim(ACCT, ISS)["Issuer"] == ISS)
check("claim refuses issuer == account (temMALFORMED)",
      _refused(lambda: X.build_claim(ACCT, ACCT)))

# ---- reward timing guard: never propose a claim the hook would reject ----
_REAL_RPC = X.C.rpc
_REAL_CFG = X.CFG
X.CFG = {"network": "xahau-testnet"}  # main() populates this on real runs
_NOW_RIPPLE = int(time.time()) - X.RIPPLE_EPOCH_OFFSET


def _rpc_reward(rt):
    def _fake(net, cmd, params):
        assert cmd == "account_info"
        return {"account_data": {"RewardTime": rt} if rt else {}}
    return _fake


X.C.rpc = _rpc_reward(_NOW_RIPPLE - 1000)  # claimed ~17 min ago
wait = X.reward_wait_seconds(ACCT)
check("claim timing refuses a premature claim",
      X.REWARD_DELAY_SECONDS - 2000 < wait <= X.REWARD_DELAY_SECONDS)

X.C.rpc = _rpc_reward(_NOW_RIPPLE - X.REWARD_DELAY_SECONDS - 10)
check("claim timing allows a due claim",
      X.reward_wait_seconds(ACCT) == 0)

X.C.rpc = _rpc_reward(None)  # never opted in: first claim is the opt-in
check("claim timing allows the opt-in claim",
      X.reward_wait_seconds(ACCT) == 0)


def _rpc_boom(net, cmd, params):
    raise RuntimeError("node down")


X.C.rpc = _rpc_boom
check("claim timing degrades gracefully when the node is unreachable",
      X.reward_wait_seconds(ACCT) == 0)
X.C.rpc = _REAL_RPC
X.CFG = _REAL_CFG

fails = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(fails)}/{len(PASS)} passed")
sys.exit(1 if fails else 0)
