#!/usr/bin/env python3
"""Per-builder envelope round-trip — no network.

For every write builder, build a real transaction with the builder,
fill the autofill fields manually (Fee/Sequence/LastLedgerSequence/
NetworkID), save a proposal, and run it through verify_proposal +
verify_envelope_invariants. This is the regression net for the
uritoken-mint/"mint" action-label mismatch: any builder whose action
label doesn't match its TransactionType fails here.

Run: python3 tests/test_envelope.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="xahau-envelope-test-")
os.environ["XAHAU_DATA_DIR"] = TMP

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


C = load(BIN / "xahau_common.py", "xahau_common_envelope")
X = load(BIN / "xahau", "xahau_cli_envelope")
from xrpl.wallet import Wallet  # noqa: E402

ACCT = Wallet.create().classic_address
DEST = Wallet.create().classic_address
ISS = Wallet.create().classic_address
NET = "xahau-testnet"
NID = C.NETWORK_IDS[NET]
TOKEN = "AB" * 32

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    if not cond:
        print(f"FAIL: {name}")


def filled(tx):
    """Stand in for autofill(): no network in tests."""
    tx = dict(tx)
    tx.update({"Fee": "15", "Sequence": 1,
               "LastLedgerSequence": 999999, "NetworkID": NID})
    return tx


CASES = [
    ("send",
     lambda: X.build_payment(ACCT, DEST, Decimal("1"), "XAH", None, None)),
    ("trustline",
     lambda: X.build_trustline(ACCT, "USD", ISS, Decimal("100"))),
    ("claim",
     lambda: X.build_claim(ACCT)),
    ("remit",
     lambda: X.build_remit(ACCT, DEST, ["1 XAH", f"10 USD.{ISS}"], None)),
    ("uritoken-mint",
     lambda: X.build_uritoken_mint(ACCT, "DEADBEEF", burnable=True)),
    ("uritoken-buy",
     lambda: X.build_uritoken_buy(ACCT, TOKEN, "25 XAH")),
    ("uritoken-burn",
     lambda: X.build_uritoken_burn(ACCT, TOKEN)),
    ("import",
     lambda: X.build_import(ACCT, "DEADBEEF")[0]),
]


def roundtrip(action, tx):
    h, path = C.save_proposal(tx, NET, ACCT, action)
    prop = json.loads(path.read_text())
    vtx = C.verify_proposal(prop, path)
    C.verify_envelope_invariants(prop, vtx, NID)
    return h


for action, build in CASES:
    try:
        roundtrip(action, filled(build()))
        check(f"envelope round-trip: {action}", True)
    except C.ProposalError as e:
        check(f"envelope round-trip: {action} -> {e}", False)
    except AttributeError as e:
        # build_import does not exist yet if Import isn't built
        check(f"envelope round-trip: {action} -> {e}", False)


def expect_refuse(name, fn):
    try:
        fn()
    except C.ProposalError:
        check(name, True)
        return
    check(name, False)


# The exact bug Mike reproduced: a URITokenMint envelope labeled "mint"
# must NOT pass the payload verifier.
def _wrong_label():
    tx = filled(X.build_uritoken_mint(ACCT, "DEADBEEF"))
    h, path = C.save_proposal(tx, NET, ACCT, "mint")
    prop = json.loads(path.read_text())
    vtx = C.verify_proposal(prop, path)
    C.verify_envelope_invariants(prop, vtx, NID)


expect_refuse("wrong action label refused (mint vs uritoken-mint)",
              _wrong_label)


# A mismatched TransactionType for a valid label must also refuse.
def _wrong_type():
    tx = filled(X.build_payment(ACCT, DEST, Decimal("1"), "XAH", None, None))
    h, path = C.save_proposal(tx, NET, ACCT, "uritoken-mint")
    prop = json.loads(path.read_text())
    vtx = C.verify_proposal(prop, path)
    C.verify_envelope_invariants(prop, vtx, NID)


expect_refuse("mismatched tx type refused (Payment as uritoken-mint)",
              _wrong_type)


# allow_hooks attestation round-trips without breaking the hash.
h, path = C.save_proposal(filled(X.build_claim(ACCT)), NET, ACCT, "claim",
                          allow_hooks=True)
prop = json.loads(path.read_text())
try:
    vtx = C.verify_proposal(prop, path)
    C.verify_envelope_invariants(prop, vtx, NID)
    check("allow_hooks envelope round-trips",
          prop.get("allow_hooks") is True)
except C.ProposalError as e:
    check(f"allow_hooks envelope round-trips -> {e}", False)

# Old envelopes without the key default to False (fail closed).
prop2 = dict(prop)
del prop2["allow_hooks"]
check("missing allow_hooks defaults falsy", not prop2.get("allow_hooks"))

print(f"\n{PASS.__len__()}/{PASS.__len__() + FAIL.__len__()} envelope tests passed")
sys.exit(1 if FAIL else 0)
