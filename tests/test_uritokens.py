#!/usr/bin/env python3
"""URIToken (P2) adversarial logic tests — no network. Run: python3 tests/test_uritokens.py

Covers: URI text/hex handling (+256-byte cap), mint/buy/burn builders,
token-id validation, exact price-match comparison, spec round-trip, shape
schemas (mint Amount/Destination allowed, burn Holder rejected per the
Xahau docs), amount sanity, spend accounting, and the ceremony describer.

Ledger pre-checks (buy price-match, burn ownership) need network and are
verified live, not here — the pure comparison helpers they rely on are
tested below.
"""
import importlib.util
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
X = load(BIN / "xahau", "xahau_cli")

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
TOKEN = "A" * 64

# ---------- uri_to_hex ----------

check("uri: text -> hex",
      X.uri_to_hex(uri_text="ipfs://QmX") == "ipfs://QmX".encode().hex().upper())
check("uri: --uri-hex passthrough", X.uri_to_hex(uri_hex="69706673") == "69706673")
check("uri: both rejected",
      exits(lambda: X.uri_to_hex(uri_text="a", uri_hex="62")))
check("uri: neither rejected", exits(lambda: X.uri_to_hex()))
check("uri: bad hex rejected", exits(lambda: X.uri_to_hex(uri_hex="zz")))
check("uri: odd-length hex rejected", exits(lambda: X.uri_to_hex(uri_hex="abc")))
check("uri: empty rejected", exits(lambda: X.uri_to_hex(uri_text="")))
check("uri: 257 bytes rejected",
      exits(lambda: X.uri_to_hex(uri_text="x" * 257)))
check("uri: 256 bytes allowed",
      X.uri_to_hex(uri_text="x" * 256) == ("x" * 256).encode().hex().upper())

# ---------- build_uritoken_mint ----------

m = X.build_uritoken_mint(ALICE, X.uri_to_hex(uri_text="ipfs://mint1"))
check("mint: minimal shape",
      m == {"Account": ALICE, "TransactionType": "URITokenMint",
            "URI": "ipfs://mint1".encode().hex().upper()})

m2 = X.build_uritoken_mint(ALICE, "69706673", digest="B" * 64, burnable=True,
                           price_spec="10 USD." + ISSUER, destination=BOB)
check("mint: full shape",
      m2["Digest"] == "B" * 64 and m2["Flags"] == 1
      and m2["Amount"] == {"currency": "USD", "issuer": ISSUER, "value": "10"}
      and m2["Destination"] == BOB)
check("mint: bad digest rejected",
      exits(lambda: X.build_uritoken_mint(ALICE, "62", digest="short")))
check("mint: bad destination rejected",
      exits(lambda: X.build_uritoken_mint(ALICE, "62", destination="nope")))
check("mint: bad price rejected",
      exits(lambda: X.build_uritoken_mint(ALICE, "62", price_spec="1 USD")))

# ---------- build_uritoken_buy / burn ----------

b = X.build_uritoken_buy(ALICE, TOKEN, "10 USD." + ISSUER)
check("buy: shape",
      b == {"Account": ALICE, "TransactionType": "URITokenBuy",
            "URITokenID": TOKEN,
            "Amount": {"currency": "USD", "issuer": ISSUER, "value": "10"}})
check("buy: bad token id rejected",
      exits(lambda: X.build_uritoken_buy(ALICE, "short", "1 XAH")))
check("buy: non-hex token id rejected",
      exits(lambda: X.build_uritoken_buy(ALICE, "Z" * 64, "1 XAH")))

u = X.build_uritoken_burn(ALICE, TOKEN.lower())
check("burn: shape + id uppercased",
      u == {"Account": ALICE, "TransactionType": "URITokenBurn",
            "URITokenID": TOKEN})
check("burn: bad token id rejected",
      exits(lambda: X.build_uritoken_burn(ALICE, "xyz")))

# ---------- amounts_equal (price-match core) ----------

listing = {"currency": "USD", "issuer": ISSUER, "value": "10"}
check("eq: exact IOU match",
      C.amounts_equal({"currency": "USD", "issuer": ISSUER, "value": "10"}, listing))
check("eq: value mismatch", not C.amounts_equal(
    {"currency": "USD", "issuer": ISSUER, "value": "10.000001"}, listing))
check("eq: currency mismatch", not C.amounts_equal(
    {"currency": "EUR", "issuer": ISSUER, "value": "10"}, listing))
check("eq: issuer mismatch", not C.amounts_equal(
    {"currency": "USD", "issuer": ALICE, "value": "10"}, listing))
check("eq: native drops match", C.amounts_equal("1000000", "1000000"))
check("eq: native drops mismatch", not C.amounts_equal("1000000", "2000000"))
check("eq: mixed types", not C.amounts_equal("1000000", listing))
check("eq: malformed -> False", not C.amounts_equal({"nope": 1}, listing))

# ---------- amount_to_spec round-trip ----------

for spec in ("1 XAH", "0.5 XAH", "10 USD." + ISSUER):
    ccy, iss, val = X.parse_amount_spec(spec)
    back = X.amount_to_spec(X.make_amount(ccy, iss, val))
    c2, i2, v2 = X.parse_amount_spec(back)
    check(f"round-trip: {spec}",
          (ccy, iss, val) == (c2, i2, v2)
          and C.amounts_equal(X.make_amount(ccy, iss, val),
                              X.make_amount(c2, i2, v2)))

# ---------- shape schemas ----------

ALLOWED = C.DEFAULT_ALLOWED_TX_TYPES
mint_tx = dict(m2, Fee="15", Sequence=1, LastLedgerSequence=100,
               NetworkID=21338, SigningPubKey="")
check("shape: full mint passes", C.validate_tx_shape(mint_tx, ALLOWED) == [])

no_uri = {k: v for k, v in mint_tx.items() if k != "URI"}
check("shape: mint requires URI",
      any("URI" in p and "required" in p
          for p in C.validate_tx_shape(no_uri, ALLOWED)))

buy_tx = dict(b, Fee="15", Sequence=1, LastLedgerSequence=100,
              NetworkID=21338, SigningPubKey="")
check("shape: buy passes", C.validate_tx_shape(buy_tx, ALLOWED) == [])
no_amt = {k: v for k, v in buy_tx.items() if k != "Amount"}
check("shape: buy requires Amount",
      any("Amount" in p and "required" in p
          for p in C.validate_tx_shape(no_amt, ALLOWED)))

burn_tx = dict(u, Fee="15", Sequence=1, LastLedgerSequence=100,
               NetworkID=21338, SigningPubKey="")
check("shape: burn passes", C.validate_tx_shape(burn_tx, ALLOWED) == [])
with_holder = dict(burn_tx, Holder=BOB)
check("shape: burn Holder rejected (not in Xahau docs)",
      any("Holder" in p for p in C.validate_tx_shape(with_holder, ALLOWED)))

# ---------- validate_amounts ----------

bad_price = dict(mint_tx, Amount={"currency": "USD", "issuer": ISSUER,
                                  "value": "-3"})
check("amounts: negative mint price rejected",
      any("Amount" in p for p in C.validate_amounts(bad_price)))
check("amounts: mint without price clean",
      C.validate_amounts({k: v for k, v in mint_tx.items()
                          if k != "Amount"}) == [])

# ---------- tx_spends ----------

sp = C.tx_spends(dict(buy_tx, Fee="15000"))
check("spends: buy counts price + fee",
      sp.get(f"USD.{ISSUER}") == Decimal("10")
      and sp.get("XAH") == Decimal("0.015"))
sp2 = C.tx_spends(dict(mint_tx, Fee="15000"))
check("spends: mint is fee-only (+reserve warning, not spend)",
      sp2 == {"XAH": Decimal("0.015")})

# ---------- allowlist / describer ----------

for t in ("URITokenMint", "URITokenBuy", "URITokenBurn"):
    check(f"defaults: {t} in allowlist", t in C.DEFAULT_ALLOWED_TX_TYPES)

lines = "\n".join(C.describe_tx(mint_tx, "uritoken-mint"))
check("describe: mint shows price", "10 USD" in lines)
check("describe: mint shows restricted buyer", BOB[:8] in lines)
check("describe: mint shows burnable", "burnable" in lines)
lines_b = "\n".join(C.describe_tx(buy_tx, "uritoken-buy"))
check("describe: buy shows price + token", "10 USD" in lines_b and TOKEN[:16] in lines_b)

n_fail = sum(1 for _, ok in PASS if not ok)
print(f"\n{len(PASS) - n_fail}/{len(PASS)} passed")
sys.exit(1 if n_fail else 0)
