# Hosted mock console

A public deployment of the console and API, running only the mock payment and accounting
providers with fixture screening.

Live at <https://arc-payables-hosted-demo.onrender.com/console/> on Render's free plan. It was opened signed out in a fresh browser on
2026-10-15: the console loads, the header reads "Hosted demo · simulated payments & ledger", the
banner shows on Overview, and `/health`, `/setup`, `/invoices` and `/payments` answer. A free
instance sleeps after about 15 minutes idle. The next request takes about a minute while it starts,
and the data is recreated from the seed. Open the URL a minute before showing it to anyone.

It was created from this repository with a Docker web service using `deploy/hosted-demo/Dockerfile`,
the free plan, auto deploy off and `HOSTED_DEMO=1`. No secret is set on the service.

## What the profile refuses

`HOSTED_DEMO=1` turns on a startup check. The process exits nonzero before serving anything when:

- `PAYMENT_PROVIDER` is not `mock`
- `ACCOUNTING_PROVIDER` is not `mock`
- `SCREENING_PROVIDER` is not `fixture`
- any live credential or endpoint is set: the Circle fields, the Frappe fields, the planner URL,
  model or key, `OPENSANCTIONS_API_KEY`, `ALERT_WEBHOOK_URL`, `PERMIT_SIGNING_PRIVATE_KEY`,
  `LOCAL_PAYMENT_PRIVATE_KEY`, `PAYMENT_GUARD_PAUSER_KEY` or `PKCS11_USER_PIN`

The failure message names the variable and never prints its value. A host that injects its own
environment variables therefore stops the container instead of starting a console that could reach
a live provider. The flag only labels the console; it does not enable anything.

## Files

- `deploy/hosted-demo/Dockerfile`: the image. It sets the mock profile and `HOSTED_DEMO=1`.
- `deploy/hosted-demo/entrypoint.sh`: seeds a disposable database under `/tmp`, runs one mock worker
  pass so the settlement log has a simulated payment, then serves the API and console on `$PORT`.
- `deploy/hosted-demo/render.yaml`: a free Render web service. Render reads `render.yaml` from the
  repository root, so copy this file there before creating the Blueprint.

## Local smoke before deploying

```sh
podman build -f deploy/hosted-demo/Dockerfile -t arc-payables-hosted-demo .
podman run --rm -p 8080:8000 arc-payables-hosted-demo
curl -s http://127.0.0.1:8080/setup | python3 -m json.tool
# http://127.0.0.1:8080/console/
```

`/setup` must report `deployment.hosted` true, both providers `mock` and screening `fixture`.
Confirm the refusal too, which must exit nonzero and print only the variable name:

```sh
podman run --rm -e PAYMENT_PROVIDER=circle arc-payables-hosted-demo
# RuntimeError: Hosted demo requires PAYMENT_PROVIDER=mock.
```

## Owner steps

1. Create the Render account and connect this repository.
2. Copy `deploy/hosted-demo/render.yaml` to the repository root, or point the Blueprint at that path.
3. Leave the free plan and the health check path `/health`; add no secret.
4. Deploy, open the URL, and confirm the Overview banner and the header label both read as a hosted
   demo, and that `/setup` reports the mock providers.
5. Keep the free instance's spin-down and ephemeral disk in mind: the database resets on restart.

## What this does not claim

The hosted console is a labelled simulation. It does not pay real invoices, hold customer data, or
run any live provider. Provider selection, the mock-settlement label and the disposable database are
the honesty controls; the URL itself is only reachable after the owner deploys it.
