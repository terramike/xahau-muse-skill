# xahau-muse-skill

Interact with the Xahau network — payments, trustlines, Hooks, xMerch
storefronts, and the monthly XAH ClaimReward — with a hard safety
boundary between **proposing** an action and **approving** it.

Built for AI agents (Muse and friends — anything with a terminal), but
safe for humans too.

## The idea

- **`xahau`** reads the network and *proposes* transactions. It builds
  the tx, autofills it against the live network (fee, sequence,
  last-ledger, `NetworkID`), shows you everything, and seals it in a
  **hash-bound proposal envelope**. It never signs, never submits, never
  sees a seed.
- **`xahau-payload`** takes a human-approved proposal hash, re-verifies
  the envelope, enforces `~/.xahau/policy.json` (spend limits,
  destination allowlist), reserves spend atomically, and creates a
  **Xaman sign request**. You approve the tap **on your phone** in the
  Xaman app — that tap is the real approval. The program then verifies
  the *validated* ledger result and appends to `~/.xahau/audit.log`.
- **`xmerch`** reads public xMerch storefronts and hands off the
  official checkout link for approval.

There is **no seed and no private key anywhere in this skill**.

## Quickstart (testnet)

```bash
pip install -r requirements.txt
python bin/xahau setup --address r… --network xahau-testnet
python bin/xahau init-policy   # review spend_limits + allowlist!
export XAMAN_API_KEY=… XAMAN_API_SECRET=…   # your own Xaman dev-console app
python bin/xahau send --to r… --amount 0.1 --ccy XAH
# review the ceremony, then:
python bin/xahau-payload send --hash <proposal-hash>  # approve in Xaman
```

Full docs: [`SKILL.md`](SKILL.md). Build plan and decisions: [`PLAN.md`](PLAN.md).

## Safety highlights

- Hash-bound proposals: any modification after human review is rejected.
- Destination allowlist (empty = no payments) + per-tx / rolling-24h
  spend limits, enforced when the payload is created.
- Hook-guarded destinations are refused unless `--allow-hooks` is given.
- Hook-aware fees: the exact transaction is probed against the `fee`
  RPC so a Hook-raised requirement is never underpaid.
- Ambiguous payload outcomes never release the spend reservation —
  `xahau-payload resume --hash …` re-checks them.

## License

MIT — see [LICENSE](LICENSE).
