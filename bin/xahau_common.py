#!/usr/bin/env python3
"""xahau_common: shared helpers for the Xahau Muse skill (P1).

Mirrors the safety architecture of the xrpl skill's xrpl_common, adapted for
Xahau: NetworkID on every transaction, XAH native asset (6 decimals),
1 XAH base / 0.2 XAH per-object reserves, and a tx-type allowlist matching
Xahau's live amendment set (verified 2026-09-24 via the `feature` RPC).

Key deviation from the xrpl skill, made deliberately: xrpl-py's binary codec
(5.2.0) does NOT know Xahau-only transaction types (URIToken*, SetHook,
Remit, ClaimReward, Import) — encode() raises KeyError for them. Proposal
envelopes therefore bind the SHA-256 of the CANONICAL JSON serialization of
the transaction instead of the binary blob. This is exactly as tamper-evident
(the hash commits to every field the human reviewed); for classic types the
proposer additionally asserts binary encodability as a submittability check.
The payload manager sends txjson to Xaman, which handles binary encoding.

No network, no API keys here — parsing, hashing, summaries, policy loading,
and the concurrency-safe spend tracker.
"""
import contextlib
import hashlib
import json
import os
import sys
import time
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

if os.name == "nt":
    import msvcrt  # Windows: no fcntl; byte-range locking instead
else:
    import fcntl

XAHAU_DIR = Path(os.environ.get("XAHAU_DATA_DIR") or Path.home() / ".xahau")
CONFIG_PATH = XAHAU_DIR / "config.json"          # address+network only
POLICY_PATH = XAHAU_DIR / "policy.json"
PROPOSALS_DIR = XAHAU_DIR / "proposals"
STATE_PATH = XAHAU_DIR / "state.json"
STATE_LOCK_PATH = XAHAU_DIR / "state.lock"
AUDIT_PATH = XAHAU_DIR / "audit.log"

NETWORKS = {
    "xahau-testnet": ["https://xahau-test.net"],
    "xahau-mainnet": ["https://xahau.network"],
}
NETWORK_IDS = {
    "xahau-testnet": 21338,
    "xahau-mainnet": 21337,
}
RIPPLE_EPOCH = 946684800  # unix seconds of 2000-01-01T00:00:00Z
NATIVE = "XAH"
DROPS_PER_XAH = Decimal(1_000_000)
RESERVE_BASE_DROPS = 1_000_000      # 1 XAH to activate an account
RESERVE_INC_DROPS = 200_000         # 0.2 XAH per owned object
REQUIRE_DEST_TAG_FLAG = 0x00020000  # lsfRequireDestTag

__version__ = "0.1.0"
POLICY_VERSION = 1
ENVELOPE_FORMAT = "xahau-proposal/1"
# Fields covered by the proposal hash. tx_sha256 commits to the canonical
# JSON of the full transaction — every safety-critical byte the human
# reviewed. Summaries are NOT stored and NOT trusted.
ENVELOPE_HASH_KEYS = ("format", "network", "account", "action",
                      "created_at", "policy_version", "tx_sha256")
ROLLING_WINDOW = 86400              # rolling spend window, seconds
MAX_FUTURE_SKEW = 300               # max clock skew for created_at
PROPOSAL_TTL_DEFAULT = 86400        # proposals expire after 24h


def _sanitize_proxy_env():
    for var in ("no_proxy", "NO_PROXY"):
        val = os.environ.get(var)
        if val:
            os.environ[var] = ",".join(
                p for p in val.split(",") if "[" not in p and "]" not in p)


_sanitize_proxy_env()


class ProposalError(Exception):
    """Envelope verification failed — tampered or stale proposal."""


# ---------- amounts / currencies (XAH: 6 decimals, like XRP) ----------

def dec(s, name):
    try:
        d = Decimal(str(s))
    except (InvalidOperation, ValueError):
        sys.exit(f"Bad {name}: {s!r}")
    if not d.is_finite():
        sys.exit(f"{name} must be a finite number, got {s!r}.")
    if d <= 0:
        sys.exit(f"{name} must be positive.")
    return d


def drops_to_xah(drops) -> Decimal:
    return Decimal(str(drops)) / DROPS_PER_XAH


def xah_to_drops(xah: Decimal) -> str:
    drops = Decimal(str(xah)) * DROPS_PER_XAH
    if not drops.is_finite() or drops != drops.to_integral_value():
        sys.exit("XAH amounts must have no more than six decimal places")
    return str(int(drops))


def choose_fee(drops_info: dict) -> int:
    """Pick a generic fee (in drops) with the standard 3x-median buffer.

    Xahau guidance is 3x the advertised median fee; base-fee-only (10
    drop) transactions can be rejected with telINSUF_FEE_P when the
    network is busy. This is the GENERIC floor only — for Payments the
    proposer additionally probes the fee RPC with the serialized
    transaction and pays max(generic, tx-specific requirement), so a
    Hook-raised requirement is never underpaid. There is deliberately
    no blind 15000-drop floor: on a quiet network a plain payment pays
    the tx-specific requirement, not a fixed minimum. The tx-specific
    `median_fee` is NOT used as a multiplier — it scales with Hook
    adjustments and would overpay wildly (verified live: 7M+ drops).
    """
    base = int(drops_info.get("base_fee", "10"))
    median = int(drops_info.get("median_fee", base))
    return max(3 * median, base, 10)


def currency_code(code: str) -> str:
    """3-char codes pass through; longer ones hex160-encode."""
    code = code.upper()
    if len(code) == 3 and code.isascii() and code.isalnum():
        return code
    raw = code.encode("ascii")
    if len(raw) > 20:
        sys.exit(f"currency code too long: {code!r}")
    return (raw + b"\x00" * (20 - len(raw))).hex().upper()


def display_currency(code: str) -> str:
    if isinstance(code, str) and len(code) == 40:
        try:
            raw = bytes.fromhex(code).rstrip(b"\x00")
            if raw.isascii():
                return raw.decode()
        except ValueError:
            pass
    return code


def norm_token(currency: str, issuer):
    """Canonical (CURRENCY, issuer-or-None) with XAH native."""
    c = display_currency(currency).upper()
    if c == NATIVE:
        return (NATIVE, None)
    return (c, issuer)


def asset_key(amount) -> str:
    """Spend-limit key for an amount: 'XAH' or 'CUR.issuer'."""
    if isinstance(amount, str):  # XAH, in drops
        return NATIVE
    return f"{display_currency(amount['currency'])}.{amount['issuer']}"


def amount_value(amount) -> Decimal:
    """Decimal value of an amount in its own units (XAH, not drops)."""
    if isinstance(amount, str):
        return drops_to_xah(amount)
    return Decimal(amount["value"])


def is_valid_classic_address(addr: str) -> bool:
    """Real base58-checksum validation, not startswith('r')."""
    try:
        from xrpl.core.addresscodec import is_valid_classic_address
        return is_valid_classic_address(addr)
    except ImportError:
        return isinstance(addr, str) and addr.startswith("r")


# ---------- canonical hashing / proposal envelopes ----------

def canonical_tx_bytes(tx: dict) -> bytes:
    """Deterministic serialization of the transaction for hash-binding."""
    return json.dumps(tx, sort_keys=True, separators=(",", ":"),
                      default=str).encode()


def tx_sha256(tx: dict) -> str:
    return hashlib.sha256(canonical_tx_bytes(tx)).hexdigest()


def tx_binary_if_known(tx: dict):
    """Binary-encode via xrpl-py when the codec knows the tx type.

    Returns the hex blob, or None for Xahau-only types the codec lacks.
    Used as a submittability sanity check, NOT for hash-binding.
    """
    try:
        from xrpl.core.binarycodec import encode
    except ImportError:
        raise  # missing dependency must be loud, never masked as "unknown type"
    try:
        return encode(tx).upper()
    except Exception:  # noqa: BLE001 — KeyError for unknown tx types
        return None


def ripple_time_from_now(seconds: int) -> int:
    return int(time.time()) - RIPPLE_EPOCH + seconds


def save_proposal(tx_dict, network, account, action):
    """Build a hash-bound proposal envelope and save it. Returns (hash, path)."""
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    envelope = {
        "format": ENVELOPE_FORMAT,
        "network": network,
        "account": account,
        "action": action,
        "created_at": int(time.time()),
        "policy_version": POLICY_VERSION,
        "tx": tx_dict,
        "tx_sha256": tx_sha256(tx_dict),
    }
    core = {k: envelope[k] for k in ENVELOPE_HASH_KEYS}
    h = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    envelope["proposal_hash"] = h
    path = PROPOSALS_DIR / f"{h}.json"
    path.write_text(json.dumps(envelope, indent=2, default=str))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return h, path


def verify_proposal(prop: dict, path) -> dict:
    """Verify the envelope and return the authoritative tx dict.

    Raises ProposalError on any tampering, format mismatch, or stale
    policy version. The transaction is the ONLY source of truth.
    """
    if prop.get("format") != ENVELOPE_FORMAT:
        raise ProposalError(
            f"unsupported proposal format {prop.get('format')!r} — rebuild "
            f"with xahau v{__version__} (old proposals are invalid)")
    if prop.get("policy_version") != POLICY_VERSION:
        raise ProposalError(
            f"proposal built for policy v{prop.get('policy_version')}, "
            f"requires v{POLICY_VERSION} — rebuild the proposal")
    core = {k: prop.get(k) for k in ENVELOPE_HASH_KEYS}
    if any(v is None for v in core.values()):
        raise ProposalError("proposal envelope is missing bound fields")
    h = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if h != prop.get("proposal_hash"):
        raise ProposalError(
            "proposal hash mismatch — the file was modified after the human "
            "reviewed it. Refusing.")
    if Path(path).stem != prop["proposal_hash"]:
        raise ProposalError("proposal filename does not match its hash")
    tx = prop.get("tx")
    if not isinstance(tx, dict):
        raise ProposalError("proposal has no transaction")
    if tx_sha256(tx) != prop.get("tx_sha256"):
        raise ProposalError(
            "transaction does not match the hash-bound digest — tampered")
    return tx


def verify_envelope_invariants(prop: dict, tx: dict, network_id: int):
    """Defense-in-depth checks on the envelope beyond the hash."""
    if prop.get("account") != tx.get("Account"):
        raise ProposalError(
            f"envelope account {prop.get('account')!r} != transaction "
            f"Account {tx.get('Account')!r} — refusing")
    ttype = tx.get("TransactionType")
    action = prop.get("action")
    expected = {
        "Payment": ("send",),
        "OfferCreate": ("buy", "sell"),
        "OfferCancel": ("cancel",),
        "TrustSet": ("trustline",),
        "URITokenMint": ("mint",),
        "URITokenBuy": ("uritoken-buy",),
        "URITokenBurn": ("uritoken-burn",),
        "SetHook": ("sethook",),
        "Remit": ("remit",),
        "ClaimReward": ("claim",),
        "Import": ("import",),
    }
    if action not in expected.get(ttype, ()):
        raise ProposalError(
            f"envelope action {action!r} does not match transaction type "
            f"{ttype!r} — refusing")
    for f in ("Account", "Fee", "Sequence", "LastLedgerSequence", "NetworkID"):
        if f not in tx:
            raise ProposalError(
                f"transaction missing required field {f!r} — refusing")
    if tx.get("NetworkID") != network_id:
        raise ProposalError(
            f"transaction NetworkID {tx.get('NetworkID')} != expected "
            f"{network_id} for {prop.get('network')} — refusing")
    if "TxnSignature" in tx or "SigningPubKey" in tx:
        raise ProposalError(
            "unsigned proposal carries TxnSignature/SigningPubKey — refusing")
    now = int(time.time())
    if prop.get("created_at", 0) > now + MAX_FUTURE_SKEW:
        raise ProposalError(
            "proposal created_at is unreasonably far in the future — refusing")


def load_proposal(prefix: str):
    if len(prefix) < 12 or any(c not in "0123456789abcdefABCDEF" for c in prefix):
        sys.exit("Proposal hash prefix must be at least 12 hexadecimal characters.")
    matches = list(PROPOSALS_DIR.glob(f"{prefix}*.json")) if PROPOSALS_DIR.exists() else []
    if not matches:
        sys.exit(f"No proposal matching {prefix!r}. See `xahau-payload --list`.")
    if len(matches) > 1:
        sys.exit(f"Ambiguous prefix {prefix!r}: {[m.stem[:12] for m in matches]}")
    return json.loads(matches[0].read_text()), matches[0]


# ---------- derived summaries (never trust stored text) ----------

def fmt_amount(a) -> str:
    if isinstance(a, str):
        return f"{drops_to_xah(a)} XAH"
    return (f"{a['value']} "
            f"{display_currency(a['currency'])}.{a['issuer'][:8]}…")


def short_addr(a: str) -> str:
    return a if len(a) <= 16 else f"{a[:8]}…{a[-4:]}"


def describe_tx(tx: dict, action_hint: str = "?") -> list:
    """Human-readable ceremony lines DERIVED from the transaction JSON."""
    ttype = tx.get("TransactionType")
    lines = [f"type:     {ttype} (proposed as: {action_hint})",
             f"network:  {tx.get('NetworkID')} "
             f"({'xahau-testnet' if tx.get('NetworkID') == 21338 else 'xahau-mainnet' if tx.get('NetworkID') == 21337 else 'UNKNOWN'})"]
    if ttype == "Payment":
        lines += [f"amount:   {fmt_amount(tx['Amount'])}",
                  f"to:       {short_addr(tx['Destination'])}"]
        tag = tx.get("DestinationTag")
        lines += [f"dest tag: {tag if tag is not None else 'NONE'}"]
    elif ttype == "TrustSet":
        la = tx["LimitAmount"]
        lines += [f"token:    {display_currency(la['currency'])}."
                  f"{short_addr(la['issuer'])}",
                  f"limit:    {la['value']}"]
    elif ttype == "OfferCreate":
        pays, gets = tx["TakerPays"], tx["TakerGets"]
        lines += [f"give:     {fmt_amount(gets)}",
                  f"receive:  {fmt_amount(pays)}"]
    elif ttype == "OfferCancel":
        lines += [f"offer seq: {tx.get('OfferSequence')}"]
    elif ttype == "ClaimReward":
        lines += ["effect:   claim monthly XAH balance reward"]
        if tx.get("Issuer"):
            lines += [f"issuer:   {short_addr(tx['Issuer'])}"]
    elif ttype == "URITokenMint":
        uri = tx.get("URI", "")
        try:
            uri_txt = bytes.fromhex(uri).decode("utf-8", "replace")
        except ValueError:
            uri_txt = uri
        lines += [f"uri:      {uri_txt[:80]}"]
        if tx.get("Digest"):
            lines += [f"digest:   {tx['Digest'][:16]}…"]
    elif ttype == "URITokenBuy":
        lines += [f"token id: {tx.get('URITokenID', '')[:16]}…",
                  f"price:    {fmt_amount(tx['Amount'])}"]
    elif ttype == "URITokenBurn":
        lines += [f"token id: {tx.get('URITokenID', '')[:16]}…"]
    elif ttype == "Remit":
        lines += [f"to:       {short_addr(tx['Destination'])}"]
        n = len(tx.get("URITokenIDs", []) or [])
        if n:
            lines += [f"uritokens: {n} attached"]
    elif ttype == "Import":
        lines += [f"issuer:   {short_addr(tx.get('Issuer', ''))}"]
    elif ttype == "SetHook":
        lines += ["effect:   install/update on-ledger Hook code",
                  "(review hook code out-of-band — the skill cannot "
                  "summarize WASM)"]
    else:
        lines += [f"(no describer for {ttype} — should have been rejected)"]
    lines.append(f"fee:      {drops_to_xah(tx.get('Fee', '0'))} XAH")
    lines.append(f"sequence: {tx.get('Sequence')}")
    lines.append(f"last_ledger: {tx.get('LastLedgerSequence', 'n/a')}")
    if "Expiration" in tx:
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(tx["Expiration"] + RIPPLE_EPOCH))
        lines.append(f"expires:  {exp}")
    return lines

# ---------- strict transaction shape ----------

COMMON_FIELDS = {"Account", "TransactionType", "Fee", "Sequence",
                 "LastLedgerSequence", "NetworkID", "SigningPubKey",
                 "TxnSignature"}
REQUIRED_FIELDS = {
    "Payment": {"Destination", "Amount"},
    "OfferCreate": {"TakerPays", "TakerGets"},
    "OfferCancel": {"OfferSequence"},
    "TrustSet": {"LimitAmount"},
    "URITokenMint": {"URI"},
    "URITokenBuy": {"URITokenID", "Amount"},
    "URITokenBurn": {"URITokenID"},
    "SetHook": {"CreateCode"},
    "Remit": {"Destination"},
    "ClaimReward": set(),
    "Import": {"Issuer"},
}
# Conservative per-type field schemas. A tx cannot smuggle fields outside
# its type's schema (no Paths/SendMax/Memos/partial-payment flags, etc.).
# These reflect Xahau's live amendment set; P2 proposers may widen SetHook/
# Remit/Import as their builders land — the allowlist is the gate.
ALLOWED_FIELDS = {
    "Payment": COMMON_FIELDS | {"Destination", "Amount", "DestinationTag"},
    "OfferCreate": COMMON_FIELDS | {"TakerPays", "TakerGets", "Expiration"},
    "OfferCancel": COMMON_FIELDS | {"OfferSequence"},
    "TrustSet": COMMON_FIELDS | {"LimitAmount"},
    "URITokenMint": COMMON_FIELDS | {"URI", "Digest", "Flags"},
    "URITokenBuy": COMMON_FIELDS | {"URITokenID", "Amount"},
    "URITokenBurn": COMMON_FIELDS | {"URITokenID", "Holder"},
    "SetHook": COMMON_FIELDS | {"CreateCode", "Flags", "HookOn",
                                "HookNamespace", "HookApiVersion",
                                "HookParameters", "HookGrants"},
    "Remit": COMMON_FIELDS | {"Destination", "DestinationTag", "Inform",
                              "MintURIToken", "URITokenIDs"},
    "ClaimReward": COMMON_FIELDS | {"Issuer"},
    "Import": COMMON_FIELDS | {"Issuer", "Blob"},
}
# Verified 2026-09-24 via live `feature` RPC on xahau.network:
# enabled — Hooks, URIToken, Remit, Import(=Burn2Mint), ClaimReward(via
# BalanceRewards); disabled — AMM, Escrow, PayChan, MultiSign, XLS-20,
# TickSize. The skill's allowlist MUST NOT include the disabled types.
# Enable only types with implemented builders and complete spend accounting.
DEFAULT_ALLOWED_TX_TYPES = ["Payment", "TrustSet", "ClaimReward"]


def validate_tx_shape(tx: dict, allowed_types) -> list:
    """Strict schema check. Returns a list of problems (empty = valid)."""
    problems = []
    ttype = tx.get("TransactionType")
    if ttype not in allowed_types:
        return [f"TransactionType {ttype!r} is not in the allowlist "
                f"{sorted(allowed_types)} — refusing"]
    allowed = ALLOWED_FIELDS.get(ttype, COMMON_FIELDS)
    for k in tx:
        if k not in allowed:
            problems.append(f"field {k!r} is not allowed for {ttype} — refusing")
    for k in REQUIRED_FIELDS.get(ttype, ()):
        if k not in tx:
            problems.append(f"field {k!r} is required for {ttype}")
    return problems


def validate_amounts(tx: dict) -> list:
    """Every amount/fee/sequence in the tx must be finite and sane."""
    problems = []

    def num(a, label, positive=True):
        try:
            v = amount_value(a)
        except Exception:  # noqa: BLE001
            return f"{label} is not a valid amount"
        if not v.is_finite():
            return f"{label} is not finite — refusing"
        if positive and v <= 0:
            return f"{label} must be positive"
        return None

    ttype = tx.get("TransactionType")
    if ttype == "OfferCreate":
        for f in ("TakerPays", "TakerGets"):
            p = num(tx.get(f), f)
            if p:
                problems.append(p)
    elif ttype in ("Payment", "URITokenBuy"):
        p = num(tx.get("Amount"), "Amount")
        if p:
            problems.append(p)
    elif ttype == "TrustSet":
        la = tx.get("LimitAmount", {})
        try:
            v = Decimal(str(la.get("value", "")))
        except (InvalidOperation, ValueError, TypeError):
            problems.append("LimitAmount value is not a valid number")
            v = None
        if v is not None and (not v.is_finite() or v < 0):
            problems.append("LimitAmount must be a finite non-negative number")
    for f in ("Fee", "Sequence", "LastLedgerSequence", "NetworkID"):
        raw = tx.get(f)
        try:
            iv = int(str(raw))
            if iv <= 0:
                problems.append(f"{f} must be a positive integer")
        except (ValueError, TypeError):
            problems.append(f"{f} must be a positive integer")
    return problems


# ---------- tx introspection (from tx JSON only) ----------

def tx_tokens(tx):
    """All non-XAH (currency, issuer) identities touched by a tx."""
    toks = set()

    def add_amount(a):
        if isinstance(a, dict):
            toks.add(norm_token(a["currency"], a.get("issuer")))

    ttype = tx.get("TransactionType")
    if ttype == "OfferCreate":
        add_amount(tx.get("TakerPays"))
        add_amount(tx.get("TakerGets"))
    elif ttype == "TrustSet":
        add_amount(tx.get("LimitAmount"))
    elif ttype in ("Payment", "URITokenBuy"):
        add_amount(tx.get("Amount"))
    toks.discard((NATIVE, None))
    return toks


def tx_spends(tx):
    """Aggregated (asset_key, Decimal) amounts this tx spends.

    For offers the spent side is TakerGets (what the creator provides).
    Fee is always XAH. Returns {asset_key: Decimal}.
    """
    spends = {}

    def add(key, amt):
        spends[key] = spends.get(key, Decimal(0)) + amt

    ttype = tx.get("TransactionType")
    if ttype == "OfferCreate":
        gets = tx.get("TakerGets")
        add(asset_key(gets), amount_value(gets))
    elif ttype in ("Payment", "URITokenBuy"):
        amt = tx.get("Amount")
        add(asset_key(amt), amount_value(amt))
    # TrustSet / OfferCancel / URITokenMint / URITokenBurn / SetHook /
    # Remit / ClaimReward / Import spend nothing beyond the fee
    add(NATIVE, drops_to_xah(tx.get("Fee", "0")))
    return spends


def tx_destination(tx):
    """(address, tag) for tx types that move value to someone, else None."""
    if tx.get("TransactionType") in ("Payment", "Remit"):
        return tx.get("Destination"), tx.get("DestinationTag")
    return None

# ---------- policy ----------

DEFAULT_POLICY = {
    "policy_version": POLICY_VERSION,
    "network_lock": "xahau-testnet",   # testnet until deliberately unlocked
    "max_fee_drops": 100000,           # must clear the fee (3x median buffer, tx-specific probe for Payments)
    "spend_limits": {
        "XAH": {"per_tx": "25", "per_day": "100"},
    },
    # [{"address": "r…", "destination_tag": 7|null}]. Empty = NO payments:
    # Payment/Remit proposals are refused until destinations are allowlisted.
    "destination_allowlist": [],
    "proposal_ttl_seconds": PROPOSAL_TTL_DEFAULT,
    "allowed_tx_types": DEFAULT_ALLOWED_TX_TYPES,
}


def load_policy():
    if not POLICY_PATH.exists():
        sys.exit(f"No policy file. Run `xahau init-policy` first ({POLICY_PATH}).")
    try:
        pol = json.loads(POLICY_PATH.read_text())
    except json.JSONDecodeError:
        sys.exit(f"Policy file is not valid JSON: {POLICY_PATH}")
    if pol.get("policy_version") != POLICY_VERSION:
        sys.exit(f"Policy is v{pol.get('policy_version')}, this skill requires "
                 f"v{POLICY_VERSION} — re-run `xahau init-policy`.")
    merged = dict(DEFAULT_POLICY)
    merged.update(pol)
    return merged


def write_default_policy():
    XAHAU_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(XAHAU_DIR, 0o700)
    except OSError:
        pass
    POLICY_PATH.write_text(json.dumps(DEFAULT_POLICY, indent=2) + "\n")
    os.chmod(POLICY_PATH, 0o600)
    return POLICY_PATH


def check_protected_files():
    """Fail closed unless skill state is owner-only."""
    problems = []
    try:
        euid = os.geteuid()
    except AttributeError:
        return  # non-POSIX: deployment must protect these another way
    for p in (POLICY_PATH, STATE_PATH, STATE_LOCK_PATH, AUDIT_PATH):
        if not p.exists():
            continue
        st = p.stat()
        if st.st_uid != euid:
            problems.append(f"{p} is not owned by the current user")
        if st.st_mode & 0o077:
            problems.append(
                f"{p} is group/world-accessible "
                f"(mode {oct(st.st_mode & 0o777)}) — run chmod 600")
    if problems:
        sys.exit("Skill state is not protected — refusing:\n  "
                 + "\n  ".join(problems))


def policy_check(prop: dict, tx: dict, policy: dict) -> list:
    """All policy gates for payload CREATION. Returns denial reasons.

    Does NOT touch spend limits (those are reserved atomically via
    SpentTracker.try_reserve after this passes). Everything is derived
    from the proposal + tx — no stored summaries trusted.
    """
    denials = []
    lock = policy.get("network_lock", "xahau-testnet")
    if prop.get("network") != lock:
        denials.append(
            f"proposal network {prop.get('network')!r} != policy network_lock "
            f"{lock!r} — refusing")
        return denials  # network mismatch poisons every other check
    want_netid = NETWORK_IDS.get(lock)
    if tx.get("NetworkID") != want_netid:
        denials.append(
            f"transaction NetworkID {tx.get('NetworkID')} != {want_netid} "
            f"for {lock} — refusing")
    ttl = int(policy.get("proposal_ttl_seconds", PROPOSAL_TTL_DEFAULT))
    age = int(time.time()) - int(prop.get("created_at", 0))
    if age > ttl:
        denials.append(
            f"proposal is {age}s old, older than TTL {ttl}s — rebuild it")
    if age < -MAX_FUTURE_SKEW:
        denials.append("proposal created_at is in the future — refusing")
    allowed = policy.get("allowed_tx_types", DEFAULT_ALLOWED_TX_TYPES)
    denials += validate_tx_shape(tx, allowed)
    denials += validate_amounts(tx)
    try:
        fee_drops = int(str(tx.get("Fee", "0")))
    except (ValueError, TypeError):
        fee_drops = None
    max_fee = int(policy.get("max_fee_drops", 10000))
    if fee_drops is None or fee_drops > max_fee:
        denials.append(
            f"fee {tx.get('Fee')} drops exceeds max_fee_drops {max_fee}")
    # Issued-token identity: any token is allowed, but it MUST have a
    # spend limit configured — unlisted assets are blocked (fail closed).
    limits = policy.get("spend_limits", {})
    for tok in tx_tokens(tx):
        key = f"{tok[0]}.{tok[1]}"
        if key not in limits:
            denials.append(
                f"no spend limit configured for {key} — fail closed "
                f"(add it to spend_limits or remove the asset)")
    # Destination allowlist: empty list = no payments at all.
    dest = tx_destination(tx)
    if dest is not None:
        addr, tag = dest
        allow = policy.get("destination_allowlist", [])
        if not allow:
            denials.append(
                "destination allowlist is empty — no payments until "
                "destinations are allowlisted in the policy")
        elif not any(e.get("address") == addr and
                     e.get("destination_tag") == tag for e in allow):
            denials.append(
                f"destination {short_addr(addr)} tag={tag} is not allowlisted "
                f"— refusing")
        if not is_valid_classic_address(addr):
            denials.append(f"destination {addr!r} is not a valid address")
    return denials


def check_require_dest_tag(rpc_fn, tx: dict) -> list:
    """Refuse untagged payments to accounts with RequireDestTag set.

    Fail-closed on lookup errors. rpc_fn(method, params) -> result dict.
    """
    dest = tx_destination(tx)
    if dest is None:
        return []
    addr, tag = dest
    try:
        r = rpc_fn("account_info", [{"account": addr,
                                     "ledger_index": "validated"}])
    except Exception as e:  # noqa: BLE001
        return [f"could not look up destination flags ({e}) — refusing"]
    data = (r or {}).get("account_data", {})
    if data.get("Flags", 0) & REQUIRE_DEST_TAG_FLAG and tag is None:
        return [f"destination {short_addr(addr)} requires a destination tag "
                f"— refusing untagged payment"]
    return []

# ---------- concurrency-safe spend tracker ----------

@contextlib.contextmanager
def _exclusive_lock(f):
    """Cross-platform exclusive file lock.

    POSIX: flock(LOCK_EX). Windows: msvcrt byte-range lock on the first
    byte (file must be opened in binary mode with at least one byte).
    """
    if os.name == "nt":
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class SpentTracker:
    """True rolling-24h per-asset spend tracker with an exclusive file lock.

    Entries are ``pending`` from reservation until the ledger outcome is
    known: validated tesSUCCESS -> ``confirmed``; validated failure or
    payload rejection/expiry -> released. Ambiguous outcomes STAY pending —
    sweep_pending() resolves them against the ledger on the next run.
    check+reserve is one atomic section under the lock.
    """

    def __init__(self, state_path=None, lock_path=None):
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.lock_path = Path(lock_path) if lock_path else STATE_LOCK_PATH

    @contextlib.contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        new_lock = not self.lock_path.exists()
        # Windows needs binary mode + a real byte for msvcrt locking.
        mode = "w+b" if os.name == "nt" else "w"
        with open(self.lock_path, mode) as lf:
            if new_lock:
                try:
                    os.chmod(self.lock_path, 0o600)
                except OSError:
                    pass
            if os.name == "nt":
                lf.write(b"\x00")
                lf.flush()
            with _exclusive_lock(lf):
                yield

    def _load(self):
        try:
            st = json.loads(self.state_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {"entries": []}
        if isinstance(st.get("entries"), list):
            return {"entries": st["entries"]}
        return {"entries": []}

    def _save(self, st):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.chmod(tmp, 0o600)
        tmp.replace(self.state_path)

    @staticmethod
    def _prune(entries, now):
        # Pending reservations are never pruned by age: a reservation must
        # never silently vanish. Only confirmed/released entries age out.
        return [e for e in entries
                if e.get("status") == "pending"
                or now - int(e.get("ts", 0)) < ROLLING_WINDOW]

    @staticmethod
    def _totals(entries):
        t = {}
        for e in entries:
            t[e["asset"]] = t.get(e["asset"], Decimal(0)) + Decimal(e["amount"])
        return t

    def _deny(self, spends, entries, policy):
        denials = []
        totals = self._totals(entries)
        limits = policy.get("spend_limits", {})
        for asset, amt in spends.items():
            lim = limits.get(asset)
            if lim is None:
                denials.append(
                    f"no spend limit configured for {asset} — fail closed")
                continue
            if not amt.is_finite():
                denials.append(f"{asset} spend {amt} is not finite — refusing")
                continue
            if amt > Decimal(lim["per_tx"]):
                denials.append(
                    f"{asset} spend {amt} > per-tx limit {lim['per_tx']}")
            day = totals.get(asset, Decimal(0))
            if day + amt > Decimal(lim["per_day"]):
                denials.append(
                    f"{asset} rolling-24h spend would be {day + amt} > "
                    f"per-day limit {lim['per_day']} (used {day})")
        return denials

    def check(self, spends: dict, policy) -> list:
        with self._locked():
            entries = self._prune(self._load()["entries"], int(time.time()))
            return self._deny(spends, entries, policy)

    def try_reserve(self, spends: dict, policy):
        """Atomically check limits AND record pending entries."""
        import uuid
        rid = uuid.uuid4().hex[:16]
        with self._locked():
            now = int(time.time())
            entries = self._prune(self._load()["entries"], now)
            denials = self._deny(spends, entries, policy)
            if denials:
                return denials, None
            for asset, amt in spends.items():
                entries.append({"rid": rid, "ts": now, "asset": asset,
                                "amount": str(amt), "status": "pending",
                                "tx_hash": None, "last_ledger": None})
            self._save({"entries": entries})
        return [], rid

    def release_reservation(self, rid):
        if not rid:
            return
        with self._locked():
            st = self._load()
            st["entries"] = [e for e in self._prune(st["entries"], int(time.time()))
                             if not (e.get("rid") == rid
                                     and e.get("status") == "pending"
                                     and not e.get("tx_hash"))]
            self._save(st)

    def bind_reservation(self, rid, tx_hash, last_ledger):
        with self._locked():
            st = self._load()
            for e in st["entries"]:
                if e.get("rid") == rid and e.get("status") == "pending":
                    e["tx_hash"] = tx_hash
                    e["last_ledger"] = last_ledger
            self._save(st)

    def bind_payload(self, rid, proposal_hash, payload_uuid):
        with self._locked():
            st = self._load()
            for e in st["entries"]:
                if e.get("rid") == rid and e.get("status") == "pending":
                    e["proposal_hash"] = proposal_hash
                    e["payload_uuid"] = payload_uuid
            self._save(st)

    def pending_for_proposal(self, proposal_hash):
        with self._locked():
            return [e for e in self._load()["entries"]
                    if e.get("proposal_hash") == proposal_hash
                    and e.get("status") == "pending"]

    def fail_tx(self, tx_hash, fee_drops):
        """Validated failure consumes only the fee, not the proposed amount."""
        with self._locked():
            st = self._load()
            keep = []
            for e in st["entries"]:
                if e.get("tx_hash") == tx_hash and e.get("status") == "pending":
                    if e.get("asset") != NATIVE:
                        continue
                    # Native payments include amount + fee in one reservation.
                    # Keep the fee only after a validated failure.
                    e["amount"] = str(drops_to_xah(fee_drops))
                    e["status"] = "confirmed"
                keep.append(e)
            self._save({"entries": keep})

    def confirm(self, tx_hash):
        with self._locked():
            st = self._load()
            for e in st["entries"]:
                if e.get("tx_hash") == tx_hash and e.get("status") == "pending":
                    e["status"] = "confirmed"
            self._save(st)

    def release_tx(self, tx_hash):
        with self._locked():
            st = self._load()
            st["entries"] = [e for e in st["entries"]
                             if e.get("tx_hash") != tx_hash]
            self._save(st)

    def sweep_pending(self, rpc_fn):
        """Resolve ambiguous pending reservations against the ledger.

        For each bound pending entry whose LastLedgerSequence has passed the
        validated ledger: tesSUCCESS -> confirmed; validated failure or
        provably not included -> released. Uncertain stays pending.
        """
        with self._locked():
            now = int(time.time())
            entries = self._prune(self._load()["entries"], now)
            bound = [e for e in entries
                     if e.get("status") == "pending" and e.get("tx_hash")
                     and e.get("last_ledger")]
            if not bound:
                self._save({"entries": entries})
                return
            try:
                cur = rpc_fn("ledger", [{"ledger_index": "validated"}]
                             ).get("ledger_index")
            except Exception:  # noqa: BLE001
                self._save({"entries": entries})
                return  # cannot tell — keep everything pending
            keep = []
            for e in entries:
                if (e.get("status") == "pending" and e.get("tx_hash")
                        and e.get("last_ledger")
                        and int(e["last_ledger"]) < int(cur)):
                    try:
                        r = rpc_fn("tx", [{"transaction": e["tx_hash"]}])
                        res = (r.get("meta", {}) or {}
                               ).get("TransactionResult")
                        validated = r.get("validated", False)
                    except Exception:  # noqa: BLE001
                        keep.append(e)  # uncertain — keep pending
                        continue
                    if validated and res == "tesSUCCESS":
                        e["status"] = "confirmed"
                        keep.append(e)
                    elif validated:
                        pass  # validated failure or not found -> release
                    else:
                        keep.append(e)
                else:
                    keep.append(e)
            self._save({"entries": keep})


def audit(action, proposal_hash, tx_hash, network, account, result, note="",
         spends=None):
    XAHAU_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": int(time.time()),
        "action": action,
        "proposal_hash": proposal_hash,
        "tx_hash": tx_hash,
        "network": network,
        "account": account,
        "result": result,
        "note": note,
    }
    if spends:
        entry["spends"] = {k: str(v) for k, v in spends.items()}
    new_file = not AUDIT_PATH.exists()
    with AUDIT_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    if new_file:
        os.chmod(AUDIT_PATH, 0o600)


# ---------- raw JSON-RPC (requests; no xrpl-py client needed) ----------

def _hook_count(rpc_fn, address) -> int:
    """Hook count via an injected rpc_fn(method, params). Shared core."""
    try:
        res = rpc_fn("account_objects",
                     [{"account": address, "type": "hook",
                       "ledger_index": "validated", "limit": 1}])
    except RuntimeError as e:
        if "actNotFound" in str(e):
            return 0
        raise
    return len(res.get("account_objects", []))


def destination_hook_count(network, address) -> int:
    """How many Hooks are installed on an account (0 if it doesn't exist yet).

    A Hook on the destination can reject or redirect a payment
    (e.g. an NFT-vending hook that only accepts exact amounts), so the
    proposer checks this before building a payment proposal. A missing
    account (actNotFound) simply means a fresh account with no hooks.
    Any other node error fails closed by raising.
    """
    return _hook_count(lambda m, p=None: rpc(network, m, p), address)


def hook_messages(ledger_tx):
    """Decode human-readable Hook return strings from validated metadata."""
    messages = []
    for item in (ledger_tx.get("meta") or {}).get("HookExecutions", []):
        execution = item.get("HookExecution") or {}
        raw = execution.get("HookReturnString") or ""
        try:
            message = bytes.fromhex(raw).rstrip(b"\x00").decode("utf-8", "replace")
        except ValueError:
            message = raw
        if message:
            messages.append((execution.get("HookAccount"), message))
    return messages


def validated_outcome(proposed_tx, ledger_tx):
    """Return (state, result, problems) for the exact proposed transaction.

    `state` is success, failed, mismatch, or pending. A validated `tec*`
    transaction failed but still consumed its fee and sequence.
    """
    if not ledger_tx.get("validated"):
        return "pending", None, []
    problems = []
    for field, expected in proposed_tx.items():
        actual = ledger_tx.get(field)
        if actual != expected:
            problems.append(f"{field}: proposed {expected!r}, ledger {actual!r}")
    result = (ledger_tx.get("meta") or {}).get("TransactionResult")
    if problems:
        return "mismatch", result, problems
    if result == "tesSUCCESS":
        if proposed_tx.get("TransactionType") == "Payment":
            delivered = (ledger_tx.get("meta") or {}).get("delivered_amount")
            if delivered is not None and delivered != proposed_tx.get("Amount"):
                return "mismatch", result, [
                    f"delivered_amount: proposed {proposed_tx.get('Amount')!r}, "
                    f"ledger {delivered!r}"]
        return "success", result, []
    if isinstance(result, str) and result:
        return "failed", result, []
    return "pending", result, []


def hook_diagnostics(rpc_fn, tx) -> list:
    """Explain a tecHOOK_REJECTED in plain lines.

    Reports Hook counts on the sender/destination and, for payments,
    the exact native amounts the destination has accepted before — a
    Hook such as an NFT vendor typically demands one exact amount, and
    that amount is visible in public ledger history. Only public-ledger
    reads via rpc_fn; never raises — failures become lines.
    """
    lines = ["hook diagnostics: a Hook on the sending or receiving account",
             "rejected this transaction (the fee was still charged)."]
    sender = tx.get("Account")
    dest = tx.get("Destination")
    for label, addr in (("sender", sender), ("destination", dest)):
        if not addr:
            continue
        try:
            n = _hook_count(rpc_fn, addr)
        except Exception as e:  # noqa: BLE001
            lines.append(f"  {label} {short_addr(addr)}: hook lookup failed ({e})")
            continue
        lines.append(f"  {label} {short_addr(addr)}: {n} Hook(s) installed")
    if tx.get("TransactionType") == "Payment" and dest:
        try:
            res = rpc_fn("account_tx", [{"account": dest, "limit": 50,
                                         "ledger_index_min": -1,
                                         "ledger_index_max": -1}])
            entries = res.get("transactions", [])
        except Exception as e:  # noqa: BLE001
            lines.append(f"  accepted-amount scan failed: {e}")
            entries = None
        if entries is not None:
            amounts = Counter()
            for entry in entries:
                t = entry.get("tx") or entry.get("tx_json") or {}
                m = entry.get("meta") or {}
                if (t.get("TransactionType") == "Payment"
                        and t.get("Destination") == dest
                        and m.get("TransactionResult") == "tesSUCCESS"
                        and isinstance(t.get("Amount"), str)):
                    amounts[t["Amount"]] += 1
            if amounts:
                lines.append("  native amounts this destination accepted before:")
                for drops, n in amounts.most_common(5):
                    lines.append(f"    {drops_to_xah(drops)} XAH  (x{n})")
            else:
                lines.append("  no successful native payments to the destination "
                             "in recent history")
    return lines


def rpc(network, method, params=None, timeout=20):
    """POST one JSON-RPC call to a Xahau node. Returns the result dict.

    Raises RuntimeError on transport failure or node-side error.
    """
    import requests
    last_err = None
    for url in NETWORKS[network]:
        for attempt in range(3):
            try:
                r = requests.post(url, json={"method": method,
                                             "params": params or [{}]},
                                  timeout=timeout)
                r.raise_for_status()
                body = r.json()
                if "error" in body:
                    raise RuntimeError(
                        f"node error {body['error']}: "
                        f"{body.get('error_message', '')}")
                return body.get("result", {})
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1 + attempt)
    raise RuntimeError(f"No healthy {network} node ({last_err}).")
