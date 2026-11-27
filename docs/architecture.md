# Architecture

The application is split into domain, market-data/broker ports, strategy eligibility, portfolio risk, execution, persistence, and Telegram control boundaries.

## Data flow

1. Load the current BSE contract master and validate SENSEX option instruments, expiry, lot size, tick size, and tradingsymbol.
2. Use the read-only Kite adapter to load the BFO instrument master and fetch quotes by exchange-qualified tradingsymbol. Filter to SENSEX CE/PE contracts and retain exchange-provided expiry, lot size, tick size, strike and instrument token. The adapter does not place orders.
3. Build option-chain snapshots from supported broker quotes/instruments. Kite Connect capabilities and rate limits must be verified; do not assume a native complete option-chain endpoint.
4. Validate quote freshness, bid/ask spread, depth/volume, IV/Greeks availability, and contract identity.
5. Classify regime. Unknown/transition regimes are no-trade.
6. Generate only defined-risk multi-leg candidates and calculate payoff, margin, transaction costs, and stress loss.
7. Evaluate the candidate against portfolio-wide limits and current broker reconciliation.
8. Build a protective leg execution plan: submit long protective legs before opening short legs; when closing, buy back original short legs before selling long protective legs. Persist intent before any broker submission and reconcile every leg/fill.
9. Monitor positions and report state through Telegram.

The deterministic planner calculates leg order and quantities from exchange lot
sizes. A paper-only sequential coordinator now exercises the fill-gating invariant:
opening stops before any short leg unless the preceding protective long is confirmed
fully filled; closing stops before selling a long unless the preceding short cover is
confirmed fully filled. Accepted-but-unfilled, partial, rejected, malformed, and
uncertain submission results halt the sequence and require reconciliation.

The paper coordinator refuses LIVE positions. A separate broker-agnostic
submission boundary now persists a stable client-order key and claims the order by
moving its durable intent to SUBMITTING before invoking an injected broker port.
A timeout, malformed response, or local persistence failure after a broker response
moves the intent to UNKNOWN where possible; the service never automatically resends
that intent. Restart recovery queries the broker by the exact persisted client key:
zero or multiple matches remain blocked, and exactly one match is correlated without
treating acknowledgement as a fill. Fill events must still be independently persisted
before the multi-leg coordinator may advance.

A broker-fill ingestion boundary now resolves each broker order ID against
all durable internal order snapshots (including cancelled orders, to allow late
fills). It verifies instrument token, trading symbol and side before forwarding
the event to the fill coordinator. Broker trade IDs are namespaced by broker order
ID for idempotency. Duplicate events are harmless; unknown or ambiguous order IDs,
contract mismatches, malformed fills, and fills that exceed the remaining order or
position quantity are rejected. Partial close intents may be smaller than the
original planned leg, but cannot exceed confirmed remaining exposure.

This is an adapter-neutral ingestion boundary only. It does not connect to Kite
websocket/order updates, poll trade history, establish event delivery guarantees,
or prove that broker trade IDs and timestamps are normalized correctly. Operational
reconciliation and durable retry of unprocessed inbound events remain required.

Live submission is disabled by default, and no Kite order-placement adapter or
runtime wiring exists. The injected broker port must provide reliable exact-key
lookup; this contract has not yet been validated against Kite's actual order-tag
and order-history semantics. This boundary is not production execution or emergency
close protection.


## Capital accounting

Sizing uses net liquidation equity, not the initial deposit. Deposits and withdrawals are tracked separately from trading P&L. Realised/unrealised P&L, fees, taxes, margin, and open max loss are reconciled. Exposure contracts as equity falls and only scales when a qualifying strategy passes all gates.

## Strategy catalogue direction

Bull call debit spread, bull put credit spread, bear put debit spread, bear call credit spread, iron condor, iron butterfly, calendars/diagonals, and defined-debit long-volatility structures are candidates. Each must have explicit regime, liquidity, IV, Greek, expiry, and scenario gates. No profitability claim is made without independent validation.

Backtesting/research tooling is intentionally excluded from this production repository.

## Option-chain analytics

The read-only option-chain builder joins the current BFO SENSEX contract master to
Kite quotes by instrument token. It excludes stale, missing, crossed, wide-spread,
shallow, zero-volume, off-tick, expired, or unpriceable contracts and records a rejection reason.

Implied volatility and Greeks are Black–Scholes estimates computed from the
bid/ask midpoint, an explicitly supplied SENSEX index price and timestamp, and an
explicit annual continuously compounded risk-free rate. The model assumes zero
dividend yield and treats expiry as 15:30 Asia/Kolkata on the exchange expiry
date. Theta is per calendar day; vega and rho are per one percentage-point
change. These are approximations, not exchange-provided Greeks. Do not use them
as standalone entry signals or as a substitute for portfolio stress testing.

The caller must supply a fresh underlying index price and timestamp. The builder
fails closed for stale underlying data and omits individual contracts whose
quotes or model prices fail validation. It does not fetch an option chain from a
nonexistent/assumed Kite endpoint, place orders, or authorize live trading.

## Regime intelligence

The initial regime classifier consumes completed, timestamped SENSEX index candles
and derives EMA(20/50), Wilder-style ATR(14)/ADX(14), and a recent ATR expansion
ratio. It emits bullish trend, bearish trend, range, volatility expansion, or
transition. Ambiguous/mixed evidence maps to transition and should suppress new
entries. Volatility expansion is a separate regime with a directional bias hint,
not automatic permission to trade in that direction.

The classifier rejects insufficient, unordered, duplicate, stale, future-dated,
or invalid OHLC data. Its thresholds are conservative initial engineering
defaults and require forward validation before strategy use. It does not
constitute a profitability claim, and it must not replace the independent risk
engine or quote-quality gates.


## Payoff validation and candidate generation

The payoff module evaluates same-expiry structures at all strike breakpoints and
rejects net short-call exposure with theoretically unbounded upside loss. It
reports premium cashflow, expiry max loss/profit and breakeven estimates using
exchange lot sizes. Costs are tracked separately and must be included in the
portfolio risk budget.

The initial candidate factory is deliberately narrow: bullish trend can produce
a bull-call debit vertical and a protected bull-put credit vertical; bearish
trend can produce a bear-put debit vertical and a protected bear-call credit
vertical. It selects only current listed strikes from a validated option-chain
snapshot and scales quantities in whole lots. Range, transition, volatility
expansion and unknown regimes currently generate no candidate. Candidate
generation is advisory only; it does not submit orders and must still pass
independent risk, margin, execution-liquidity and broker-reconciliation gates.


## Read-only broker reconciliation

The reconciliation service compares every non-closed internal position leg,
netted by exchange instrument token, with the broker's BFO net position snapshot.
Unrelated non-BFO holdings in the same Kite account are outside this SENSEX engine's scope.
It also checks the current contract-master symbol mapping and identifies active
or pending broker orders. Fractional/invalid quantities, malformed broker
responses, unknown non-zero positions, quantity mismatches, pending orders,
unauthenticated sessions, and adapter exceptions all fail closed.

This is a read-only safety service. It does not cancel orders, modify positions,
or execute closes. Until order intents and fills are durably tracked and a
broker-backed execution state machine is implemented, pending orders keep the
portfolio unreconciled and new entries must remain disabled.


Broker fill ingestion now uses a durable SQLite inbox. Each broker-order/trade identity
and its normalized payload are committed before order/position projections are changed.
Conflicting payload reuse is rejected. Receipts move through RECEIVED, PROCESSING and
PROCESSED states; processing claims use a bounded lease so an abandoned PROCESSING
receipt can be reclaimed after a crash. Failed projections return the receipt to
RECEIVED. The order ledger remains the first fill fact and the position projection
is replayed idempotently, allowing recovery after interruption between the two writes.
This inbox is not a broker event transport: a future Kite adapter must still provide
reliable event acquisition, reconnect/backfill, and reconciliation for missed events.


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
