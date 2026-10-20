# Webhook Chaos Lab

**Your webhook returned 200. Did it create the same side effect sixteen times?**

A dependency-free Python CLI for deterministic webhook failure drills. Deliver duplicate,
reordered and concurrent test events, lose a response **after a durable write**, and assert
what actually happened using a separate readback endpoint.

No signup. No provider account. No payment. Runs entirely on loopback by default.

## Try it in one command

Python 3.10+ required. From this checkout:

```sh
python -m webhook_chaos_lab demo
```

The demo starts an ephemeral loopback server and SQLite database, runs identical drills
against intentionally broken and fixed handlers, then removes its temporary database.

```text
Intentionally broken receiver: FAIL
  FAIL duplicate (readback_matches)
  FAIL reordered (readback_matches)
  FAIL concurrent (readback_matches)
  FAIL timeout_after_write (readback_matches)

Fixed transactional receiver: PASS
  PASS duplicate
  PASS reordered
  PASS concurrent
  PASS timeout_after_write

Demo PASS: detected all broken cases; fixed receiver passed.
```

Default runtime is approximately 17 seconds, including an observation window after each
drill. Expected broken-case failures make the **demo succeed**, not fail CI.

Optional install from your checkout (no runtime dependencies):

```sh
python -m pip install .
webhook-chaos demo --json report.json
```

## What it actually tests

| Drill | Delivery pattern | Required durable outcome |
| --- | --- | --- |
| Duplicate | Same event 3 times | 1 effect, 1 recorded event ID |
| Reordered | Sequence 2, then sequence 1 | 2 unique effects; latest state stays `fulfilled` |
| Concurrent | Same event 16 times across 8 workers | Exactly 1 effect |
| Timeout after write | Commit, delay response 400 ms, client times out at 100 ms, read back, retry | Commit visible before retry; still exactly 1 effect |

Every drill uses an isolated, random `entity_id`. The receiver cannot pass by acknowledging
requests alone. Both the effect count and event IDs are checked, along with the final state.
Readback is polled through the configured observation window; the final sample must match.
This is a bounded check, not proof that no future asynchronous duplicate can occur.

The demo's readback uses a separate database connection to a durable effects ledger. The
fixed receiver puts event deduplication, side effects and monotonic state updates in one
SQLite transaction. The broken receiver deliberately has neither deduplication nor a
monotonic-state guard. For external effects such as email or another service, a database
transaction alone is insufficient; adapt your real outbox/idempotency mechanism and read
its actual destination effects.

## Test your receiver through a staging adapter

This initial release uses an explicit **provider-neutral test contract**, not arbitrary
Stripe payloads, signatures or a webhook proxy. Put a small adapter in an isolated test
application. Do not point it at production or payment-processing endpoints.

```sh
python -m webhook_chaos_lab run \
  --target http://127.0.0.1:8000/test-events \
  --readback http://127.0.0.1:8000/test-effects \
  --json report.json
```

The target receives JSON such as:

```json
{
  "id": "chaos-<run>-duplicate-e1",
  "entity_id": "chaos-<run>-duplicate",
  "sequence": 1,
  "state": "created",
  "test": true
}
```

Sequence 2 has state `fulfilled`. Each unique event represents one test side effect. A
late older event must not regress the aggregate state. If your business semantics differ,
map this fixture explicitly in your test adapter rather than asserting artificial success.

The CLI GETs the readback URL with an appended `entity_id` query parameter. Existing query
parameters are preserved. Return HTTP 200 JSON with:

```json
{
  "effect_count": 1,
  "applied_event_ids": ["chaos-<run>-duplicate-e1"],
  "state": "created"
}
```

`applied_event_ids` must be sorted and must reflect actual durable effects, including any
duplicates. Never return the expected fixture, request-attempt count, or only a deduplication
marker. For reordered delivery, return both IDs sorted, count 2 and state `fulfilled`.

For the timeout drill, the adapter must honor `X-Chaos-Delay-Response-Ms: 400` **after the
side effect has committed**, before writing the HTTP response. The CLI proves a client
timeout occurred and independently observes the commit before retrying. If that fault
injection is unsupported or the write was not visible, the drill fails explicitly; it
cannot silently pass a normal retry as a timeout-after-write test. Never enable this
fault-injection hook on public production routes.

HTTPS remote staging requires deliberate opt-in with `--allow-remote`. There is no auth,
secret management, arbitrary header injection or provider credential support in v0.1.
Use loopback or an appropriately isolated HTTPS test adapter. This URL check is an
accidental-target guard, **not** a network security boundary. Redirects are not followed.
The tool creates persistent test effects on external targets; you own cleanup of the
reported entity IDs. It never deletes your data.

## Reports and CI

```sh
python -m webhook_chaos_lab demo --json demo-report.json
python -m unittest discover -s tests -v
```

- Exit **0**: all assertions passed (or the demo proved both expected outcomes).
- Exit **1**: failed assertion, delivery error, missing readback or unavailable fault injection.
- Exit **2**: invalid arguments/configuration, malformed JSON or report write failure.

The JSON includes schema version, unique run ID, scenario entity IDs, expected/observed
outcomes, each individual check and delivery results. Reports omit arbitrary response bodies and retain only contract fields; unexpected state
and event IDs are replaced by markers. Review reports before sharing publicly. Set
`--settle-timeout 5` for a five-second observation window per scenario (maximum 60).
Network timeouts can extend total runtime beyond the observation window.

## Scope and limits

This is a fault-drill harness, not a production receiver, payment simulator, load test,
security audit, exactly-once guarantee or certification. It does not test signature
verification, replay freshness, every event schema, provider backoff or queue durability.
Concurrency scheduling is nondeterministic; the test pattern and assertions are fixed.
Passing four drills is evidence for those fixtures only. Use integration tests against
your actual side-effect destinations and repeat under realistic staging conditions.

## Development

```sh
python -m unittest discover -s tests -v
python -m webhook_chaos_lab demo --json demo-report.json
```

Please keep contributions focused on measurable outcomes. A new drill should include a
broken implementation that fails, a fix that passes and independent readback assertions.

MIT licensed. See [LICENSE](LICENSE).

## Background

Real webhook integrations must handle retries, duplicate deliveries and unordered events.
Stripe describes these behaviors in its [official webhook documentation](https://docs.stripe.com/webhooks).
This lab demonstrates the general failure patterns; it is not affiliated with Stripe and
its fixtures are not Stripe-compatible payloads.
