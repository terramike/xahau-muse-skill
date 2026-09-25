---
name: xahau
description: Interact with the Xahau network — payments, trustlines, Hooks, xMerch, and the monthly XAH ClaimReward — with a propose → Xaman payload → phone-tap safety boundary. No seed ever touches the skill.
---

# xahau-muse-skill (P1)

Interact with the Xahau network from the terminal — payments, trustlines,
URITokens, and the monthly XAH ClaimReward — with a hard safety boundary
between proposing an action and approving it on your phone.

There is **no local seed and no private key anywhere in this skill**.
Signing happens in the Xaman wallet on your phone via a sign-request
payload. That tap is the real approval.

## Architecture: propose → review → payload → phone tap (v0.1)

Two programs. The agent only ever runs the first.

1. **`xahau` — reads + proposer.** Builds transactions, autofills them
   against the live network (fee, sequence, last-ledger, `NetworkID`),
   prints the full ceremony (network, account, assets+issuers, amounts,
   fee, sequence), and saves a **hash-bound proposal envelope**. It never
   signs, never submits, never sees API credentials.
2. **`xahau-payload` — the payload manager.** Takes a human-approved
   proposal hash, re-verifies the envelope, enforces `~/.xahau/policy.json`,
   reserves spend atomically, then `POST`s a sign request to the Xaman
   API. It prints the sign URL — open it on your phone (or scan the QR in
   Xaman; push works after one prior interaction with the app). You
   approve or reject the tap in Xaman. The program polls until the payload
   resolves, then verifies the **validated** ledger result via public RPC
   and appends to `~/.xahau/audit.log`. If the outcome is ambiguous
   (timeout, crash, no txid), the spend reservation is NOT released —
   `xahau-payload resume --hash <prefix>` re-checks it later.

### The envelope (what the hash binds)

A proposal is `format: xahau-proposal/1` and the approval hash covers
`format`, `network`, `account`, `action`, `created_at`, `policy_version`,
and `tx_sha256` — the SHA-256 of the canonical JSON of the complete
transaction. Any modification after the human reviewed it is rejected.

Deliberate deviation from the xrpl skill: xrpl-py's binary codec (5.2.0)
does not know Xahau-only transaction types (`URIToken*`, `SetHook`,
`Remit`, `ClaimReward`, `Import`), so envelopes bind the canonical-JSON
digest instead of the binary blob. This is exactly as tamper-evident;
for classic types the proposer additionally asserts binary encodability.

### The ceremony (every write)

```bash
xahau send --to r… --amount 1 --ccy XAH
# → prints the full proposal + hash, e.g. e0587d9eb75bd911…
# → NOTHING is submitted.

# A human reviews the exact hash, then:
xahau-payload send --hash e0587d9eb75bd911
# → envelope verify → policy gates → spend reserved → Xaman payload created
# → open the sign URL on your phone, approve the tap in Xaman
# → validated ledger result → audit log
```

Without a phone tap, nothing moves. Ever.

## Policy (`xahau init-policy` creates `~/.xahau/policy.json`, v1)

- `network_lock` — default `xahau-testnet`. The emergency brake.
  Mainnet proposals hard-fail until you explicitly opt in.
- `allowed_tx_types` — `Payment`, `OfferCreate`, `OfferCancel`,
  `TrustSet`, `URITokenMint`, `URITokenBuy`, `URITokenBurn`, `SetHook`,
  `Remit`, `ClaimReward`, `Import`. This matches Xahau's live amendment
  set (verified via the `feature` RPC): **no** Escrow, PayChans,
  MultiSign, XLS-20 NFTs, or AMM — they are disabled on Xahau.
- `spend_limits` — per-asset `{per_tx, per_day}` caps with a true
  rolling-24h window, reserved atomically under a file lock. Assets with
  no configured limit are **blocked** (fail closed).
- `destination_allowlist` — `[{address, destination_tag}]` pairs.
  **Empty = no payments.** Payment/Remit proposals are refused until
  destinations are allowlisted.
- `max_fee_drops` (default 10000 — Hook transactions cost far more than
  the base fee), `proposal_ttl_seconds` (default 86400).
- **Fees:** the proposer pays 3x the network's advertised `median_fee`
  with a 15000-drop floor — never the 10-drop `base_fee`. Xahau's fee
  escalation has a high structural floor (median fee level minimum
  128000 → `median_fee` ≥ 5000 drops), and empirically even the median
  fee can be rejected with `telINSUF_FEE_P` while ~14189+ drops lands.
  `max_fee_drops` defaults to 100000 so the cap clears this floor.
- Every transaction must carry the correct `NetworkID` (21338 testnet /
  21337 mainnet) — the envelope invariants reject anything else.

## Commands

Reading (no credentials, no proposals):

- `balance [address]` — XAH + trustline balances (with reserve math)
- `quote --quote USD --quote-issuer r… [--base XAH]` — order-book both sides
- `offers [address]` — open offers
- `hook [address]` — installed Hooks and their decoded parameters (a
  Hook's `AMOUNT` parameter reveals exactly how much XAH that Hook
  demands — useful before sending to a Hook-guarded destination)
- `network` — ledger index, base fee, reserves
- `reconcile --hash …` — validated outcome of any transaction (prints
  Hook return messages; on `tecHOOK_REJECTED`, also prints Hook
  diagnostics: hook counts on both sides plus the exact native amounts
  the destination accepted before)

Writing (always propose → human reviews hash → payload → phone tap):

- `send --to r… --amount A --ccy XAH [--destination-tag N] [--allow-hooks]`
  — refuses to propose if the destination has Hooks installed unless
  `--allow-hooks` is given (a Hook can reject or redirect your payment)
- `trustline --ccy USD --issuer r… --limit N`
- `claim [--issuer r…]` — monthly XAH balance reward
- `xahau-payload resume --hash <prefix>` — re-check an ambiguous payload
  (timeout/interrupt); never re-sends

Honest scope: only `Payment`, `TrustSet`, and `ClaimReward` builders are
implemented in P1 — the policy allowlist matches. `Remit`, `Import`
(Burn2Mint), `URIToken*`, and `SetHook` are planned for P2 and are
refused by default policy until then.

### xMerch storefronts (`bin/xmerch`)

Read-side only, for [xMerch](https://xmerch.app) storefronts:

- `xmerch shop [--shop <shop>.xmerch.app]` — list products with XAH prices
- `xmerch checkout --product <slug> [--shop <shop>.xmerch.app]` —
  resolve the official xMerch checkout/payment link and hand it off for
  approval

This is a storefront reader plus official-checkout handoff only. **A
direct wallet payment does NOT create an xMerch order** — orders and
fulfillment belong to xMerch; pay only through the official checkout
link unless you have verified vendor API access.

## Testnet quickstart

```bash
python -c "from xrpl.wallet import Wallet; print(Wallet.create().classic_address)"
# fund the new address at the testnet faucet (needs ~1 XAH reserve):
#   https://test.xahauexplorer.com/faucet
xahau setup --address r… --network xahau-testnet
xahau init-policy   # add the test address to destination_allowlist,
                   # set max_fee_drops >= 100000
xahau send --to r… --amount 0.1 --ccy XAH
# review the ceremony (network, to, amount, fee ~0.015 XAH), then:
xahau-payload send --hash <proposal-hash>   # approve promptly in Xaman
```

Notes from live testing: the fee is **Hook-aware**. The proposer takes
3× the advertised median as the generic buffer, then serializes the
*unsigned payment* and asks the `fee` RPC what that exact transaction
requires; it pays the higher of the two. A Hook on the destination can
raise the requirement far above the generic level (verified live:
14167 drops vs 10 for plain accounts) — the probe exists so a Hook
requirement is never underpaid. Note: the Hook-adjusted `median_fee` is
NOT used — it scales with the Hook adjustment and would overpay wildly
(7M+ drops in the live test). A transaction that reaches the
ledger but fails there returns a `tec` code (e.g. `tecHOOK_REJECTED`
when the destination's Hook rejects the payment) — the fee is consumed
in that case. Approve Xaman payloads promptly: proposals expire
~1 minute after creation (`LastLedgerSequence`).

## Setup

```bash
pip install -r requirements.txt   # or use ~/workspace/tools/xrpl-pkgs
xahau setup --address r… [--network xahau-testnet]
xahau init-policy                 # review spend_limits + allowlist!
export XAMAN_API_KEY=… XAMAN_API_SECRET=…
# (from your own Xaman dev-console app — never in a file, never in chat)
```

The payload path refuses to run without the env credentials. That is the
expected state until the operator provides a key.

## The platform boundary (read this)

The phone tap in Xaman is the real human approval — the proposal-hash
review in chat is the pre-check. Client-side spend limits and the
destination allowlist are enforced when the payload is *created*; they
are best-effort, not a vault boundary. `tesSUCCESS` from submission is
provisional until the validated ledger confirms it — `xahau-payload`
waits and reports the validated result, decoding Hook return messages
on failure and treating `tec*` results as fee-only spend. A timeout or
interruption never releases an ambiguous reservation; `resume --hash`
re-checks it. The payload path is tested
against the live Xaman API on testnet (propose → payload → phone tap →
ledger apply confirmed via `tecHOOK_REJECTED` on a Hook-guarded
destination); a clean `tesSUCCESS` confirmation is still pending.

## Xahau specifics

- Native asset XAH, 6 decimals. Base reserve 1 XAH, 0.2 XAH per object.
- Testnet: `https://xahau-test.net` (NetworkID 21338). Mainnet:
  `https://xahau.network` (NetworkID 21337).
- Live now: Hooks, URITokens, Remit (atomic multi-asset payments),
  Import (Burn2Mint XRP→XAH), monthly ClaimReward.
- Not on Xahau: AMM, Escrow, Payment Channels, MultiSign, XLS-20 NFTs.

## Layout

- `bin/xahau` — reads + proposer (never signs)
- `bin/xahau-payload` — Xaman payload manager (no seeds, env keys only);
  `resume --hash` re-checks ambiguous payloads
- `bin/xmerch` — xMerch storefront reader + official checkout handoff
- `bin/xahau_common.py` — envelopes, policy, spend tracker, RPC,
  Hook-aware fee probe, ledger validation
- `tests/test_p1.py` — adversarial logic tests, no network needed
- `tests/test_validation.py` — outcome validation, fee-only failure
  accounting, lifecycle tests (no network, no real addresses)
- `PLAN.md` — the full build plan and locked decisions
