# xahau-muse-skill

Let your Muse move XAH and use Xahau apps — with you approving every
transaction on your phone.

This skill lets an AI assistant (Muse, or any agent with a terminal)
interact with the **Xahau network**: send payments, manage trustlines,
mint URITokens, claim the monthly XAH reward, and check out on xMerch
storefronts. It was built so a non-technical owner can hand their Muse
a wallet task and stay in control.

## The safety model

Nothing moves until you tap approve on your phone in the Xaman app —
the skill can only propose transactions, never sign them. Your spending
policy (which network, how much per transaction, which destinations) is
enforced by the software before any sign request is created, and an
empty destination list means no payments at all. Every proposal is
sealed with a cryptographic hash, so anything changed after you review
it is rejected automatically.

There is **no seed and no private key anywhere in this skill, ever.**

## Install

macOS / Linux:

```bash
git clone https://github.com/terramike/xahau-muse-skill
cd xahau-muse-skill
./install.sh
```

The installer checks Python, installs what's needed (using a private
environment when your system requires it), puts `xahau`,
`xahau-payload`, and `xmerch` on your PATH, and runs the test suite as
a self-check. Re-running it is always safe.

Windows (PowerShell) — three manual steps:

```powershell
git clone https://github.com/terramike/xahau-muse-skill
cd xahau-muse-skill
pip install -r requirements.txt
python bin/xahau --help   # sanity check
```

Then use `python bin/xahau`, `python bin/xahau-payload`, `python bin/xmerch`
in place of the bare commands below.

## Your first payment (testnet — free, safe)

```bash
xahau setup --address rYOURADDRESS --network xahau-testnet
xahau init-policy
```

Open `~/.xahau/policy.json` and add a second address you control to
`destination_allowlist` (testnet starts locked: empty = no payments).
Fund both addresses at the [testnet faucet](https://test.xahauexplorer.com/faucet)
(a second account in Xaman works fine — payments to yourself are
rejected by the ledger as redundant), then:

```bash
export XAMAN_API_KEY=… XAMAN_API_SECRET=…   # your own Xaman dev-console app
xahau send --to rSECONDADDRESS --amount 0.1 --ccy XAH   # review the proposal!
xahau-payload send --hash <proposal-hash>              # approve the tap in Xaman
```

Read the proposal carefully before the tap — that review is the whole
point. Full walkthrough: [`QUICKSTART.md`](QUICKSTART.md).

## What it can do

- **Payments** (`xahau send`) and multi-asset **Remit**
- **Trustlines**, **URIToken** mint/buy/burn, monthly **ClaimReward**
- **Burn2Mint Import** (Xahau-side; the XRPL burn stays manual)
- **Hook awareness**: inspect installed Hooks, refuse blind sends into
  Hook-guarded destinations, diagnose `tecHOOK_REJECTED`
- **xMerch**: browse public storefronts, pay via official checkout

## What it won't do

- Never asks for, receives, or stores a wallet seed.
- Never installs or modifies Hooks on your account (Hook *awareness*
  only — inspection, gating, diagnostics).
- xMerch vendor order/fulfillment APIs are not included.
- Mainnet is a deliberate opt-in: `network_lock` stays `xahau-testnet`
  until you change it yourself.

## Docs

- [`QUICKSTART.md`](QUICKSTART.md) — one page, first payment
- [`SKILL.md`](SKILL.md) — the full operator manual
- [`PLAN.md`](PLAN.md) — design decisions and build history

## Proven on mainnet

v0.1.0 completed a clean end-to-end mainnet payment through Xaman:
propose → payload → phone tap → validated `tesSUCCESS`
([`9BB40E4F…00CF`](https://xahauexplorer.com/tx/9BB40E4FB19A01CC01089671224506E875582B3DF6B586147B4612B5538C00CF),
0.1 XAH, fee 0.015 XAH, ledger 26055772).

## License

MIT — see [LICENSE](LICENSE).
