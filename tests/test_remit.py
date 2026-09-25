#!/usr/bin/env python3
"""Remit (P2) adversarial logic tests — no network. Run: python3 tests/test_remit.py

Covers: --amount spec parsing, build_remit structure (AmountEntry wrappers
per the Xahau Remit docs), duplicate/self/oversize rejection, Remit shape
schema (required Amounts, 1-32 entries, no duplicate currencies), amount
sanity, multi-asset spend accounting, token extraction, and the ceremony
describer.

Test addresses below were freshly generated for these tests and hold no
funds; they replace no real addresses.
"""
import importlib.util
import os
os.environ.setdefault("XAHAU_DATA_DIR",
                       __import__("tempfile").mkdtemp(prefix="xahau-test-"))
import sys
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
X = load(BIN / "xahau", "xahau_cli")  # builders + parse_amount_spec

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def exits(fn):
    try:
        fn()
    except SystemExit:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


ALICE = "rLCMmXSWJdu1hAAXcsamQZWYoTwUjmd47N"
BOB = "rL7aHMMowzhjBbpgavH47kbu7eGZo2XsQd"
ISSUER = "rMCoVEJm9QpNfpov8EkRvT6MDNd34V9JZR"

# ---------- parse_amount_spec ----------

ccy, iss, val = X.parse_amount_spec("1 XAH")
check("spec: '1 XAH' -> native", ccy == "XAH" and iss is None and val == Decimal("1"))

ccy, iss, val = X.parse_amount_spec("10.5 USD." + ISSUER)
check("spec: IOU with issuer",
      ccy == "USD" and iss == ISSUER and val == Decimal("10.5"))

ccy, iss, val = X.parse_amount_spec("2 xah")
check("spec: lowercase ccy normalized", ccy == "XAH" and iss is None)

check("spec: bare value rejected", exits(lambda: X.parse_amount_spec("1")))
check("spec: XAH with issuer rejected",
      exits(lambda: X.parse_amount_spec("1 XAH." + ISSUER)))
check("spec: IOU without issuer rejected",
      exits(lambda: X.parse_amount_spec("1 USD")))
check("spec: non-numeric value rejected",
      exits(lambda: X.parse_amount_spec("abc XAH")))
check("spec: bad issuer rejected",
      exits(lambda: X.parse_amount_spec("1 USD.notanaddress")))
check("spec: three tokens rejected",
      exits(lambda: X.parse_amount_spec("1 XAH extra")))

# ---------- build_remit ----------

tx = X.build_remit(ALICE, BOB, ["1 XAH", "10 USD." + ISSUER], None)
check("build: TransactionType Remit", tx["TransactionType"] == "Remit")
check("build: Account/Destination", tx["Account"] == ALICE and tx["Destination"] == BOB)
check("build: native entry wrapped per docs",
      tx["Amounts"][0] == {"AmountEntry": {"Amount": "1000000"}})
check("build: IOU entry wrapped per docs",
      tx["Amounts"][1] == {"AmountEntry": {"Amount": {
          "currency": "USD", "issuer": ISSUER, "value": "10"}}})
check("build: no DestinationTag when None", "DestinationTag" not in tx)

tx2 = X.build_remit(ALICE, BOB, ["0.5 XAH"], 42)
check("build: DestinationTag set", tx2.get("DestinationTag") == 42)

check("build: self-remit refused (temREDUNDANT)",
      exits(lambda: X.build_remit(ALICE, ALICE, ["1 XAH"], None)))
check("build: duplicate XAH refused",
      exits(lambda: X.build_remit(ALICE, BOB, ["1 XAH", "2 XAH"], None)))
check("build: duplicate IOU refused",
      exits(lambda: X.build_remit(ALICE, BOB,
                                 ["1 USD." + ISSUER, "2 USD." + ISSUER], None)))
ok_two_issuers = True
try:
    X.build_remit(ALICE, BOB, ["1 USD." + ISSUER, "2 USD." + ALICE], None)
except SystemExit:
    ok_two_issuers = False
check("build: same ccy different issuers allowed", ok_two_issuers)
check("build: >32 distinct entries refused",
      exits(lambda: X.build_remit(
          ALICE, BOB,
          ["0.001 XAH"] + [f"1 C{i:02d}." + ISSUER for i in range(32)], None)))
check("build: 7-decimal XAH refused",
      exits(lambda: X.build_remit(ALICE, BOB, ["0.0000001 XAH"], None)))

# ---------- validate_tx_shape ----------

ALLOWED = ["Payment", "TrustSet", "ClaimReward", "Remit"]
good = dict(tx, Fee="15", Sequence=1, LastLedgerSequence=100,
            NetworkID=21338, SigningPubKey="")
check("shape: valid remit passes", C.validate_tx_shape(good, ALLOWED) == [])

no_amounts = {k: v for k, v in good.items() if k != "Amounts"}
check("shape: missing Amounts flagged",
      any("Amounts" in p and "required" in p
          for p in C.validate_tx_shape(no_amounts, ALLOWED)))

smuggled = dict(good, Memos=[])
check("shape: unknown field rejected",
      any("Memos" in p for p in C.validate_tx_shape(smuggled, ALLOWED)))

not_list = dict(good, Amounts={"AmountEntry": {"Amount": "1000000"}})
check("shape: non-list Amounts rejected",
      any("array" in p for p in C.validate_tx_shape(not_list, ALLOWED)))

empty = dict(good, Amounts=[])
check("shape: zero entries rejected",
      any("1-32" in p for p in C.validate_tx_shape(empty, ALLOWED)))

unwrapped = dict(good, Amounts=[{"Amount": "1000000"}])
check("shape: missing AmountEntry wrapper rejected",
      any("AmountEntry" in p for p in C.validate_tx_shape(unwrapped, ALLOWED)))

dup = dict(good, Amounts=[{"AmountEntry": {"Amount": "1000000"}},
                          {"AmountEntry": {"Amount": "2000000"}}])
check("shape: duplicate native rejected",
      any("duplicate" in p for p in C.validate_tx_shape(dup, ALLOWED)))

check("shape: Remit not in allowlist refused",
      any("allowlist" in p
          for p in C.validate_tx_shape(good, ["Payment", "TrustSet"])))

# ---------- validate_amounts ----------

zero = dict(good, Amounts=[{"AmountEntry": {"Amount": "0"}}])
check("amounts: zero entry rejected",
      any("Amounts[0]" in p for p in C.validate_amounts(zero)))

neg = dict(good, Amounts=[{"AmountEntry": {"Amount": {
    "currency": "USD", "issuer": ISSUER, "value": "-5"}}}])
check("amounts: negative IOU rejected",
      any("Amounts[0]" in p for p in C.validate_amounts(neg)))

check("amounts: valid remit clean", C.validate_amounts(good) == [])

# ---------- tx_spends / tx_tokens ----------

spends = C.tx_spends(dict(good, Fee="15000"))
check("spends: XAH entry counted",
      spends.get("XAH") == Decimal("1") + Decimal("0.015"))
check("spends: IOU entry counted",
      spends.get(f"USD.{ISSUER}") == Decimal("10"))
check("spends: no phantom assets", len(spends) == 2)

toks = C.tx_tokens(good)
check("tokens: IOU extracted", ("USD", ISSUER) in toks)
check("tokens: native excluded", ("XAH", None) not in toks)

# ---------- describe_tx ----------

lines = C.describe_tx(good, "remit")
joined = "\n".join(lines)
check("describe: lists XAH amount", "1 XAH" in joined)
check("describe: lists IOU amount", "10 USD" in joined)
check("describe: shows destination", BOB[:8] in joined)

# ---------- defaults ----------

check("defaults: Remit in default allowlist",
      "Remit" in C.DEFAULT_ALLOWED_TX_TYPES)

n_fail = sum(1 for _, ok in PASS if not ok)
print(f"\n{len(PASS) - n_fail}/{len(PASS)} passed")
sys.exit(1 if n_fail else 0)
