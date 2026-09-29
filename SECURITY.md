# Security policy

This project moves money, so the interesting failures are the ones that move it wrongly. Please
treat anything in that category as security-relevant rather than as a bug report.

## Reporting

Report privately through GitHub's [security advisories](../../security/advisories/new) rather than
a public issue. Include what you did, what happened, and what you expected. If a credential may
have been exposed, say so plainly in the first line and rotate it before anything else.

There is no bounty and no formal SLA; this is a testnet project and it is not running anyone's
production treasury.

## What is in scope

* Anything that lets a payment be authorized without the deterministic policy passing.
* Anything that lets a payment destination, or an amount, be set by invoice data, an uploaded
  document, a supplier record that was never verified, or a language model.
* Anything that causes a second payment for one authorization, or that reports a settlement that
  did not happen.
* Anything that lets the audit chain be rewritten without detection, or that makes a rewrite look
  verified.
* Anything that exposes a signing key, a Circle entity secret, or an API credential to a caller,
  a log, the repository, or a model's context.

## What is not in scope

* Mainnet. There is no mainnet route, and the guard refuses to deploy off Arc Testnet by
  construction.
* A local operator with database access deleting or editing their own records. The audit chain
  makes that detectable, not prevented.
* Testnet USDC having no value, which is the point of it.

## Hard rules the code already assumes

* A signing key is read only by the payment service. The advisory layer has no signing tools and
  never receives one.
* `SIGNER_BACKEND=kms` fails closed rather than falling back to a local key.
* Any configuration that would allow an unbounded payment is refused at deployment: the guard
  cannot be deployed without an explicit budget, and an epoch cap smaller than a single payment is
  rejected.
* Credentials belong in the environment, never in the repository. `LICENSE`, `.gitignore` and CI
  are configured so a secret in the tree is a mistake rather than a convenience.
