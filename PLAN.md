# Xahau Muse Skill — Build Plan (draft, 2026-09-24)

Goal: a public, distributable agent skill so other people's muses can interact
with the Xahau network — signing via the user's Xaman wallet, with xMerch
integration for Mike's shop (Terramike.xMerch.app). Patterned on the xrpl skill
(github.com/terramike/xrpl-muse-skill) but NOT a trading-skill clone.

## Research basis (verified 2026-09-24, sources in chat summary)

- Xahau: rippled fork ("xahaud"), Xahau Launch Alliance. Mainnet Oct/Nov 2023.
  Network IDs: mainnet 21337, testnet 21338. Native XAH (6 decimals).
  Reserves: 1 XAH base, 0.2 XAH per object. Base fee 10 drops (burned).
  r-addresses identical model to XRPL. Hooks live on mainnet. URIToken is the
  NFT standard (XLS-20 disabled). Order-book DEX retained; AMM (XLS-30) NOT
  live. Retired: Escrow, PayChan, MultiSign, CryptoConditions, TickSize.
  `Remit` (atomic multi-asset payments) live. Monthly ClaimReward (~0.34% p.a.
  balance adjustment, 30-day cooldown, offsets Hook fees). Burn2Mint (XRP->XAH
  one-way bridge) live. EVM sidechain: none on Xahau (XRPL's went live
  June 2025 separately).
- Xaman wallet dev API: backend payload flow (POST /platform/payload with
  X-API-Key/Secret -> uuid + deeplink/QR -> user approves on phone with
  biometric/PIN -> poll/WebSocket/webhook -> signed). Xaman submits by default
  (options.submit). Plain backend payloads fit an agent skill; xApps are for
  in-wallet UI. Xahau txs work via NetworkID: 21337 in txjson (+ options
  force_network). Docs: docs.xaman.dev. Universal SDK `xumm` (npm) is current.
- Distribution: ONE registered app serves many users — any Xaman user can open
  any app's payload QR/deeplink and sign. Push needs a per-user user_token from
  prior interaction (~30 days). Two models: (a) shared app (Mike's key, shared
  quota, QR-first for strangers) vs (b) bring-your-own app key (each user's
  own quota, push from the start). Pricing/quotas NOT published — must confirm
  before committing to (a).
- xMerch: wallet-native commerce for Xahau+XRPL vendors, run by Meister
  (MWorks). Subdomain-per-approved-vendor (approval-gated intake). Mike's shop
  terramike.xmerch.app verified rendering. Accepts XAH/XRP via Xaman + Stripe
  cards. Fees: 1.5% vendor + 1.5% buyer handling. There is NO public vendor
  REST API / webhooks — vendor ops live behind the Vendor Dashboard. There IS
  a dev surface: xMerch CLI (npm `xmerch`), xBase starter (Next.js 16, Xaman
  payments), devs.xmerch.app. So v1 "integration" = read public shops
  (server-rendered) + create Xaman payment payloads to vendors + verify
  settlement on public RPC/explorers. Order/listing management is not
  programmatically verifiable today.
- Ecosystem context: Ethiopia remittance corridor (TerraPay + Cooperative Bank
  of Oromia, settling EUR->ETB on Xahau, May 2026) is the flagship deployment.
  Roadmap (Apr 2026) frames Xahau as enterprise/regulated-payments. XAH
  liquidity: ~2 exchanges (Bitrue, CoinEx), ~$4.6k 24h volume, ~$0.013-0.015.
  Funding routes: Bitrue/CoinEx, C14 "XAH On Demand" xApp (fiat->XAH), or
  Burn2Mint from XRP.

## Proposed architecture: propose -> payload -> phone-approve

Core insight: with Xaman there is NO local seed. The signer program becomes a
payload manager, and the phone tap is the real approval.

1. `xahau` (proposer): builds txs against live Xahau RPC (NetworkID 21337),
   prints the full ceremony (account, network, assets+issuers, amounts, fee,
   expiry), saves a hash-bound proposal envelope (canonical binary + metadata,
   same tamper-evidence idea as xrpl-proposal/3). Never submits.
2. Payload manager: takes an approved proposal -> POST /platform/payload
   (txjson with NetworkID, options.expire, options.submit) -> returns the
   deeplink + QR for the user to scan -> polls/WebSocket for resolution ->
   reports validated result via public RPC `tx` lookup -> appends to audit log.
3. Policy (`~/.xahau/policy.json`): tx-type allowlist adjusted for Xahau
   (OfferCreate/OfferCancel/TrustSet/Payment/URITokenMint/URITokenBuy/
   URITokenBurn/Remit/ClaimReward/Import — locked), per-asset per-tx + rolling-24h spend limits (fail-closed),
   destination allowlist, network lock (testnet default, mainnet hard-fail
   until opt-in). Policy gates payload CREATION; the phone tap is the human
   approval; proposal-hash review in chat is the pre-check.
4. `xmerch` companion: `shop` (read public storefront products/prices),
   `pay --to <vendor> --amount` (Xaman payment payload + settlement verify).
   NO order/listing management in v1 (no public API).
5. Read-only commands: balance, quote (order-book), offers, reconcile,
   inspect-token (issuer risk), hooks-info?, claimreward status.

## Safety posture (mirrors xrpl skill, adapted)

- Same adversarial test-suite pattern (payload lifecycle: reject/expire/
  tamper/network-mismatch; policy fail-closed; hash binding) + testnet e2e.
- Honest platform boundary in a SECURITY.md: the chat ceremony is a pre-check,
  the phone tap is the approval; spend limits enforced client-side are
  best-effort; state it out loud.
- NEVER invent: API pricing, quotas, xMerch write capabilities, listings.

## Phases

- P0 — Confirm unknowns: Xaman dev-console URL + payload API pricing/quotas
  (no signup/spend without Mike's say-so); multi-sig-on-Xahau status;
  live `feature` RPC as amendment ground truth.
- P1 — `xahau` core: read commands + propose/payload/poll/resolve + policy
  file + audit log, on Xahau testnet.
- P2 — `xmerch` companion: shop read + vendor payment + settlement verify.
- P3 — Adversarial tests + testnet e2e; SECURITY.md; neutralized public repo.
- P4 — Publish (github.com/terramike/xahau-muse-skill), wiki/install copy.

## Decisions (locked 2026-09-24, Mike's word)

1. **Distribution: bring-your-own app key.** Each user registers their own
   (historically free) Xaman app. Mike already has a Xaman dev account from
   XRPixel Jets (xApp: xahau.teleport) — usable for skill dev.
2. **Scope: payments/commerce/Hooks + xMerch.** No DEX trading focus.
3. **Cover Burn2Mint (XRP->XAH) and monthly ClaimReward.** Cheap wins, in scope.
4. **xMerch v1: read-side + payments.** If xMerch notices, Mike asks Meister
   about vendor API access.

## Amendment ground truth (verified 2026-09-24 via live `feature` RPC)

Enabled: Hooks, HooksUpdate1, HookCanEmit, ExtendedHookState, Cron,
BalanceRewards, URIToken, Remit, Import (=Burn2Mint), ZeroB2M, PriceOracle,
TicketBatch, Checks, Clawback, DeepFreeze, DepositAuth, DisallowIncoming,
Flow/FlowCross.
Disabled/retired: AMM (AMMClawback), Escrow, PayChan, MultiSign
(MultiSignReserve enabled but MultiSign itself disabled — do NOT advertise
multisig), XLS-20 (NonFungibleTokensV1), TickSize, CryptoConditions,
HooksUpdate2, HookOnV2, NamedHooks, TrustSetAuth, DeletableAccounts.
Skill tx-type allowlist must reflect this — it differs from the xrpl skill's.

## P1 build status (built 2026-09-24)

Built: SKILL.md, requirements.txt, bin/xahau (reads + proposer),
bin/xahau-payload (payload manager), bin/xahau_common.py,
tests/test_p1.py — 39/39 adversarial unit tests pass, no network needed.

Verified live against xahau-test.net: `network` (ledger 12614654,
NetworkID 21338, 10-drop base fee, 1/0.2 XAH reserves), `quote`
(empty-book handled), `balance` (funded + actNotFound error path),
`reconcile` on a real validated URITokenMint (tesSUCCESS), and the full
propose path — autofill (fee/sequence/last-ledger/NetworkID) + hash-bound
envelope saved, nothing submitted. Payload path is code-complete but
BLOCKED on XAMAN_API_KEY/XAMAN_API_SECRET; it refuses cleanly without
them. Policy gates verified end-to-end (empty destination allowlist
denied a payment before any key check). Nothing published, mainnet
untouched, real ~/.xahau never created (smoke tests used a temp HOME).

Deliberate deviation: envelopes bind SHA-256 of the canonical JSON tx,
not the binary codec — xrpl-py 5.2.0's codec does not know Xahau-only tx
types (URIToken*, SetHook, Remit, ClaimReward, Import; verified by direct
encode() test). Equally tamper-evident; documented in SKILL.md.

Xahau RPC quirks found live (encoded in the code): server_info reports
reserves in whole XAH (not drops) and base_fee_xrp as XAH; account_info
uses capitalized `Sequence`; `ledger current` returns
`ledger_current_index`.

---

## P1 merge (2026-09-24): best of both implementations

Per Mike's "Yes, merge the best of both", the Codex review copy was
merged INTO this working copy (not the reverse). Codex's copy stays at
`~/workspace/xahau-codex-review/xahau-muse-skill/` for reference; nothing
was merged without deliberate review.

**Taken from Codex:**
- Hook-aware fee probe: serialize the unsigned Payment, ask the `fee`
  RPC for that exact transaction, pay max(generic, required). Verified
  live (hook'd dest required 14167 drops; plain 10 drops).
- `xahau hook ADDRESS` — installed Hooks + decoded parameters (AMOUNT →
  XAH). Verified live against the NFT-hook destination.
- `validated_outcome` — field-by-field proposal-vs-ledger comparison
  including delivered_amount; `tec*` treated as fee-only failure.
- `finish_payload` + `xahau-payload resume --hash`: ambiguous outcomes
  (timeout, crash, missing txid) NEVER release the spend reservation.
- `fail_tx` — validated failure consumes only the fee.
- `bind_payload` / `pending_for_proposal` — payload UUID linked to the
  reservation; duplicate send refused with a resume pointer.
- `_prune` never expires pending reservations.
- `hook_messages` — decoded Hook return strings.
- XAH precision (≤6 decimals), ≥12-hex-char proposal prefixes,
  `XAHAU_DATA_DIR` env override.
- `bin/xmerch` — xMerch storefront reader + official checkout handoff.
- SKILL.md frontmatter; honest P1 tx-type scope.

**Kept from this copy (Codex regressions avoided):**
- `--allow-hooks` destination gate (Codex dropped it).
- `hook_diagnostics` accepted-amount scan (Codex replaced it with
  return-strings only; both are complementary and both are kept).
- Testnet quickstart docs and full P1 test suite.

**Deliberately changed beyond both:**
- Fee logic: the blind 15000-drop floor is REMOVED. `choose_fee` =
  max(3×median, base, 10); Payment pays max(generic, tx-specific
  required). Codex's `max(generic, required)` kept the floor and never
  actually paid the exact requirement — now it does on quiet networks.
- `DEFAULT_ALLOWED_TX_TYPES` narrowed to ["Payment", "TrustSet",
  "ClaimReward"] — the only P1 builders. Existing policy.json files
  are not force-migrated.
- All tests use freshly generated addresses; no real accounts anywhere.

**Tests:** 54/54 test_p1.py + 17/17 test_validation.py = 71/71, no
network. Live read-only verification: `xahau hook` decoded the 36 XAH
AMOUNT requirement; fee probe paid 0.015 XAH correctly for both hook'd
and plain destinations on testnet.

**P2 (Remit done 2026-09-24 — builder, shape schema, multi-asset spend
accounting, hook gate, 41/41 tests, live testnet proposal verified;
URITokens done 2026-09-24 — mint/buy/burn builders, exact price-match and
ownership ledger pre-checks, 0.2 XAH reserve-lock warning, 47/47 tests,
live testnet proposal verified):**
Import/Burn2Mint (done 2026-09-25, Option A — manual XPOP), deeper
xMerch. SetHook deliberately cut 2026-09-25: hook installation is
persistent unauditable code — wrong risk profile for a user-friendly
payments/commerce skill; hook *awareness* stays. Published to GitHub
(terramike/xahau-muse-skill).
