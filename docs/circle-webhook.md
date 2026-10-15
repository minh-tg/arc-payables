# Circle webhook receiver

`POST /webhooks/circle` accepts a signed Circle notification, checks the signature over the exact
request bytes, and records the notification once by id. It is not enabled unless
`PAYMENT_PROVIDER=circle`; on any other profile the route answers `503 webhook_not_configured`.

**Status: unverified live.** No real Circle notification has reached this receiver. The scheme
below is an assumption read from Circle's developer documentation and from the shape of the W3S
API this repository already calls; the direct signature page returned 404 when it was checked, and
no SDK is used. The owner runs the live test at the end of this document before any claim is made
that the integration works against Circle.

## Assumed signing scheme

- The signature is over the raw request body, ECDSA with SHA-256, and is not over a decoded or
  re-encoded payload. The route reads the bytes once and verifies those same bytes.
- `X-Circle-Key-Id` names the signing key. `X-Circle-Signature` is the base64 DER-encoded ECDSA
  signature.
- The public key for that id is base64 DER SubjectPublicKeyInfo. The curve is read from the key, so
  the receiver does not pin one.
- The production resolver fetches the key from `{CIRCLE_API_BASE_URL}/notifications/publicKey/{id}`
  with the configured `CIRCLE_API_KEY`, caches it for `CIRCLE_WEBHOOK_KEY_CACHE_SECONDS`, and treats
  a 404 as an unknown key id. The key and the signature are never written to a log or a response.
- The assumed notification body is
  `{"notificationType": "...", "notificationId": "...", "transaction": {"id": "...", ...}}`.
  `TRANSACTION_CONFIRMED` and `TRANSACTION_FAILED` are the only types acted on. If
  `notificationId` is absent, the replay key is the SHA-256 of the key id plus the exact bytes.

## What the receiver does, and does not do

A verified notification of a handled type is matched to an existing payment by the provider
operation id in `transaction.id`. When that payment is still pending or submitted, the notification
is recorded as `recorded`; with no matching pending payment it is recorded as `unmatched`; any
other signed type is recorded as `ignored`. A second delivery of the same notification id is a
no-op `duplicate`.

The receiver writes one row in `circle_notifications` and nothing else. It does not create a
payment, does not confirm one, and never writes `confirmation_status` from the payload. The payload
status is not settlement proof. The worker's existing chain-checked reconcile remains the only path
that may move a pending payment forward, and it reads the chain and the provider itself.

The status field is deliberately not `scheduled`: a verified notification does not by itself start
the reconcile. The route cannot call the existing reconcile function safely because that function
can reach the provider's submit path when the guard reports no settlement and the permit has not
expired, so the notification is recorded and the worker's periodic reconcile handles the payment.

## Responses

| case | HTTP | body |
| --- | --- | --- |
| recorded, matching pending payment | 200 | `{"status": "recorded"}` |
| duplicate notification id | 200 | `{"status": "duplicate"}` |
| signed, type not handled | 200 | `{"status": "ignored"}` |
| signed, no matching pending payment | 200 | `{"status": "unmatched"}` |
| signature or key id header missing | 400 | `{"error": {"code": "missing_headers", ...}}` |
| signature does not verify, or unknown key id | 401 | `{"error": {"code": "invalid_signature", ...}}` |
| required fields missing, or body not JSON | 400 | `{"error": {"code": "invalid_notification", ...}}` |
| body over `CIRCLE_WEBHOOK_MAX_BODY_BYTES` | 413 | `{"error": {"code": "body_too_large", ...}}` |
| key could not be fetched | 503 | `{"error": {"code": "key_unavailable", ...}}` |
| notification id could not be recorded | 503 | `{"error": {"code": "store_unavailable", ...}}` |
| Circle provider not configured | 503 | `{"error": {"code": "webhook_not_configured", ...}}` |

The error bodies carry a code and a message only. No body echoes the request bytes, a header, a key
or a signature. An unknown key id and a bad signature share one code so the route is not an oracle.

## Owner live test

Run against a testnet deployment with `PAYMENT_PROVIDER=circle` and the Circle credentials set.

1. Start the service and confirm `/ready` reports the Circle provider configured.
2. In the Circle console, subscribe this deployment's URL to the wallet transaction notifications
   for the wallet id in `CIRCLE_WALLET_ID`.
3. Confirm the receiver logs `circle webhook <id> ignored` for a signed notification of a type it
   does not handle, or `unmatched` for one naming no local payment.
4. Send one payment, then confirm the terminal notification arrives and the log line names the
   notification id and outcome only, never a key or a signature.
5. Confirm the payment record is unchanged by the notification itself, and that the worker's next
   reconcile pass is what updates it.
6. Replay the same notification body and confirm the second response is `{"status": "duplicate"}`.
