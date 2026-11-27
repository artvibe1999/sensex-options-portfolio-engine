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

This remains an adapter-neutral durable ingestion boundary. The supervised
Kite monitor now acquires missed fills by periodically polling per-order trade
history and routes them through this inbox. Kite websocket/postback subscription is
not used; correctness relies on current-session history backfill and periodic
broker reconciliation. Durable retry of unprocessed inbound events remains required.

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

This adapter does not place, modify or cancel orders and does not create its own
polling loop. The supervised runtime selects current-session order IDs on each
cycle: Kite's order and trade books are transient and available for the current day
only. Persisted inbox receipts and portfolio events remain the durable local record.
Repeated polling is idempotent through the broker order/trade ID inbox key. The
supervisor owns recurring polling, adapter recreation, retry/backoff, missed-event
backfill, stale-state detection and Telegram health alerts.


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
This service does not schedule itself, enable live mode, or submit/cancel orders.
The Telegram runtime invokes it at startup and the supervised Kite monitor repeats
it during operation with bounded retry/backoff and fail-closed health supervision.


Safety clarification: the startup result field is named `recovery_complete`, not
`ready`. A successful reconciliation is not live readiness and does not authorize
resuming entries. The full `LiveReadinessService`, risk clearance, Telegram
allowlisting and separate live confirmation remain independent mandatory gates.

## Supervised Kite fill monitoring and reconciliation

The Telegram runtime starts a supervised KiteBrokerMonitor after startup recovery
when both Kite credentials are configured. It runs the existing ordered recovery
service on a configurable interval: replay durable pending fill receipts, discover
and ingest all current-IST-session individual Kite trades for correlated LIVE
intents, refresh the BFO SENSEX contract master, and reconcile durable positions
and orders against Kite's read-only broker snapshots. The same pipeline is repeated
after every reconnect, so missed postbacks are recovered from individual trade
history rather than assumed delivered.

The monitor executes synchronous Kite calls in a worker thread to keep Telegram's
async event loop responsive. An independent watchdog detects stale successful-cycle
timestamps and poller-worker exit. On authentication, transport, malformed-data,
fill-ingestion, pending-receipt, contract-master, reconciliation, stale-state or
worker failure, the runtime immediately pauses entries, forces PAPER mode, and
closes the startup-recovery gate. Retries use bounded exponential backoff and
construct a fresh Kite adapter/service after failure. Health transitions are
logged and sent to configured allowlisted Telegram chats; alert-delivery failure
is logged but cannot disable the fail-closed control.

A later successful cycle reopens only the startup-recovery gate. It never clears
the pause or restores LIVE mode; an operator must separately use the existing
Telegram control flow, and all other readiness gates still apply. Shutdown stops
the monitor and leaves the runtime fail-closed. Missing Kite credentials mean
that no monitor is started and startup recovery remains incomplete.

Defaults are a 30-second poll interval, 120-second stale threshold, and 30-second
maximum retry backoff. Settings validation requires a poll interval of at least
10 seconds, a stale threshold at least twice the poll interval, and a bounded
retry maximum. This is read-only monitoring: it does not submit, modify, cancel,
or automatically retry broker orders. Broker order execution remains a separate
unfinished requirement.


## Kite order execution adapter (PE-02)

The optional `KiteOrderExecutionAdapter` is a separate adapter from the read-only
market-data client. It only accepts contracts found uniquely in a freshly fetched
BFO instrument master and restricts execution to SENSEX CE/PE contracts. Orders
are regular, NRML, DAY LIMIT orders; the action must carry a positive price aligned
to the current contract tick and a positive quantity divisible by the current lot
size. MARKET orders and automatic slicing are intentionally unsupported.

A deterministic 20-character alphanumeric Kite tag is derived from the durable
client-order key. Kite documents `tag` as a maximum-20-character order label
returned in the day's order book; it is not a server-enforced idempotency key and
there is no dedicated lookup-by-tag endpoint. Recovery therefore scans the
transient same-day order book, matches the tag exactly, and verifies BFO exchange,
current SENSEX contract, token/symbol, side and quantity against the persisted
intent. Zero matches remain UNKNOWN; multiple matches or mismatched fields require
manual reconciliation. The adapter never interprets an acknowledgement as a fill.

Modify/cancel first retrieve individual order history and verify the same tag,
BFO contract identity, current token/symbol, whole-lot quantity and open/partial
status. The returned broker order ID must match the requested ID. A cancel response
is only a cancel-request acknowledgement; final cancellation and any racing fills
must still be acquired through PE-01 fill ingestion and reconciliation.

A conservative per-process throttle spaces adapter preflight and order API calls by at least 160 ms.
This is a local guard, not a global guarantee across multiple processes or other
clients sharing the same Kite API key. Kite's documented API limits remain the
ultimate external constraint. Durable intent price is persisted and compared
before the submission service crosses the broker boundary.

PE-04 constructs the adapter from the same authenticated Kite client as the
read-only broker and stores it in Telegram application state. Construction does
not submit an order. The durable submission service's explicit live-enable flag
remains false by default, and the multi-leg coordinator is not invoked by Telegram.



## Durable live multi-leg execution (PE-03)

`LiveMultiLegExecutionCoordinator` composes the durable order/position ledgers,
the one-shot broker submission boundary, Kite's LIMIT adapter, and the individual
trade-history fill ingestor. It is a standalone service and is intentionally not
constructed by the Telegram runtime; runtime readiness/wiring remains PE-04.

The coordinator requires two independent opt-ins: its own `live_execution_enabled`
flag and a `DurableBrokerSubmissionService` constructed with live submission
enabled. Both default to false. It accepts LIVE position plans only and persists
the immutable position plus each LIMIT/NRML order intent before submission. Price
is supplied by an injected executable-limit policy and persisted in the intent;
the Kite adapter performs current BFO contract, tick, lot, authentication and order
field checks again at the broker boundary.

Opening follows the deterministic protective sequence: BUY legs first, then SELL
legs. A later leg is not submitted until the prior order's individual trade-history
fills have been ingested and the durable order ledger confirms the exact requested
quantity. An OMS acknowledgement, order-book COMPLETE status, or cancel request
acknowledgement is never substituted for fill facts. Closing follows the inverse
risk-reduction sequence: buy back short legs before selling protective long legs.

A non-full order is cancelled through a durable CANCEL_PENDING claim. The service
then refreshes the exact tagged broker order and waits for broker-confirmed
CANCELLED/REJECTED state before any replacement. Trade-history polling runs after
cancellation and again after status refresh to capture cancel/fill races. Replacements
are sized only from the remaining durable position exposure and are bounded by the
configured close-replacement limit. Derivative residual quantities that are not
whole current lots are not sent; they remain blocked for manual handling.

A failed opening attempts residual cleanup only after every opening order is either
fully filled or broker-terminal/cancel-confirmed. Cleanup buys back any filled short
exposure first and then sells filled protective longs. A close failure, ambiguous
order, missing broker correlation, unconfirmed cancellation, pending fill-inbox
receipt, or incomplete trade-history projection blocks further execution and
leaves the durable position in ERROR with explicit per-leg residual quantities.
There is no automatic resubmission after SUBMITTING/UNKNOWN; a replacement gets a
new immutable intent only after the old order is definitively terminal.

Cancellation terminality is preserved independently from fill state. If an
individual fill arrives after a cancel acknowledgement, the order ledger records
the late fill while retaining `cancel_confirmed`; the position ledger can reopen
a previously flat position to CLOSING if a late opening fill is discovered after
flattening. This ensures reconciliation sees the new residual rather than silently
discarding the race.

PE-03 is an execution orchestration capability, not a live-trading enablement
milestone. No Telegram command or runtime path invokes this coordinator until
PE-04 injects all live readiness, risk, market-data, authentication and persistence
dependencies. Paper remains the process-start default and no real order was placed
during implementation.


## Full runtime live-readiness wiring (PE-04)

The Telegram application now constructs and retains the runtime safety dependency
graph when Kite credentials are configured: one authenticated `KiteReadOnlyAdapter`,
the separate `KiteOrderExecutionAdapter`, shared durable SQLite order/position
lifecycles, the portfolio query service, and `LiveReadinessService`. The runtime
injects that service as the provider for both `/resume` and `/confirm_live`.
Missing credentials, a missing optional Kite SDK, or any construction failure
installs a deny-all readiness provider and leaves the process in PAPER mode with
entries paused. No broker credentials are logged.

Every live-readiness evaluation independently checks:
- Kite profile authentication.
- A freshly downloaded BFO SENSEX CE/PE contract master with unique tokens/symbols,
  positive lot/tick metadata and a current IST expiry.
- A same-expiry, same-strike CE/PE quote pair. Both quotes must be no older than
  two seconds, have valid bid/ask, no crossed market, spread at most 3%, positive
  top-of-book depth and volume, and timestamp skew at most two seconds.
- SQLite `PRAGMA quick_check` plus successful access to the command audit and
  portfolio event tables.
- Read-only broker positions/orders reconciliation against durable position and
  order projections, including all unresolved order states.
- Current Kite equity-segment funds and margin utilisation plus the adaptive
  portfolio risk gate.
- Explicit operator confirmation that the Kite order-placement static IP has
  been configured. `KITE_STATIC_IP_CONFIRMED` defaults false; it must only be
  set true after the account's registered static IP is configured in the Kite
  developer console. Kite rejects order requests from unregistered IPs.

The risk gate reads the live Kite funds/margins endpoint and checks positive
available funds, account margin utilisation, net liquidation equity, available
margin, daily loss, weekly/monthly drawdown, open-position and daily-entry limits,
peak-equity drawdown scaling, and aggregate open risk. It requires an authoritative
`PortfolioSnapshot` provider. The current runtime intentionally does not fabricate
that snapshot: until PE-06 completes authoritative equity, cash-flow, P&L and
drawdown accounting, the default risk provider returns false and live activation
is impossible. This is a deliberate dependency boundary, not a readiness bypass.

A successful startup recovery is still not enough. Both `/resume` and the separate
`/confirm_live` command re-evaluate the complete dependency graph. A failure
denies the command. Monitor failures continue to force PAPER mode and pause entries.
The order adapter is constructed for dependency readiness only; neither the
submission service nor the PE-03 coordinator is activated by this milestone.
