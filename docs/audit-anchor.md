# Audit-chain anchor

The audit chain in the SQLite store recomputes hash links, so it detects an edit that leaves the
stored hashes alone. It cannot detect someone who rewrites the log and recomputes every hash,
because the verifier and the rewrite use the same algorithm. The anchor records the head of that
chain on Arc, so a later recomputation can be compared with the head published at a known entry
count.

## What an anchor proves, and what it does not

An anchor proves that a supplied chain recomputes to the head published at that entry count, as of
the block the transaction was mined in. It does not prove the invoices are true, it does not cover
entries written after the anchor, and it says nothing about entries written before the first anchor.
It does not survive a chain reorganization, and a later anchor supersedes an earlier one.

## Contract

`src/AuditAnchor.sol` stores the latest head with its entry count, anchor version and chain id, and
emits every anchor, so earlier anchors stay readable from the logs. Only the owner can publish, and
each publish must advance the entry count. The contract holds no funds and makes no external call.
`PaymentGuard` is unchanged and is not involved.

## Read: verify the local chain against the anchor

Run where the audit database lives. No key is needed and nothing is sent.

```sh
export AUDIT_ANCHOR_ADDRESS=0x...                 # deployed AuditAnchor
export AUDIT_ANCHOR_SIGNER=0x...                  # the audit signing address
uv run arc-payables-anchor verify
```

`AUDIT_ANCHOR_SIGNER` is optional. Without it the chain is still hash-verified, but its signatures
are reported as unanchored. `AUDIT_ANCHOR_RPC_URL` defaults to `https://rpc.testnet.arc.io`, and
`DATABASE_PATH` selects the store. `--full-hashes` prints hashes whole. Exit codes: 0 MATCH, 1
MISMATCH or a broken local chain, 3 the published head could not be read.

A MATCH against a longer local chain prints how many newer entries the anchor does not cover. A
MISMATCH prints both heads whole.

## Write: deploy the contract (owner, spends gas)

```sh
export AUDIT_ANCHOR_OWNER=0x...      # the only address that may publish
export DEPLOYER_PRIVATE_KEY=0x...    # pays for the deployment
forge script script/DeployAuditAnchor.s.sol --rpc-url "$RPC_URL" --broadcast
```

Record the deployed address and confirm its bytecode before publishing. The owner may differ from
the deployer.

## Write: publish a head (owner, spends gas)

```sh
export AUDIT_ANCHOR_ADDRESS=0x...
export AUDIT_ANCHOR_SIGNER=0x...
export AUDIT_ANCHOR_PRIVATE_KEY=0x...   # the owner key, read only from the environment
uv run arc-payables-anchor publish             # dry run; sends nothing
uv run arc-payables-anchor publish --send      # one transaction
```

`publish` verifies the local chain first and refuses (exit 2) when it does not verify or when its
signatures are not anchored to the configured signer. The key is never passed as an argument, never
printed and never written to an error message. `AUDIT_ANCHOR_VERSION` sets the anchor version and
defaults to 1.

## Not part of the payment path

Anchoring is a separate tool. It never signs a payment permit, changes invoice state, or calls the
guard.
