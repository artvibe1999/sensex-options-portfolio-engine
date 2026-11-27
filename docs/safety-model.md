# Safety model

- Every process start is paper mode, regardless of persisted state or environment values.
- Live mode is runtime-only and requires an allowlisted Telegram user to request and separately confirm it.
- The confirming user must match the requester. Broker authentication, reconciliation, and risk checks must pass.
- All Telegram commands require both user-ID and chat-ID allowlisting. Dangerous actions need explicit confirmation and audit records.
- Stale data, invalid option chain, stale contract master, uncertain regime, missing margin, unreconciled broker state, or risk-limit breach means no new trade.
- Emergency close is a managed workflow: halt entries, reconcile, inspect outstanding orders and all legs, protect short exposure, cancel/replace where appropriate, close in controlled sequence, then verify broker positions. Never report success until broker state confirms it.
- Partial fills, rejected orders, rate limits, disconnects, and restart recovery are first-class states.
- Broker submission is claimed durably before the external call. A timeout or uncertain response is never retried automatically; exact client-key lookup and reconciliation are required.
- Live broker submission is disabled by default. An acknowledgement is not a fill, and must not unlock the next multi-leg order.
- Secrets must be injected at runtime and must never be committed.
- Defined-risk payoff at expiry does not eliminate legging, liquidity, slippage, assignment/settlement, or overnight gap risks.


## Broker fill ingestion boundary
- Match broker order IDs against all durable intents, including cancelled intents that may receive late fills.
- Verify instrument token, trading symbol and side before recording a fill.
- Namespace broker trade IDs by broker order ID and rely on durable idempotency across replays.
- Reject unknown/ambiguous order IDs, malformed fills, mismatched contracts and overfills.
- Allow partial close orders only within independently confirmed remaining position exposure.
- A broker acknowledgement is never a fill. Only independently ingested fill events update position exposure.
- This is not a Kite event-stream integration; live execution remains disabled until adapter semantics, event delivery, reconciliation and failure recovery are validated.


## Durable broker fill inbox

Broker fill ingestion now uses a durable SQLite inbox. Each broker-order/trade identity
and its normalized payload are committed before order/position projections are changed.
Conflicting payload reuse is rejected. Receipts move through RECEIVED, PROCESSING and
PROCESSED states; processing claims use a bounded lease so an abandoned PROCESSING
receipt can be reclaimed after a crash. Failed projections return the receipt to
RECEIVED. The order ledger remains the first fill fact and the position projection
is replayed idempotently, allowing recovery after interruption between the two writes.
This inbox is not a broker event transport: a future Kite adapter must still provide
reliable event acquisition, reconnect/backfill, and reconciliation for missed events.


### Stale-worker fencing

Inbox claim attempts also act as fencing tokens. Completion and failure updates must
match the exact attempt number that acquired the lease, so a stale worker cannot
finalize or reset a receipt after another worker has reclaimed it. A stale worker
may still finish an idempotent projection replay, but it cannot change the newer
worker's inbox state.
