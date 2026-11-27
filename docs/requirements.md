# SENSEX Options Portfolio Engine — Product & Trading Requirements

Status: normative product specification and implementation target  
Scope: SENSEX index options only; defined-risk multi-leg portfolios; intraday and explicitly approved overnight holding  
Broker: Zerodha Kite Connect (BFO instruments)  
Control plane: Telegram  
Default operating mode: paper  
Capital reference: ₹50,000 is an illustrative starting deposit, not a fixed ceiling or permanent sizing input

**Important:** This document specifies candidate strategies and engineering rules. It does not assert that any strategy is profitable. Strategy approval requires independent forward validation and realistic transaction-cost, slippage, and fill modelling. The repository intentionally excludes a backtesting/research engine unless the owner explicitly reverses that requirement.

---

## 1. Product objectives and non-negotiable constraints

Build an independent, reliable Python portfolio engine that selects, sizes, executes, monitors, and safely exits SENSEX index-option strategies using current BSE contract metadata and broker data.

### 1.1 Scope boundaries

- Trade SENSEX index options only. Never silently trade BANKNIFTY, NIFTY, stock options, futures, or unrelated BFO instruments.
- Support defined-risk multi-leg structures: debit and credit spreads, iron condors, iron butterflies, calendars/diagonals, and defined-debit long-volatility structures.
- Support intraday trades and overnight positions only when strategy, expiry, margin, gap-risk, and event policies explicitly allow them.
- Use whole exchange lots only. Never hardcode lot size, strike interval, expiry weekday, tick size, or contract symbol.
- Use current BSE contract-master data and broker instrument identifiers. Validate contract identity immediately before order submission.
- No unbounded naked short option exposure. Every short option must be covered by a verified protective long leg or another mathematically defined risk cap.
- Fail closed: uncertain regime, stale quotes, invalid chain, missing margin, reconciliation mismatch, unavailable broker state, breached risk limit, or uncertain order state means no new risk.
- Do not promise returns, win rate, or profitability.

### 1.2 Safety defaults

1. Every process start forces PAPER mode, regardless of persisted state or environment variables.
2. Live mode can be requested only by an allowlisted Telegram user in an allowlisted chat.
3. /live is a request, not activation. /confirm_live is a separate explicit confirmation bound to a short-lived, single-use pending request.
4. Live activation requires all readiness gates: fresh market data, valid contracts, broker authentication, clean reconciliation, healthy persistence, risk approval, execution adapter readiness, and no unresolved order/position state.
5. A process restart, lost heartbeat, stale data, failed reconciliation, audit failure, or loss of required services forces PAPER or HALTED state.
6. No VM deployment, credential rotation, account changes, or real order placement is authorized by this document.
7. Emergency controls must never report a position closed unless broker fills and reconciliation confirm it.

---

## 2. Architecture and component boundaries

Use ports-and-adapters (hexagonal) architecture. Domain logic must not depend on Kite SDK, Telegram SDK, SQLite, HTTP, or framework-specific objects.

### 2.1 Logical components

| Component | Responsibility | Required output |
|---|---|---|
| Configuration and secrets | Typed settings, validation, environment/secret-manager loading | Immutable validated settings; secrets never logged |
| Exchange calendar and contract master | BSE sessions, holidays, SENSEX contracts, expiry, lot/tick/strike metadata | Versioned contract snapshot |
| Market-data adapter | Index candles, option quotes, depth, volume, broker timestamps | Normalized timestamped market events |
| Data-quality engine | Freshness, ordering, crossed quotes, spreads, depth, volume, outliers | Quality score and explicit rejection reasons |
| Option-chain builder | Join current SENSEX contracts to live quotes | Validated CE/PE chain by expiry and strike |
| Analytics engine | EMA, VWAP, ATR, ADX/DMI, RSI, Bollinger, volume profile, IV, Greeks, expected move | Timestamped feature snapshot |
| Regime engine | Trend, range, expansion, compression, transition, event-risk classification | Regime plus confidence and directional bias |
| Strategy catalogue | Strategy-specific setup detection and eligibility | Candidate structures with rationale |
| Strategy arbiter | Rank only eligible candidates; avoid correlated duplicates | One preferred candidate or no-trade |
| Payoff and scenario engine | Expiry payoff, breakevens, Greeks, stress scenarios, costs | Defined max loss/profit and scenario report |
| Portfolio and capital engine | Equity accounting, risk budgets, lot sizing, margin, concentration | Approved quantity or rejection |
| Risk governor | Hard portfolio limits and circuit breakers | Allow/reject plus reason |
| Execution service | Durable intents, multi-leg sequencing, order updates, partial-fill handling | Authoritative execution state |
| Reconciliation service | Compare internal state to broker positions/orders | Clean/mismatch/unknown status |
| Position manager | Monitoring, adjustments, exits, overnight decisions | Exit/adjustment intent |
| Persistence and audit | Append-only events, snapshots, command audit, idempotency | Recoverable state |
| Telegram control plane | Status, approvals, controls, alerts, reporting | Authenticated command result |
| Observability | Structured logs, metrics, health, alerts | Operational health state |

### 2.2 Event-driven data flow

1. Load BSE trading calendar and current contract master.
2. Validate SENSEX option instrument identity, expiry, strike, option type, lot size, tick size, and token.
3. Fetch underlying index candles and option quotes/depth from Kite-supported endpoints. Do not assume Kite exposes a native complete option-chain endpoint; build the chain from current instruments and supported quote calls, respecting rate limits.
4. Normalize timestamps to Asia/Kolkata and preserve exchange/broker source timestamps.
5. Run data-quality checks before any indicator or strategy computation.
6. Compute completed-candle indicators and option analytics.
7. Classify regime and event risk.
8. Generate strategy candidates; each candidate includes entry evidence, invalidation, payoff, liquidity, Greeks, and expiry policy.
9. Arbitrate eligible candidates; suppress duplicate or highly correlated portfolio exposures.
10. Calculate portfolio stress loss, margin, risk budget, and whole-lot quantity.
11. Persist an immutable order intent before any order submission.
12. Execute the structure with protective-leg sequencing and idempotent broker order references.
13. Track every acknowledgement, fill, rejection, cancellation, timeout, and partial fill.
14. Reconcile after every material broker event and periodically thereafter.
15. Monitor positions, exits, overnight eligibility, and Telegram alerts.

### 2.3 Domain model

Core entities:
- Instrument: exchange, segment, token, symbol, underlying, expiry, strike, CE/PE, lot size, tick size, freeze quantity.
- Quote: bid/ask prices and quantities, LTP, volume, OI if available, exchange timestamp, receive timestamp.
- Candle: interval, OHLCV, start/end timestamp, completion flag.
- FeatureSnapshot: indicator values, source timestamps, lookback/config version.
- RegimeSnapshot: regime, bias, confidence, volatility state, transition flag.
- StrategyDefinition: stable ID/version, eligible regimes, legs, setup rules, entry/exit rules, risk parameters, overnight permission.
- Candidate: legs, quantities, limit prices, payoff, Greeks, costs, rationale, expiry, data lineage.
- PortfolioSnapshot: net equity, cash, deposits, withdrawals, realised/unrealised P&L, charges, margin, positions.
- OrderIntent, BrokerOrder, Fill, Position, PositionLeg.
- RiskDecision, ReconciliationResult, CommandAudit, SystemMode.

All events require unique IDs, timestamps, source, correlation ID, and schema version.

---

## 3. Market data, contract and quote-quality rules

### 3.1 Contract master

- Refresh from the current BSE/Broker BFO instrument master before the session and revalidate during the day.
- Filter by exact SENSEX underlying and CE/PE option type.
- Store master download time, source, checksum/version, expiry, token, symbol, strike, lot size, tick size, and applicable freeze quantity.
- Reject expired contracts, duplicate token/symbol mappings, missing fields, impossible strikes, stale master, or contract mismatch.
- Do not infer expiry schedules from memory or fixed weekdays; read actual listed contracts.
- Never reuse an old token after a contract-master refresh without resolving the current mapping.

### 3.2 Quote quality

A quote is eligible only if:
- bid and ask are positive, finite, and not crossed;
- bid <= ask and prices align to contract tick size;
- quote age is within configured threshold (initial target <= 2 seconds for multi-leg entry);
- bid/ask spread is within both absolute and relative strategy limits;
- displayed depth is sufficient for intended lot quantity, where depth is available;
- volume and OI checks pass strategy liquidity policy;
- underlying index price is fresh and timestamp-aligned;
- every leg in a candidate has synchronized quotes (maximum inter-leg timestamp skew initially 2 seconds).

Use conservative executable marks: buy long legs at ask and sell short legs at bid. Do not use midpoint for expected execution or risk approval. Midpoint may be used only for indicative analytics and must be labelled non-executable.

Reject zero-volume, shallow, stale, off-tick, crossed, missing, unpriceable, or excessively wide contracts. Persist rejection reasons.

### 3.3 Liquidity score

Score each contract/structure using relative bid/ask spread, bid/ask quantity versus desired order size, traded volume and OI where reliable, quote age/update frequency, inter-leg quote skew, estimated market impact/slippage, and ability to close without leaving an unprotected short leg.

Liquidity is a hard gate, not merely a ranking preference. Never substitute a liquid contract with a materially different risk profile without recomputing payoff and approval.

---

## 4. Indicator and analytics specification

All indicators use completed candles unless explicitly described as live microstructure features. Persist timeframe, lookback, smoothing method, and source timestamp. No indicator alone authorizes a trade.

### 4.1 Underlying price indicators

- **EMA(9), EMA(20), EMA(50), EMA(200):** exponential moving averages for short momentum, pullback zones, primary trend, and broad trend. Standard recursive EMA with alpha = 2/(n+1).
- **VWAP:** session cumulative typical-price-volume / cumulative volume. Reset each exchange session. If reliable volume is unavailable, mark VWAP unavailable; do not fabricate.
- **ATR(14):** Wilder true range average; TR = max(high-low, abs(high-prev_close), abs(low-prev_close)). Used for volatility normalization, stops, and expansion.
- **ADX(14), +DI, -DI:** Wilder directional movement. ADX measures trend strength, not direction. Direction requires DI relationship and price structure.
- **RSI(14):** Wilder-smoothed gains/losses. Use 50-line trend context, 40/60 pullback bands and 30/70 extremes contextually, not as automatic reversal triggers.
- **Bollinger Bands(20, 2):** 20-period SMA +/- 2 standard deviations. Use bandwidth percentile and close location to identify compression/expansion.
- **Donchian(20):** rolling high/low breakout reference, excluding current incomplete candle.
- **Opening range:** first 15-minute range by default; optional 5-minute opening micro-range only when configured and validated. Do not mix definitions within a strategy version.
- **Previous session levels:** previous day high/low/close, opening gap, and prior value area when volume data supports it.
- **Relative volume (RVOL):** current interval volume divided by historical median/mean volume for same time-of-day bucket. Require sufficient history and robust holiday/half-session handling.
- **Volume profile:** session/prior-session POC, VAH, VAL and high/low-volume nodes, only when tick/volume data quality is sufficient. Do not label broker candle-volume estimates as exchange-wide volume profile.
- **Market structure:** confirmed swing highs/lows, higher-high/higher-low and lower-high/lower-low sequences, break of structure (BOS), change of character (CHOCH), failed breakout and retest.
- **Gap metrics:** opening gap in points and ATR units; classify gap-and-go versus gap-fill only after acceptance/rejection evidence.

### 4.2 Options analytics

- **Implied volatility:** solve Black–Scholes IV numerically from valid option price and synchronized underlying price, with explicit risk-free rate, expiry timestamp, and dividend assumption. Reject non-convergent or implausible solutions.
- **Greeks:** delta, gamma, theta, vega, rho per contract and portfolio aggregate. Document units. Theta is per calendar day; vega/rho convention must be explicit (e.g. per 1 volatility/rate percentage point).
- **IV rank/percentile:** only when sufficiently long, consistently sampled SENSEX option IV history exists. Until then mark unavailable; do not substitute India VIX as SENSEX IV.
- **India VIX:** based on NIFTY options, not a direct SENSEX implied-volatility measure. It may be broad market-volatility context only, never SENSEX IV.
- **Expected move:** approximate one-standard-deviation move = spot × ATM IV × sqrt(time_to_expiry_years). Use correct time convention and label approximation. Compare with ATR, expected range, and recent realized volatility.
- **Skew:** compare put/call IV at matched absolute delta or moneyness for same expiry. Require enough valid strikes and quote quality.
- **Term structure:** compare ATM IV across expiries with consistent sampling.
- **Option OI/volume changes:** secondary context only; OI is not proof of directional positioning or institutional intent.
- **Portfolio Greeks:** aggregate all legs using signed quantity × lot size × per-unit Greek. Include scenario repricing for large underlying and IV moves; Greeks are local approximations and cannot replace stress tests.

### 4.3 Multi-timeframe confirmation

- 1-minute: execution timing and microstructure only.
- 3-minute: setup confirmation and short-term momentum.
- 5-minute: primary intraday structure and VWAP interaction.
- 15-minute: regime and opening range context.
- 60-minute/daily: higher-timeframe trend, key levels, and overnight context.

A lower timeframe must not override a conflicting higher timeframe without an explicitly defined countertrend strategy. Incomplete higher-timeframe candles cannot be treated as confirmed.

---

## 5. Regime classification and strategy map

The regime engine returns one of BULL_TREND, BEAR_TREND, RANGE, VOL_EXPANSION, VOL_COMPRESSION, TRANSITION, or UNKNOWN. Include confidence, directional bias, volatility state, and reasons.

Initial configurable heuristics (engineering starting points, not empirically optimized):
- Trend strength: ADX(14) >= 20–25 with +DI/-DI directional agreement, EMA20/50 slope/alignment, and market structure confirmation.
- Bull trend: price accepts above VWAP/EMA20, EMA20 > EMA50, positive +DI dominance, higher highs/lows, and no nearby resistance invalidating reward/risk.
- Bear trend: inverse of bull conditions.
- Range: ADX below approximately 18–20, flat EMA20/50, repeated VWAP crossings, and price contained within established support/resistance/value area.
- Compression: Bollinger bandwidth or ATR percentile near a low rolling percentile, contracting ranges, and declining realized volatility.
- Expansion: ATR/realized range increases materially above recent baseline with a range break or event impulse.
- Transition/unknown: conflicting indicators, stale data, insufficient history, sudden regime change, or low confidence.

Thresholds must be config-versioned and calibrated only through approved evaluation. Never force a trade to satisfy a daily trade count. No qualifying candidate means no trade.

### 5.1 Strategy-to-regime map

| Strategy | Preferred regime | Avoid / hard block |
|---|---|---|
| Bull Call Debit Spread | Bull trend; bullish breakout after retest | Bear trend; weak upside reward; poor liquidity |
| Bull Put Credit Spread | Bull trend or stable bullish-to-neutral support | Strong bearish/expansion risk; support failure |
| Bear Put Debit Spread | Bear trend; bearish breakdown after retest | Bull trend; weak downside reward |
| Bear Call Credit Spread | Bear trend or stable bearish-to-neutral resistance | Strong bullish/expansion risk; resistance breakout |
| Iron Condor | Confirmed range / compression with bounded event risk | Trend/expansion, impending event, weak wings |
| Iron Butterfly | Tight, stable range around accepted value/ATM with sufficient premium | Breakout, expansion, unstable gamma, overnight gap risk |
| Calendar / Diagonal | Defined term-structure or event/decay thesis with controlled near-leg risk | Poor term structure, uncertain expiry/settlement handling |
| Long Iron Butterfly / defined-debit long-volatility | Compression before catalyst or expected realized move > implied move | Low movement expectation, excessive debit, poor liquidity |

---

## 6. Detailed strategy catalogue

All parameters below are initial policy templates. Each must be independently versioned, tested in paper mode, and approved before live eligibility. Every structure is evaluated at current contract lot size and expiry. Credit strategies must have protective wings.

### 6.1 Bull Call Debit Spread

**Thesis:** defined-risk bullish directional exposure with limited premium outlay and capped reward.  
**Structure:** buy a lower-strike call; sell a higher-strike call of same expiry and quantity.

**Setup and indicators**
- Regime = BULL_TREND, or confirmed bullish range breakout with successful retest.
- EMA20 > EMA50; EMA20 slope positive; price above VWAP or a confirmed VWAP reclaim.
- ADX/DMI supportive (e.g. ADX >= 20 and +DI > -DI) or strong breakout volume/structure confirmation.
- 3m/5m higher-low or bullish BOS; avoid entry directly under major resistance unless reward remains adequate.
- RVOL and quote liquidity pass; IV not so elevated that debit is unjustified.

**Entry filters**
1. Wait for close above opening-range high, Donchian/structure resistance, or VWAP reclaim.
2. Prefer retest that holds the broken level; reject a candle closing back inside prior range.
3. Confirm momentum with RSI > 50 or rising from 40–50, and positive DI/EMA slope.
4. Select long call around 0.55–0.70 absolute delta; short call around 0.25–0.40 delta, subject to listed strikes and spread width.
5. Require positive net reward/risk at expiry and minimum expected reward/risk after costs (initial target >= 1.3, configurable).
6. Buy at ask / sell at bid for conservative debit; reject if spread or slippage erases edge.

**Invalidation and exit**
- Underlying closes below breakout/retest support or VWAP and structure turns lower.
- Long-leg delta/momentum deteriorates and original thesis is invalidated.
- Exit before expiry if remaining reward is small relative to remaining risk, target debit value is achieved, or theta/gamma risk becomes unfavourable.
- Initial management template: partial profit only where whole-lot constraints permit; otherwise close full spread at strategy-defined profit target (e.g. 50–70% of max profit), or exit on technical invalidation.
- Hard exit before expiry cutoff; never hold solely to realize maximum payoff if liquidity deteriorates.

**Risk:** max loss = net debit × lot size × lots + estimated charges/slippage. Max profit = (strike width - net debit) × lot size × lots. Breakeven = long call strike + net debit.

### 6.2 Bull Put Credit Spread (defined-risk)

**Thesis:** bullish-to-neutral support thesis; collect credit while defining downside risk.  
**Structure:** sell higher-strike put; buy lower-strike put (same expiry/quantity).

**Setup and indicators**
- BULL_TREND or well-established support in a stable range with bullish bias.
- Price above VWAP/EMA50 or successful support reclaim; higher lows or demand-zone hold.
- ADX/DMI must not show accelerating bearish trend.
- No imminent high-impact event or expiry gamma risk overwhelming credit.

**Entry filters**
1. Price tests support, prior breakout level, VWAP, or value-area low and rejects lower prices.
2. Require bullish close/reclaim and no confirmed bearish BOS.
3. Short put typically 0.15–0.30 delta; long protective put farther OTM (e.g. 0.05–0.15 delta), chosen by defined max loss, not fixed strike distance.
4. Credit must compensate for width, tail risk, IV, costs and expected move; reject tiny credit relative to max loss.
5. Underlying support should be outside expected adverse move or have clear technical invalidation.
6. Quote, margin, and portfolio risk gates must pass.

**Exit**
- Close on support failure, bearish BOS, sustained VWAP loss, or regime flip.
- Profit-taking template: close after capturing 50–70% of initial credit; avoid holding for final pennies while gamma risk rises.
- Risk exit if spread mark reaches configured multiple of initial credit or stress loss exceeds approved budget.
- Close/roll only through a new candidate and fresh risk approval; never roll automatically to conceal a loss.
- Close before expiry risk window unless separately approved expiry policy applies.

**Risk:** max loss = (put strike width - credit) × lot size × lots + costs. Max profit = credit × lot size × lots. Breakeven = short put strike - credit.

### 6.3 Bear Put Debit Spread

**Thesis:** defined-risk bearish directional exposure.  
**Structure:** buy higher-strike put; sell lower-strike put (same expiry/quantity).

**Setup**
- BEAR_TREND or confirmed bearish breakdown and retest.
- EMA20 < EMA50 with negative slope; price below VWAP; lower highs/lows.
- -DI > +DI and ADX supportive, or strong breakdown with RVOL expansion.
- Avoid entering directly into major support unless payoff supports the trade.

**Entry**
1. Close below opening-range low, prior swing low, VWAP support, or Donchian low.
2. Prefer failed retest from below; reject recovery into prior range.
3. RSI < 50 or declining; confirm bearish BOS/CHOCH.
4. Buy put around 0.55–0.70 absolute delta; sell lower put around 0.25–0.40 delta.
5. Debit, reward/risk, liquidity, expected move and costs pass.

**Exit**
- Underlying reclaims broken support/VWAP and confirms bullish reversal.
- Close on target, adverse momentum divergence, or regime flip.
- Profit target template 50–70% of max profit or earlier when remaining upside is small relative to gamma/theta risk.
- Hard exit before expiry cutoff.

**Risk:** max loss = debit × lot size × lots + costs. Max profit = (strike width - debit) × lot size × lots. Breakeven = long put strike - debit.

### 6.4 Bear Call Credit Spread (defined-risk)

**Thesis:** bearish-to-neutral resistance thesis with capped upside risk.  
**Structure:** sell lower-strike call; buy higher-strike call (same expiry/quantity).

**Setup**
- BEAR_TREND or stable range with well-defined resistance.
- Price below VWAP/EMA50 or rejected at resistance; lower highs; no bullish BOS.
- +DI should not be accelerating above -DI; block if bullish expansion begins.

**Entry**
1. Rally into resistance, prior breakdown level, VWAP or value-area high and rejection.
2. Confirm bearish candle/structure; avoid selling into a strong bullish impulse.
3. Short call around 0.15–0.30 delta; long call farther OTM around 0.05–0.15 delta.
4. Credit must meet minimum return-on-risk after charges and stress scenarios.
5. All legs must be liquid and protective long must be confirmed available.

**Exit**
- Resistance breaks and price accepts above it; bullish BOS; regime changes to bull/expansion.
- Profit-taking after 50–70% credit capture.
- Risk exit at configured spread-price multiple or max-loss threshold; never wait for theoretical max loss if thesis invalidates.
- Close before expiry gamma/settlement risk unless approved policy explicitly says otherwise.

**Risk:** max loss = (call width - credit) × lot size × lots + costs. Max profit = credit × lot size × lots. Breakeven = short call strike + credit.

### 6.5 Iron Condor (defined-risk range strategy)

**Thesis:** benefit from range-bound/contained movement and time decay, with capped loss on both tails.  
**Structure:** buy lower put wing; sell higher put; sell lower call; buy higher call. Same expiry, equal quantities; wings cover shorts.

**Setup**
- Regime = RANGE or VOL_COMPRESSION.
- ADX weak/flat, EMA20/50 relatively flat, repeated VWAP reversion, no confirmed directional BOS.
- Support/resistance and value area established; expected move contained within short strikes with buffer.
- Avoid event sessions, trend days, opening drive, and volatility expansion.

**Entry filters**
1. Wait until opening volatility settles; no automatic opening-minute condor.
2. Sell put/call around 0.10–0.20 delta; protective wings farther OTM, selected by max-loss and liquidity constraints.
3. Short strikes beyond relevant support/resistance and preferably beyond expected move boundary with safety buffer.
4. Require acceptable total credit versus wing width/max loss, and adequate return after costs.
5. Check portfolio delta/gamma/vega and stress both directional tails and IV expansion.
6. Require no material scheduled event or overnight gap exposure outside approved policy.

**Exit and adjustment**
- Close after 40–60% of credit captured; do not hold for final decay if risk/reward deteriorates.
- Exit if spot approaches short strike, breaks range boundary and confirms acceptance, or portfolio delta/gamma exceeds limits.
- No unbounded rolling. Any adjustment is a new structure with fresh max-loss and margin calculation.
- Prefer close rather than legging out into an unprotected short.
- Intraday default: close before end-of-day cutoff. Overnight holding requires separate explicit approval, lower size, and gap stress.

**Risk:** max loss on either side = (wing width - total credit) × lot size × lots + costs; max profit = total credit × lot size × lots. Calculate both breakevens from short strikes +/- total credit.

### 6.6 Iron Butterfly (defined-risk ATM range strategy)

**Thesis:** tighter range/near-ATM pinning thesis; higher gamma and adjustment risk than a wider condor.  
**Structure:** sell ATM call and ATM put; buy OTM call and OTM put wings, same expiry and equal quantities.

**Setup**
- Confirmed tight RANGE/compression regime with spot repeatedly accepted near fair-value/POC region.
- Low directional conviction, stable VWAP, no expansion impulse.
- Premium sufficient relative to wing width, costs and stress loss.
- Not eligible merely because ATM IV is high; high IV may signal event risk.

**Entry**
1. Do not enter during opening volatility or immediately before scheduled event.
2. ATM short strike near accepted spot/value reference; wings selected by defined max loss and liquidity.
3. Check net credit, breakevens, gamma concentration, expected move and gap scenarios.
4. Require low directional drift and adequate distance to both breakevens.
5. Size smaller than ordinary vertical spreads because ATM gamma is concentrated.

**Exit**
- Take profit earlier (template 25–50% of credit) due to gamma risk.
- Exit if spot leaves central range, crosses a short strike with acceptance, or underlying/IV stress breaches risk.
- Avoid expiry-day holding without dedicated intraday gamma controls.
- No automatic rolling; any adjustment is a fresh risk-approved candidate.

**Risk:** max loss = (wing width - credit) × lot size × lots on either side; max profit = credit × lot size × lots. Breakevens = body strike +/- credit.

### 6.7 Calendar / Diagonal Spread

**Thesis:** exploit a defined volatility term-structure or time-decay relationship, while controlling near-expiry gamma and expiry mismatch.

**Structure**
- Calendar: sell near-expiry option and buy same-strike, same-type farther-expiry option.
- Diagonal: sell near-expiry option and buy farther-expiry option at a different strike.
- Calls or puts selected by directional bias; quantities and expiries explicitly matched and validated.

**Setup**
- Market stable/range-bound around forecast target for neutral calendars, or mild directional bias for diagonals.
- Far-expiry IV/term structure and near-expiry IV relationship support thesis.
- No unmodelled event between expiries; event risk explicit.
- Both expiries have valid contract metadata, liquid quotes, and documented settlement/expiry process.

**Entry**
1. Identify target price zone using VWAP, value area, expected move and structure.
2. Compare near/far IV, skew and expected theta decay; require quantified rationale rather than generic “sell theta”.
3. Choose short near leg with controlled delta/gamma and long far leg with sufficient vega/convexity.
4. Evaluate payoff across spot/IV/time scenarios, not just expiry payoff; calendars cannot be assessed with a single-expiry payoff formula.
5. Require risk-defined debit or verified bounded-risk structure under broker/exchange margin model.
6. Verify expiry dates and holiday-adjusted sessions directly from contract master.

**Exit**
- Close when spot exits target zone, term structure moves against thesis, IV shock breaches scenario loss, or short leg approaches expiry risk window.
- Close/roll near leg only with a new risk-approved intent and protection preserved.
- Do not assume farther-expiry long automatically protects every near-expiry settlement state.
- Close before near expiry unless dedicated expiry/settlement policy has been implemented and tested.

**Risk:** max loss for a fully paid, correctly structured debit calendar is generally limited to debit paid under the defined model; diagonals and multi-expiry structures require full broker/exchange payoff and stress validation. Never infer max loss from a simplistic same-expiry vertical formula.

### 6.8 Long Iron Butterfly / defined-debit long-volatility structure

**Thesis:** benefit from a sufficiently large move away from a central strike/zone when expected realized movement is greater than implied movement, with loss capped at debit.

**Structure (long iron butterfly):** buy an ATM call and ATM put; sell an OTM call and an OTM put farther out, same expiry and equal quantities. This is a debit structure with a long ATM straddle partly financed by short wings. A plain long straddle/strangle may be considered as a separate configuration only if explicitly enabled and its debit is affordable.

**Setup**
- VOL_COMPRESSION transitioning toward VOL_EXPANSION, or scheduled catalyst with defensible movement thesis.
- Narrowing ATR/Bollinger bandwidth followed by early range expansion, or expected realized move exceeds implied move after costs.
- Avoid buying expensive volatility after the move has already occurred.
- Ensure short wings do not cap payoff before expected move zone.

**Entry**
1. Identify compression, catalyst, breakout trigger or expected-move dislocation.
2. Require breakout/expansion confirmation or a clearly specified pre-event risk budget; no oversized event bet.
3. Buy near-ATM call/put; sell farther OTM call/put wings beyond expected target zone.
4. Compute net debit, payoff curve, breakevens and scenarios under spot, IV and time changes.
5. Reject if debit exceeds approved per-trade risk budget or IV crush makes thesis unattractive.

**Exit**
- Take profit when move is realized and IV/option marks meet target; do not wait for expiry if momentum fades.
- Exit if compression persists beyond time stop, breakout fails, or IV collapses.
- Close before expiry cutoff and before short wings become unmanaged risk.
- Max loss must remain net debit under defined same-expiry structure, plus costs.

**Risk:** max loss = net debit × lot size × lots + costs. For long iron butterfly, maximum profit is bounded by wing geometry and must be computed by payoff engine at all strike breakpoints; do not use a memorized formula when wing widths differ.

---

## 7. Strike selection and structure construction

1. Start with actual listed strikes from current contract master; never synthesize unavailable strikes.
2. Use delta bands as initial selection guides, not hard-coded strike offsets:
   - directional long legs: approximately 0.55–0.70 absolute delta;
   - directional short profit-capping legs: approximately 0.25–0.40 absolute delta;
   - credit spread short legs: approximately 0.15–0.30 absolute delta;
   - credit spread protective wings: approximately 0.05–0.15 absolute delta;
   - condor short legs: approximately 0.10–0.20 absolute delta.
3. Select wings using max loss, expected move, technical support/resistance, liquidity, and margin; delta alone is insufficient.
4. Verify same expiry for verticals and iron structures. Calendar/diagonal expiry mismatch must be intentional and fully modelled.
5. Recompute net premium using executable sides: buy at ask, sell at bid.
6. Include brokerage, exchange transaction charges, STT, GST, stamp duty, SEBI fees and applicable taxes using current broker/exchange schedules. Do not hardcode rates indefinitely; make them versioned/configurable and reconcile actual charges.
7. Calculate max loss/profit, breakevens, payoff at spot scenarios, portfolio Greeks, margin and stress loss before approving quantity.
8. Reject if a leg is unavailable, quote timestamps are unsynchronized, or protective coverage cannot be proven.

---

## 8. Entry filters, scoring, arbitration and no-trade policy

Every strategy emits a structured diagnostic record:
- strategy ID/version;
- regime required/observed;
- each indicator value and threshold;
- setup state and candle timestamps;
- entry trigger state;
- liquidity and quote-quality result;
- IV/Greeks/expected-move result;
- selected legs and delta;
- payoff, margin, costs, max loss and stress loss;
- portfolio risk result;
- exact rejection reason(s).

### 8.1 Hard gates

Reject a candidate if any fail:
- SENSEX contract identity / expiry / lot / tick validation;
- fresh, synchronized underlying and option quotes;
- quote spread, depth and liquidity;
- strategy regime and setup;
- no unresolved transition or event-risk block;
- defined max loss and protective leg coverage;
- minimum reward/risk or credit-to-risk requirement;
- current margin and funds availability;
- portfolio and drawdown risk;
- broker authentication and clean reconciliation;
- no pending/unknown order state;
- trading session and entry cutoff;
- execution service and persistence health.

### 8.2 Candidate scoring

Only candidates that pass every hard gate may be scored. Example weighted score (configurable, not a probability of profit):
- setup/structure quality: 25%;
- regime/timeframe alignment: 20%;
- reward-to-risk after costs: 15%;
- liquidity/execution quality: 15%;
- IV/expected-move suitability: 10%;
- portfolio diversification / Greek fit: 10%;
- time-of-day suitability: 5%.

Penalize overlapping exposure, correlated strategies, poor fill probability, high gamma near expiry, and event risk. Score cannot override a failed hard gate.

### 8.3 Arbitration

- Rank eligible candidates and select best risk-adjusted portfolio addition.
- Prefer no trade over forcing a candidate.
- Do not open several candidates expressing same directional bet unless aggregate risk and correlation limits explicitly allow it.
- Avoid duplicate positions in same expiry/strike cluster.
- Re-evaluate ranking after fills, regime changes, capital changes, or material market-data updates.

---

## 9. Risk, capital, sizing and portfolio controls

### 9.1 Equity accounting

Use NetEquity as dynamic capital base:
NetEquity = cash + marked portfolio value - liabilities/charges, with marks and broker reconciliation defined consistently.

Track deposits and withdrawals separately from P&L. Reconcile opening equity, deposits/withdrawals, realised P&L, unrealised P&L, brokerage/taxes/charges, current cash and margin, open position max loss and stress loss.

₹50,000 is only an example starting capital. Every strategy eligibility, risk budget, lot size, and margin decision must adapt to current verified equity. Capital growth must not automatically increase risk linearly; equity drawdown must contract risk promptly.

### 9.2 Initial conservative risk policy (configurable, not guaranteed optimal)

- Target risk per trade: 0.75% of current NetEquity.
- Hard maximum risk per trade: 1.00% of NetEquity.
- Maximum risk attributed to one strategy family: 1.00% of NetEquity.
- Aggregate open defined risk: 2.00% of NetEquity.
- Daily realised + unrealised loss stop: 2.00% of start-of-day equity.
- Weekly drawdown stop: 4.00% from weekly peak/equity reference.
- Monthly drawdown stop: 7.00% from monthly peak/equity reference.
- Maximum concurrent positions: 2 initially.
- Maximum new entries per day: 3 initially.
- Maximum margin utilisation: 70% of available eligible funds.
- Any tighter broker, exchange, or account limit takes precedence.

These are starting guardrails. Risk may be strategy-specific and adjusted only through explicit versioned configuration and evidence; no strategy can bypass hard portfolio limits.

### 9.3 Drawdown scaling

- Drawdown < 3%: normal approved risk budget.
- 3–5%: reduce new risk budget by 25%.
- 5–7%: reduce new risk budget by 50%.
- 7–10%: suspend discretionary new entries pending review.
- >=10%: halt new entries and require explicit review/reset procedure.

Apply scaling to current equity and existing open risk. Do not reset drawdown reference to disguise losses.

### 9.4 Lot sizing

For each candidate:
1. Compute worst-case structure loss including fees, conservative slippage and stress buffer.
2. Compute maximum lots allowed by per-trade risk.
3. Compute maximum lots allowed by strategy-family and aggregate portfolio risk.
4. Compute margin/funds affordability using current broker margin estimate and configurable safety buffer.
5. Compute maximum lots allowed by liquidity and freeze quantity.
6. Take minimum whole-lot quantity across all limits.
7. Reject if quantity < 1 lot or broker margin/contract data is unavailable.

lots = floor(min(risk_budget / per_lot_stress_loss, margin_budget / per_lot_margin, liquidity_limit, freeze_limit)).

Do not round up. Do not silently reduce protective wings. Recalculate portfolio risk and margin after proposed entry and after every fill.

### 9.5 Portfolio Greeks and stress

Track net delta, gamma, theta, vega and rho by expiry and portfolio. Stress:
- SENSEX spot +/- 0.5%, 1%, 1.5%, 2% and strategy-specific technical gap levels;
- IV +/- 3, 5, 10 volatility points;
- combined spot/IV shocks;
- time decay and overnight gap scenarios;
- spread widening and partial-fill scenarios.

Use full repricing for material shocks. If stress loss exceeds approved budget, reduce/close risk under controlled execution.

---

## 10. Session, expiry, intraday and overnight policy

### 10.1 Session controls

- Use current official BSE trading calendar, session timings and holidays.
- Do not assume a normal session on holidays, special sessions, or shortened sessions.
- Opening volatility window: default no new complex short-premium structures during first 15 minutes. Directional breakout strategies may trade only after their specific completed-candle trigger and all filters.
- Mid-session: allow only strategies whose regime, liquidity and time-decay assumptions remain valid.
- Late session: progressively tighten entry eligibility and close intraday-only positions before configured cutoff.
- Never open a position when insufficient time remains to execute, monitor and safely exit before cutoff.

### 10.2 Expiry policy

- Read actual expiry from current contract master and exchange calendar.
- Recompute time-to-expiry using exact exchange session/expiry timestamp and timezone.
- Increase gamma, liquidity, slippage and settlement stress buffers as expiry approaches.
- Disable new short-gamma positions inside configurable expiry danger window unless dedicated tested expiry strategy exists.
- Close or manage all positions before broker/exchange-specific cutoffs. Never assume automatic exercise/settlement behavior without verified current rules.
- Calendar/diagonal legs monitored independently as expiries differ.

### 10.3 Intraday policy

- Each strategy declares intraday_only or overnight_allowed.
- Intraday-only positions must close before cutoff even if showing a loss; no accidental overnight carry.
- Late entry blocked if minimum monitoring/exit time unavailable.
- Exit triggers include technical invalidation, payoff/target, time stop, regime flip, liquidity deterioration, risk limit, or system-health failure.

### 10.4 Overnight policy

Overnight is opt-in per strategy and position. Require:
- strategy permits overnight;
- clean broker reconciliation and no pending orders;
- defined maximum loss under gap scenarios;
- adequate available funds/margin buffer;
- no unresolved event or expiry risk;
- portfolio stress and Greeks within stricter overnight caps;
- explicit Telegram/user policy approval if configured.

Use lower size than intraday default (initial policy: at most 50% of ordinary per-trade risk budget). Never carry a position solely because market closed before intended exit.

---

## 11. Position management and exit framework

Each position stores its original thesis and invalidation conditions. Exit logic is deterministic and reason-coded.

### 11.1 Exit priority

1. Emergency/system safety or broker reconciliation failure.
2. Hard portfolio loss, margin, or stress limit.
3. Strategy invalidation / regime flip.
4. Expiry, session cutoff, overnight disallowance.
5. Profit target / trailing rule.
6. Time stop / theta decay / diminishing reward.
7. Optional adjustment only after new full risk approval.

### 11.2 Position monitoring

- Reprice all legs using current executable bid/ask and mark quality.
- Track realised/unrealised P&L after estimated and actual charges.
- Monitor portfolio Greeks, stress loss, margin, liquidity, and short-leg coverage.
- Reconcile broker quantities and active orders periodically and after every fill/cancel/replace.
- If data becomes stale, do not make optimistic marks. Freeze new entries, alert, and use broker-confirmed state for emergency decisions.

### 11.3 No silent legging

Never close a protective long leg while its short leg remains open unless a validated sequence explicitly preserves risk bounds. For multi-leg entry, establish protective long legs before short legs where broker/exchange rules and margin permit; after each fill, recompute residual portfolio. If a protective leg is partially filled, size dependent short legs only to confirmed protected quantity. If a short leg fills without intended protection due to race/partial fill, immediately enter recovery mode and execute safest available hedge/close sequence.

---

## 12. Execution, order state machine and recovery

### 12.1 Order lifecycle

Persist intent before broker submission. State machine:
CREATED -> VALIDATED -> SUBMITTING -> ACKNOWLEDGED -> OPEN/PARTIAL -> FILLED, with terminal or recovery states REJECTED, CANCEL_PENDING, CANCELLED, EXPIRED, UNKNOWN, RECOVERY_REQUIRED.

- Use deterministic internal intent IDs and broker tags where supported.
- Idempotency must prevent duplicate order submission after retry/restart.
- Never interpret timeout as rejection; query broker state and reconcile.
- Record broker order ID, exchange order ID, average fill price, filled/pending quantity, timestamps and rejection messages.
- Handle partial fills independently per leg.
- Recompute actual structure payoff and protection after each fill.
- Persist every state transition before acknowledging success to Telegram.

### 12.2 Multi-leg sequencing

- Validate all legs, prices, quantity, margin and limits before first order.
- Submit protective long leg(s) first when required to cap exposure and permitted by margin.
- Confirm fills before submitting dependent short legs.
- If protective leg is partially filled, size dependent short legs only to confirmed protected quantity.
- If any leg rejects or times out, cancel remaining unfilled intents and enter recovery; do not assume all-or-none.
- For exit, close risk-increasing short legs first where appropriate, then close longs; sequence must be structure-aware and preserve protection during transitions.
- A broker API without atomic multi-leg orders requires explicit partial-fill risk controls.

### 12.3 Restart recovery

On startup:
1. Force PAPER mode.
2. Load durable order intents, broker IDs, fills, positions and audit trail.
3. Authenticate broker read-only and fetch current BFO orders/positions.
4. Reconcile internal and broker states.
5. Mark unresolved/ambiguous orders UNKNOWN and block new entries.
6. Alert with exact residual exposure.
7. Do not auto-resume live mode. Require fresh explicit Telegram activation only after readiness passes.

### 12.4 Emergency close-all

Emergency flow:
1. Halt new entries immediately.
2. Persist emergency state and alert.
3. Fetch current broker orders/positions and reconcile.
4. Cancel pending entry orders where safe and verify cancellation.
5. Calculate residual exposure including partial fills.
6. Protect short legs first and close in controlled structure-aware sequence.
7. Re-query broker state after each order and retry only after checking idempotency and actual status.
8. Report each leg as filled/part-filled/rejected/pending/unknown.
9. Keep HALTED state until broker positions are flat and reconciliation confirms it.
10. If closure fails, report exact remaining instrument, side, quantity, broker order state and manual intervention requirement. Never claim success.

---

## 13. Broker integration: Zerodha Kite Connect

- Use a narrow adapter interface for instruments, quotes, candles, margins, orders, positions, and order updates.
- Read-only market data and reconciliation are separate from order placement permissions.
- Never log API secrets, access tokens, request authorization headers, or credential-bearing URLs.
- Access token/session generation and renewal must be an explicit secure workflow; never invent or store credentials in source control.
- Respect API rate limits and batch quote requests within documented limits.
- Handle network errors, throttling, malformed responses, stale responses and session expiry as explicit failure states.
- Verify current BSE segment/product/order constraints before enabling order submission.
- Broker margin estimates are not a substitute for strategy max-loss calculation.
- Use broker-confirmed positions and fills as authority for actual exposure, while preserving internal intent history.

---

## 14. Telegram operations and notifications

### 14.1 Security

- Allowlist both Telegram user ID and chat ID.
- Reject groups/channels unless explicitly allowlisted.
- Never trust usernames as identity.
- Deduplicate updates by Telegram update ID and persist command audit before dispatch.
- Commands that change state require authorization, idempotency, confirmation where appropriate, and durable audit result.
- Do not echo tokens, credentials, or sensitive broker data.

### 14.2 Required commands

| Command | Behaviour |
|---|---|
| /start, /help | Show supported commands and current safety mode |
| /status | Mode, health, regime, market-data freshness, broker/reconciliation readiness |
| /paper | Force paper mode immediately |
| /live | Create short-lived pending live request; does not activate |
| /confirm_live | Confirm pending request only if every readiness gate passes |
| /pause | Stop new entries; continue safe monitoring |
| /resume | Resume only if health, reconciliation and risk gates pass; live mode remains separately confirmed |
| /positions | Broker-reconciled positions with legs, quantity, P&L, Greeks and risk |
| /orders | Open/recent orders and per-leg states |
| /pnl | Realised, unrealised, charges, daily/weekly/monthly results |
| /capital | Net equity, deposits/withdrawals, cash, margin and available risk budget |
| /risk | Drawdown, current limits, open risk, stress and margin utilisation |
| /strategies | Strategy eligibility, regime, candidate score and rejection diagnostics |
| /close position_id | Controlled close with confirmation and broker reconciliation |
| /closeall | Emergency controlled close-all sequence |
| /emergency | Halt entries, reconcile, protect/close exposures, report residuals |

A command must not return a fabricated empty portfolio when service is unavailable. Report UNAVAILABLE, STALE, or UNRECONCILED explicitly.

### 14.3 Alerts

Notify on entry intent, each fill/partial fill, rejection, exit, risk threshold, regime transition, stale data, broker auth failure, reconciliation mismatch, margin warning, drawdown stop, emergency action, and overnight carry decision. Every alert includes timestamp, position/order ID, strategy, exact status and next action.

---

## 15. Persistence, audit, observability and security

- SQLite is acceptable for initial single-process deployment if transactions, WAL/locking policy, backups, and recovery are tested. Provide migration path to server database before multi-instance operation.
- Use append-only event history plus materialized current-state tables.
- Enforce unique constraints on command update IDs, internal intent IDs, broker order IDs where available, and fill IDs.
- Store schema versions and migration history.
- Use explicit transactions and deterministic connection closure.
- Persist mode transitions, risk decisions, candidate diagnostics, broker responses, reconciliation snapshots and operator commands.
- Logs must be structured, redacted and correlated; never log credentials.
- Health checks cover data age, broker auth, persistence, Telegram polling, execution adapter, reconciliation and clock drift.
- Alert on repeated exceptions and prevent silent worker death.
- Backups must be encrypted and restore-tested before live operation.

---

## 16. Testing and acceptance criteria

### 16.1 Unit and property tests

- Indicator calculations against known fixtures and edge cases.
- Regime classification on trends, ranges, transitions, gaps and missing candles.
- Option quote quality, tick alignment, quote skew and stale data.
- IV/Greeks numerical convergence and invalid-price handling.
- Payoff functions across all strike breakpoints and unequal wing widths.
- Max loss is finite and protective coverage holds for every short leg.
- Lot sizing never rounds up and always respects equity, risk, margin and freeze limits.
- Risk/drawdown boundaries and deposit/withdrawal accounting.
- Order state transitions, idempotency, duplicate updates and restart recovery.
- Partial fills, leg rejection, timeouts, cancellation races and protective-leg failures.
- Telegram allowlists, confirmation expiry, replay attacks and audit failures.
- Reconciliation mismatch, unknown orders, malformed broker payloads and non-SENSEX positions.
- Emergency close failure must never be reported as flat.

### 16.2 Integration / paper acceptance

- Run full exchange-session simulations using recorded/synthetic fixtures without sending live orders.
- Exercise all eight strategy families through candidate generation and every rejection path.
- Test costs, conservative bid/ask fills, slippage, spread widening and partial fills.
- Paper trade over a meaningful variety of regimes, expiry distances, volatility states and event sessions.
- Record strategy-level expectancy, drawdown, tail loss, fill quality, turnover, cost sensitivity and regime-specific outcomes.
- No live activation until risk, execution, reconciliation, recovery and operational controls pass a formal checklist.

### 16.3 Live-readiness checklist

Live must remain unavailable until:
- order lifecycle is durable and idempotent;
- multi-leg execution/recovery is tested;
- broker-backed position and order reconciliation is clean;
- close-all has been exercised in controlled paper/sandbox conditions;
- margin and costs are verified;
- Telegram controls and audit are tested;
- monitoring and alert delivery are reliable;
- documented operator runbook exists;
- owner explicitly authorizes live activation.

Passing software tests does not prove strategy profitability.

---

## 17. Implementation roadmap and dependency order

**P0 — Foundation and specification**
- Typed config, exchange calendar, contract master, quote normalization and quality gates.
- Domain models, persistence, audit, paper mode and health.
- Indicators, regime classifier, option-chain analytics and payoff engine.

**P1 — Strategy engine**
- Implement and test the four directional verticals.
- Add condor and iron butterfly only after regime/liquidity/scenario gates.
- Add calendar/diagonal and long-volatility only after multi-expiry scenario modelling.
- Candidate diagnostics, strategy arbitration and no-trade behaviour.

**P2 — Portfolio risk**
- Equity ledger with deposits/withdrawals, dynamic sizing, margin and costs.
- Portfolio Greeks, scenario stress, drawdown controls and overnight policy.

**P3 — Execution and recovery**
- Durable order intents/fills, idempotency, multi-leg sequencing, partial-fill recovery.
- Broker reconciliation integrated into every readiness and order gate.
- Restart recovery and emergency close-all.

**P4 — Telegram and operations**
- Complete read-only reporting commands.
- State-changing commands with authorization, audit, confirmations and failure-safe behaviour.
- Monitoring, alerting, runbooks and backup/restore.

**P5 — Paper validation**
- Run all strategy families through paper sessions and regime coverage.
- Measure operational performance and conservative fill assumptions.
- Fix issues and repeat; no profitability claims absent independent evidence.

**P6 — Live eligibility (not currently authorized)**
- Formal sign-off only after all live-readiness criteria pass.
- Start with smallest permitted risk and manual supervision.
- Every restart returns to paper; Telegram request + separate confirmation required.

---

## 18. Current implementation status and known gaps (updated 2026-11-27)

The feature branch now contains substantially more than the initial foundation. Status below reflects code present and tested; a foundation or planning primitive is not described as a complete live service.

Implemented and covered by automated tests:
- Python package, typed settings, SQLite persistence, append-only command audit and paper-first startup.
- Telegram user/chat allowlisting, separate live request/confirmation boundary, idempotent update handling, and fail-safe audit finalization.
- Read-only Kite BFO instrument, quote, position, order and per-order trade-history access.
- Contract/option-chain validation, data-quality checks, IV/Greeks estimates, EMA/ATR/ADX regime foundation and four directional vertical candidate generators.
- Expiry payoff validation and foundational risk/capital sizing controls.
- Durable order lifecycle, broker-order-ID integrity, idempotent partial/late fills, multi-leg position lifecycle and fill-to-position projection recovery.
- Durable broker-fill inbox, processing leases and stale-worker fencing.
- Read-only broker reconciliation against persisted positions and unresolved live orders.
- Deterministic protective-leg execution planning, paper-only sequential execution coordinator, and durable broker-agnostic submission/recovery boundary. The submission boundary is not wired to Kite and live submission remains disabled.
- Ordered Kite startup recovery, including pending-receipt replay, current-session trade-history backfill and broker reconciliation, now invoked before Telegram polling when credentials are configured.
- Read-only Telegram positions/orders views, Ruff and pytest CI.

Partially implemented or not yet complete against this specification:
- Durable fill-confirmed multi-leg live orchestration, including partial-fill/reject/timeout handling, cancel/replace and residual-exposure management.
- Full live-readiness dependency wiring for fresh market data, contract master, broker health, persistence and portfolio risk; Telegram resume/live confirmation remain fail-closed without it.
- Managed emergency close-all with verified broker fills and final reconciliation.
- Full authoritative Telegram P&L, capital, risk and strategy reporting, plus safe managed position controls.
- Iron condor, iron butterfly, calendar/diagonal and defined-debit long-volatility candidate engines.
- Complete portfolio Greeks/stress scenarios, multi-expiry modelling, strategy arbitration and correlation controls.
- Complete exchange-calendar, session, expiry, overnight and event-policy enforcement.
- Kite access-token generation/renewal and secure session lifecycle.
- Structured production monitoring, alerting, runbooks, encrypted backup/restore drills, security review and end-to-end paper validation across all strategy families/regimes.

The environment-backed Telegram entrypoint attempts read-only Kite startup recovery before polling when both Kite credentials are configured. The Telegram lifecycle now starts a supervised recurring Kite monitor: it replays the durable inbox, backfills current-session individual trades, refreshes the BFO contract master and reconciles broker positions/orders on each cycle. It recreates the Kite adapter after failures, retries with bounded backoff, and uses an independent watchdog for stale data and worker death. Unhealthy state forces paper mode, pauses entries and closes the startup-recovery gate; health transitions are logged and alerted to allowlisted Telegram chats. Recovery never auto-resumes entries or restores live mode. Missing credentials leave recovery incomplete and generate an operator alert. Credentials use masked secret settings and are unwrapped only at the adapter boundary. This monitor is read-only and is not full live readiness; it does not enable order placement.

The branch remains a development/paper foundation. It is not ready for live production deployment. No live trading, deployment, broker credential change or repository merge is authorized by this status update.
---

## 19. Configuration and governance

- All thresholds, delta bands, time windows, risk limits, regime thresholds, strategy versions and expiry cutoffs must be typed, validated, versioned and auditable.
- No strategy may silently change rules based on an unreviewed parameter update.
- A strategy configuration change requires tests and a recorded version.
- Market data, strategy, risk, execution and broker adapter versions must be included in each candidate and trade audit.
- Keep the production repository free of backtesting/research tooling as previously requested. Strategy evaluation outputs may be reviewed separately; do not add a backtest engine or push research datasets unless explicitly authorized.
- This specification is the target contract. Implementation status must be updated honestly as each requirement is delivered; documentation must never imply a feature is implemented merely because it is specified.

## 20. Canonical pending-work register (stable IDs)

This register is the single source of truth for continuation updates. The IDs are
stable and must not be renumbered or regrouped. A task is COMPLETE only when its
acceptance conditions are implemented, tested, and verified on the branch. A
foundation, stub, plan, injectable interface, or paper-only simulation does not
complete a live requirement. The count below counts IDs, not sub-bullets.

**Register update: PE-01 through PE-05 are COMPLETE. PE-05 adds the allowlisted,
audited, fail-closed emergency close-all path with entry halt, pending-entry
cancellation, durable fill recovery, structure-aware close sequencing and final
broker-flat reconciliation. PE-06 remains a required dependency for normal live
entry readiness and authoritative equity/drawdown snapshots. The canonical
register now has 12 pending or partial items.** All are in scope because they map
directly to requirements in sections 5–16. No additional feature may be added
unless it is explicitly required by this document.

| ID | Status | Requirement and completion condition |
|---|---|---|
| PE-01 | COMPLETE | **Supervised broker fill acquisition and reconciliation.** Recurring current-session Kite trade-history polling, supervised Telegram lifecycle, adapter recreation/retry, missed-event backfill through the durable inbox, periodic broker reconciliation, watchdog stale/worker-death detection, allowlisted Telegram alerts and fail-closed paper/pause behaviour are implemented and covered by CI at commit f3d973462e3a15ef741c7f24ec2de7355827f0ab. |
| PE-02 | COMPLETE | **Kite order-placement adapter.** Added a BFO SENSEX-only LIMIT adapter with refreshed-contract/token/symbol checks, whole-lot and tick-aligned price validation, NRML/DAY/regular order policy, deterministic 20-character alphanumeric tag derived from the durable client key, exact same-day order-book lookup bound to persisted order identity, guarded LIMIT modify/cancel, response/status validation, and a conservative per-process request throttle. Kite tags are correlation hints, not broker-side idempotency guarantees; the durable submission boundary never retries uncertain submissions, and missing/ambiguous lookup results remain blocked. PE-04 constructs this adapter for readiness; PE-05 uses it only inside the allowlisted emergency CLOSE service. Normal strategy-entry submission remains disabled. |
| PE-03 | COMPLETE | **Durable live multi-leg execution and recovery.** Implemented in `live_execution.py`: immutable durable LIMIT intents, fill-ledger gating, one-shot broker submission/recovery, confirmed cancel-before-replace, partial/reject/timeout handling, cancel/fill race recovery, short-first residual cleanup, unresolved-opening-order close protection and explicit blocked exposure reporting. The coordinator/submission flags remain disabled for normal strategy entry; PE-05 explicitly enables them only inside the Telegram emergency CLOSE service. The coordinator is never invoked with OPEN by this runtime path. |
| PE-04 | COMPLETE | **Complete live-readiness wiring.** Telegram runtime constructs and injects authenticated Kite read-only and order adapters, current BFO contract master, paired CE/PE quotes (2-second freshness, depth/volume/spread/skew checks), SQLite quick-check, durable position/order lifecycles, broker reconciliation, live account funds/margin checks, adaptive portfolio-risk gate and explicit Kite static-IP confirmation. Both /resume and /confirm_live evaluate every gate. Missing dependencies deny readiness. The portfolio risk gate requires authoritative PE-06 equity/drawdown snapshots and therefore intentionally remains false until PE-06 is complete; no risk values are fabricated and normal live entries remain disabled. PE-05 is a separate close-only emergency exception. |
| PE-05 | COMPLETE | **Managed emergency close-all.** /closeall and /emergency immediately pause entries and force PAPER mode; recover and cancel correlated pending OPEN orders, verify broker cancellation and racing fills, reconcile broker/local SENSEX net exposure, resume only durable VALIDATED CLOSE intents, close every managed LIVE position through PE-03 (short legs first, then protective longs), require per-leg individual trade fills, permit bounded replacements only after terminal/cancel-confirmed state, and perform final broker-position/order-book/fill-projection reconciliation. Uncorrelated manual SENSEX exposure/orders, stale quotes, missing static-IP confirmation, unknown orders, and broker/local mismatches block automatic closure and are reported as residuals. Emergency operation remains halted in PAPER mode; no auto-resume. |
| PE-06 | PARTIAL | **Complete equity and portfolio risk accounting.** Added a durable append-only account ledger with atomic idempotency, immutable explicit opening equity, deposits/withdrawals separated from profit, fee-aware realised P&L, broker-reconciled equity marks, flow-adjusted peak/weekly/monthly drawdown projections, strategy-specific risk/allocation lot sizing, and fail-closed `/pnl`, `/capital`, `/risk` reports. The mark path now invokes the existing broker position/order reconciliation service and refuses mismatches or unresolved orders; live readiness reads the persisted fresh snapshot and remains blocked when it is absent/stale/inconsistent. Remaining: automatically ingest Kite trade fills, realised/unrealised P&L and actual charges; derive open max-loss from persisted filled strategy/payoff data rather than a supplied value; source broker net liquidation equity from an authoritative account balance feed (Kite margin is not treated as equity); calculate entry counts and open risk from actual ledgers; exercise the end-to-end runtime path and verify CI. Partial implementation head: `53734d3973fc6192d5376245b79be303594d31d3`. Ruff passed; pytest **247 passed**; **82% total statement coverage**. PR CI run: https://github.com/artvibe1999/sensex-options-portfolio-engine/actions/runs/46015071913 — passed. PE-06 remains PARTIAL because the automatic broker accounting/payoff-risk integration gates listed above are still outstanding. |
| PE-07 | PARTIAL | **Portfolio Greeks and scenario stress.** Complete aggregate Greeks and multi-leg/multi-expiry stress calculations, including defined-risk verification and their use in candidate, sizing, monitoring and readiness gates. |
| PE-08 | PARTIAL | **Strategy arbitration and correlation controls.** Complete candidate scoring/selection, portfolio interaction and correlation checks, concurrency limits, rejection diagnostics, and deterministic no-trade decisions. |
| PE-09 | NOT STARTED | **Iron Condor candidate engine.** Implement the required regime, liquidity, IV/Greek, expiry, payoff, margin, risk and scenario gates; add tests for valid and rejected candidates. |
| PE-10 | NOT STARTED | **Iron Butterfly candidate engine.** Implement the required regime, liquidity, IV/Greek, expiry, payoff, margin, risk and scenario gates; add tests for valid and rejected candidates. |
| PE-11 | NOT STARTED | **Calendar/Diagonal candidate engine.** Implement only after multi-expiry contract alignment and scenario modelling are available; validate expiry, term-structure, risk and execution gates. |
| PE-12 | NOT STARTED | **Defined-debit long-volatility candidate engine.** Implement the specified defined-debit structures with volatility, liquidity, expiry, payoff, margin and scenario gates. |
| PE-13 | PARTIAL | **Exchange calendar and lifecycle policies.** Complete authoritative exchange-session/holiday handling, expiry cutoffs, intraday/overnight eligibility, event-session restrictions and restart-safe enforcement. |
| PE-14 | PARTIAL | **Telegram reporting and managed position controls.** Complete authoritative /status, /positions, /orders, /pnl, /capital, /risk and /strategies reporting plus safe confirmed /close, /closeall and /emergency flows. Never return fabricated empty values. |
| PE-15 | NOT STARTED | **Kite session lifecycle.** Implement the documented secure access-token generation/renewal and expiry/re-authentication workflow without storing secrets in source control or logs. |
| PE-16 | PARTIAL | **Production operations and security.** Complete structured redacted monitoring, health checks, alert delivery, supervised worker failure alerts, operator runbooks, encrypted backups, restore drills, migration/recovery checks and production security review. |
| PE-17 | NOT STARTED | **End-to-end paper acceptance.** Exercise all eight strategy families and required rejection paths over representative sessions/regimes/expiry distances/volatility states, including conservative costs, slippage, spread widening and partial fills. Record the required operational and risk metrics; resolve failures and repeat. This is validation, not a profitability guarantee. |

### Register rules

1. Every continuation report must reproduce these same IDs and statuses, or a
   clearly identified subset with the total unchanged.
2. Update a status only when code and tests establish the acceptance condition.
   Record the verifying commit and CI result beside any status transition.
3. A newly discovered defect is attached to the relevant existing ID. Create a
   new ID only if the original requirements contain a distinct requirement that
   is not represented above; explain the exact section mapping before adding it.
4. Do not count CI reruns, documentation regrouping, or subtask splitting as new
   pending items.
5. The remaining count is the number of IDs not marked COMPLETE. PARTIAL and
   BLOCKED items remain pending.
6. The register is not authorization to merge, deploy, enable live mode, or place
   orders. Paper mode remains the default and all existing live-confirmation
   controls remain mandatory.



## PE-03 — durable live multi-leg execution completed
- Added `LiveMultiLegExecutionCoordinator`, composing the order lifecycle, position lifecycle, durable one-shot broker submission service, Kite LIMIT adapter contract and Kite individual trade-history fill ingestor.
- Both coordinator live execution and the durable submission service default disabled. The coordinator is not constructed by Telegram runtime; no live execution command was enabled.
- Opening sequence is deterministic: protective BUY legs first, then SELL legs. Every next leg is gated on exact requested quantity being present in the individual trade-history fill ledger and projected into the durable position ledger. Broker acknowledgements and order-book COMPLETE status alone cannot advance.
- Closing sequence buys back SELL-origin short legs before selling BUY-origin protective longs.
- Partial/open orders are cancelled through a durable CANCEL_PENDING claim. A cancel request acknowledgement is not final; exact broker order state must confirm CANCELLED before a replacement intent can be created. Replacement quantity is recalculated from durable remaining exposure and bounded by the configured replacement count.
- Failed opening attempts unwind only after all related opening orders are terminal or cancellation-confirmed. Cleanup covers short residuals before long residuals. Non-whole-lot residual quantities, unknown orders, missing correlation, incomplete trade-history projections and unresolved cancellations remain blocked with per-leg residual reporting.
- No uncertain submission is automatically retried. Restart recovery reuses the original immutable order intent and exact client-key lookup; a new replacement uses a new durable intent only after the previous order is confirmed terminal.
- Added cancellation-terminality projection so late fill events after cancellation preserve the cancel-confirmed fact. Late opening fills discovered after flattening reopen the position to CLOSING instead of disappearing from the exposure ledger.
- Added a hard execution gate that replays pending fill-inbox receipts and refuses any new broker action while fill-to-position projection remains incomplete.
- Verification at commit `c42131f3de93b49e45fefaea49e93eb127488819`: Ruff passed; pytest **205 passed**; **85% statement coverage**. Push CI and PR CI both passed.
- PE-03 is an isolated execution capability; PE-04 separately completed the runtime readiness dependency graph. The PE-03 coordinator remains uninvoked by Telegram and its live-enable flags remain false. Paper remains the process-start default.


### PE-03 exact-head verification
- Branch head: `272f949adeda4cd06b21b293a0aa153bfce4ec02`.
- Ruff passed; pytest **206 passed**; **85% statement coverage**.
- Push CI: https://github.com/artvibe1999/sensex-options-portfolio-engine/actions/runs/46006148413 — passed.
- Pull-request CI: https://github.com/artvibe1999/sensex-options-portfolio-engine/actions/runs/46006138830 — passed.
- PR #1 remains open, draft and unmerged. No deployment or live order was performed.


## PE-04 — full runtime live-readiness wiring
- `build_application_from_settings` now constructs shared `PositionLifecycle` and `OrderLifecycle` services and injects the runtime readiness provider into Telegram command dispatch.
- With Kite credentials, the runtime constructs `KiteReadOnlyAdapter`, `KiteOrderExecutionAdapter`, and `LiveReadinessService`; broker, adapter, store, lifecycle services and readiness service are retained in `application.bot_data`. The execution adapter is never invoked by PE-04.
- `LiveReadinessService` now explicitly checks broker authentication, persistence health, execution prerequisites, fresh contract master, market-data timestamp, broker reconciliation and portfolio-risk clearance. Missing providers and exceptions reject readiness.
- Added `SqliteStore.health_check()` using SQLite quick-check and table-read probes.
- Added authenticated `KiteReadOnlyAdapter.margins("equity")` access and a live margin-readiness gate. It validates enabled segment, available cash/live balance, positive net funds, finite broker margin values and configured margin-utilisation ceiling.
- Added paired same-expiry/same-strike SENSEX CE/PE quote checks: maximum age 2 seconds, maximum bid/ask spread 3%, positive top depth and volume, and maximum quote timestamp skew 2 seconds.
- Added explicit `KITE_STATIC_IP_CONFIRMED` setting, default false. It must only be enabled after the account's static IP has been registered with Kite. Zerodha states that order requests from unregistered IPs are rejected.
- Added adaptive portfolio risk readiness checks for equity, available margin, peak-equity drawdown, daily loss, weekly/monthly drawdown, maximum concurrent positions, daily entries and aggregate open risk.
- PE-06 remains incomplete, so no authoritative portfolio risk snapshot provider is currently supplied by the runtime. The live risk gate explicitly rejects readiness until that dependency exists. This prevents false approval while preserving PE-04's complete wiring.
- If Kite credentials or the optional SDK are missing, runtime construction falls back to a deny-all provider and keeps startup in PAPER mode with entries paused. No credentials are logged.
- `/resume` and `/confirm_live` both evaluate the full readiness provider; startup recovery alone cannot enable trading.
- PE-04 does not enable order submission, activate the PE-03 coordinator, merge the PR, or deploy the application.


### PE-04 exact-head verification
- Exact branch head: `6e0fba9efdddc4e7bd7932f5eb078a7430b208a3`.
- Ruff passed; pytest **218 passed**; **83% total statement coverage**.
- Push CI: https://github.com/artvibe1999/sensex-options-portfolio-engine/actions/runs/46007642623 — passed.
- Pull-request CI: https://github.com/artvibe1999/sensex-options-portfolio-engine/actions/runs/46007651387 — passed.
- PE-04 is complete as a runtime readiness integration. Live activation remains blocked by the deliberately unavailable authoritative PE-06 portfolio-risk snapshot provider, and also requires operator-confirmed Kite static-IP registration.
- PR #1 remains open, draft and unmerged. No deployment or live order was performed.


### PE-04 final exact-head verification
- Final branch head: `bd6ca584c9f7a8bded7e8cad20fdaf90a05b8e43`.
- Ruff passed; pytest **218 passed**; **83% total statement coverage**.
- Push CI: https://github.com/artvibe1999/sensex-options-portfolio-engine/actions/runs/46007994344 — passed.
- Pull-request CI: https://github.com/artvibe1999/sensex-options-portfolio-engine/actions/runs/46008002081 — passed.
- PR #1 remains open, draft and unmerged. No deployment or live order was performed.


## PE-05 — managed emergency close-all

- Added EmergencyCloseAllService and a structured EmergencyCloseReport. Each invocation persists a durable start and finish/failure event, including unresolved orders, residual broker quantities and reasons.
- Telegram /closeall and /emergency are allowlisted and audited. They immediately pause new entries and force PAPER mode before running the synchronous broker workflow in a worker thread. The handler remains responsive.
- While emergency execution is active, /resume, /live and /confirm_live are rejected. Completion does not restore entries or LIVE mode.
- The service requires confirmed Kite static-IP registration, healthy durable SQLite, authenticated Kite access and a fresh SENSEX BFO contract master before attempting broker order actions.
- Every correlated LIVE opening order is recovered by its durable client tag. Unsubmitted entry intents are rejected locally; submitted entries are cancelled once, then refreshed and trade-history backfilled until broker terminal/cancel-confirmed state is established. A cancel acknowledgement alone never permits closing or replacement.
- Existing close orders are never cancelled by emergency handling. Uncertain close orders block duplicate closes. A durable, validated-but-not-submitted close intent may be resumed by the same PE-03 coordinator after broker/local exposure preflight.
- Before closing, the service reconciles signed SENSEX broker net quantities with durable LIVE position fills and checks the broker order book. Unknown/unmanaged SENSEX exposure or uncorrelated active SENSEX orders are not adopted as fabricated fills and block automated flattening.
- Each managed position is reconstructed from its immutable durable plan and the current contract master, then closed through LiveMultiLegExecutionCoordinator. Its short legs are bought back before protective long legs are sold. Closing LIMIT prices use fresh validated bid/ask quotes, current tick sizes and marketable-side rounding. Exact individual trade-history fills are required; bounded replacements are permitted only after terminal/cancel-confirmed prior orders.
- After closure, the service backfills current-session trade history again, verifies all durable positions and orders are terminal/flat, and independently re-reads Kite net positions and active SENSEX orders. Local CLOSED status alone is never accepted as proof of broker flatness.
- If broker authentication, static IP, persistence, quote quality, order correlation, cancellation, fill projection or final reconciliation fails, the workflow stops or leaves remaining managed positions for explicit residual reporting. It never claims success while exposure is unverified.
- PositionLifecycle.finalize_flat supports positions with zero recorded exposure only through an explicit durable flat-finalization event. Generic status transitions cannot manually mark an open position CLOSED.
- The emergency service is the only runtime caller of the PE-03 coordinator. It invokes CLOSE only; it never invokes OPEN or normal strategy entry. The durable submission/coordinator enable flags are explicitly enabled only within this close-only service.
- PE-05 is a close-only safety path. It does not bypass PE-06 risk accounting for new live entries, auto-resume after emergency, deploy, or place any order during implementation.
