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



## Kite trade-history fill backfill

`KiteTradeHistoryIngestor` is a read-only transport adapter around Kite's per-order
`order_trades(order_id)` API. A caller supplies explicit internal order IDs for
the current polling cycle; the adapter resolves each to its durable broker order ID,
fetches individual exchange trades, validates BFO, order ID, token, symbol, side,
quantity, price and fill timestamp, then sends normalized events through the durable
fill inbox. The fill phase is derived by comparing the durable order side with its
original position-leg side (same side = OPEN; inverse side = CLOSE).

This adapter does not place, modify or cancel orders and does not create a polling
loop. Callers must select current-session order IDs: Kite's order and trade books
are transient and available for the current day only. Persisted inbox receipts and
portfolio events remain the durable local record. Repeated polling is idempotent
through the broker order/trade ID inbox key. WebSocket/postback acquisition,
reconnect scheduling, missed-event orchestration and production runtime wiring
remain separate work.



## Restart-safe current-session trade-history discovery

Order intent projections now expose the immutable SQLite event creation timestamp.
`KiteTradeHistoryIngestor.poll_current_session(session_date=...)` scans all durable
order snapshots, selects correlated LIVE intents created on the explicit IST session
date, and backfills each order's individual Kite trade history. It deliberately
includes canceled/rejected terminal intents so late fills after cancellation are not
lost. It excludes paper intents, uncorrelated intents and prior-session orders.
The caller must supply the session date and schedule repeated calls; this helper is
not itself a scheduler and does not replace asynchronous Kite postbacks/websockets.


## Ordered Kite startup recovery

`KiteStartupRecoveryService` provides an explicit startup orchestration primitive:
(1) replay durable pending fill-inbox receipts, (2) backfill all correlated LIVE
orders created during the current IST session using Kite per-order trade history,
and (3) reconcile durable positions and orders against read-only broker state.
It refuses non-current session dates, stops before broker polling if local receipt
replay fails, and returns ready only when the final broker reconciliation passes.
The caller must keep entries paused unless `StartupRecoveryResult.ready` is true.
This service does not schedule itself, enable live mode, or submit/cancel orders;
runtime wiring, recurring polling, and operational supervision remain required.


Safety clarification: the startup result field is named `recovery_complete`, not
`ready`. A successful reconciliation is not live readiness and does not authorize
resuming entries. The full `LiveReadinessService`, risk clearance, Telegram
allowlisting and separate live confirmation remain independent mandatory gates.
