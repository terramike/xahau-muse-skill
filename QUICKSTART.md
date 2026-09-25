# QUICKSTART — your first Xahau payment

One page. About ten minutes. Everything happens on **testnet**, where
the money is free.

## 1. Install

```bash
git clone https://github.com/terramike/xahau-muse-skill
cd xahau-muse-skill
./install.sh        # macOS/Linux. Windows: see README "Windows" section.
```

## 2. Get testnet XAH

Create a Xahau testnet address in your Xaman app (Settings → network →
Xahau Testnet), then fund it at the
[testnet faucet](https://test.xahauexplorer.com/faucet).

## 3. Get a Xaman app key

Free at the [Xaman developer console](https://apps.xaman.dev).
You need the **API key** and **API secret** of your own app — they stay
in your shell, never in a file:

```bash
export XAMAN_API_KEY='…' XAMAN_API_SECRET='…'
```

## 4. Point the skill at your address (testnet, locked down)

```bash
xahau setup --address rYOURADDRESS --network xahau-testnet
xahau init-policy
```

Now open `~/.xahau/policy.json` and make two edits:

- `destination_allowlist`: add `{"address": "rSECONDADDRESS"}` —
  payments are refused until the destination is listed. (Use a second
  address you control — a second account in Xaman works fine. Payments
  to yourself are rejected by the ledger as redundant.)
- Leave `network_lock` on `xahau-testnet`. Mainnet is a deliberate
  opt-in you make later, by hand.

## 5. Propose, review, tap

```bash
xahau send --to rSECONDADDRESS --amount 0.1 --ccy XAH
```

Read the proposal — network, account, amount, fee. Then:

```bash
xahau-payload send --hash <the-proposal-hash>
```

Approve the tap in Xaman on your phone **within about a minute**
(proposals expire). The program verifies the validated ledger result
and logs it to `~/.xahau/audit.log`.

Two of your own addresses keeps the first test counterparty-free while
exercising the whole pipeline: propose → payload → phone tap → ledger.

## 6. If something looks wrong

- `xahau-payload spend-status` — what the spend ledger thinks happened.
- `xahau-payload resume --hash <proposal-hash>` — re-check an unclear
  outcome against the ledger.
- Nothing here can spend without your phone tap. When in doubt, reject
  the tap in Xaman and investigate.

## Going further

`SKILL.md` is the full operator manual: trustlines, URITokens, Remit,
ClaimReward, Import, Hook diagnostics, xMerch. Mainnet works exactly
like above — the only differences are `--network xahau-mainnet`,
real XAH, and your deliberate `network_lock` edit.
