# Strategy engine (runtime)

`python -m sensex_engine` runs the autonomous SENSEX portfolio engine. Every process start is **PAPER**.

## Cycle

Every 30 s during 09:15–15:30 IST (weekdays). Holidays/closed sessions fail closed from data: an index quote older than 120 s freezes the cycle, and (because Kite keeps refreshing the index timestamp after hours) option quotes with zero bid/ask produce an empty chain, so no entry is possible and positions are not marked optimistically.

1. Re-read the shared Kite token (`KITE_TOKEN_FILE`), refresh the BFO contract master once per day, and fetch one batched quote. The batch covers the index, near-month future, strikes within ±3.5% of spot for the two nearest **non-expiring** expiries, and every open leg.
2. Fetch 5-minute SENSEX index bars (structure, trend, momentum) and near-month SENSEX futures bars (volume and session VWAP; the index has no volume). Forming bars are never used.
3. Build the market state:
   - EMA 9/20/21/50/200, ADX/±DI, RSI, MACD histogram, ATR, Bollinger bandwidth and rank, realised volatility
   - Swings, HH-HL/LL-LH, BOS/CHoCH
   - Opening range, previous day/week high and low, range tests
   - From the option chain: ATM IV, expected move (S·IV·√(D/365)) and IV term structure
4. Manage open positions at executable marks: longs at bid, shorts at ask.
5. On every newly completed 5-minute bar, evaluate all eight families, arbitrate, size and execute.

## Strategies (`strategies.py`)

The families are listed in arbitration priority order. Each has explicit setup checks; `/strategies` shows every failed rule.

| # | Family | Key setup rules | Strikes (delta bands) |
|---|---|---|---|
| 1 | Bull call debit | HH-HL, BOS or OR breakout, EMA9>21>50, above VWAP, ADX≥20 rising, +DI>−DI, RSI>55, MACD>0, volume>avg, ATR≥median, no resistance inside reward | long 0.55–0.65, short 0.25–0.35 |
| 2 | Bear put debit | mirror image (RSI<45, MACD<0) | long 0.55–0.65, short 0.25–0.35 |
| 3 | Bull put credit | confirmed higher low, support tested, close>prev high, EMA20>50, above VWAP, RSI 50–65, ADX>18; short put below support | short 0.20–0.30, wing 0.05–0.15 |
| 4 | Bear call credit | mirror image; short call above resistance | short 0.20–0.30, wing 0.05–0.15 |
| 5 | Iron condor | ADX<20, flat or crossing EMAs, VWAP oscillation, narrowing bands, RSI 40–60, ATR contracting, IV>RV, ≥2 tests of each boundary; shorts outside range **and** expected move | shorts 0.15–0.20, wings 0.05–0.10 |
| 6 | Iron butterfly | ADX<18, RSI 45–55, within 0.3 ATR of VWAP, ≥3 VWAP crossings, compressed bands, ATR falling, IV>RV | ATM body, symmetric wings, half the risk budget |
| 7 | Calendar/diagonal | ADX<20, falling short-term RV, front IV ≥ 1.05× back IV | short near 0.40–0.55, long next expiry at the same or a more ITM strike (loss ≤ debit) |
| 8 | Long iron butterfly | recent squeeze, now expanding, ATR rising, ADX turning up, volume, OR break, IV not rich; expected move must exceed the breakeven distance | long ATM, short symmetric wings |

Structures are priced at ask for buys and bid for sells, including the round-trip charge model (`charges.py`).

They are rejected for any of the following:
- spread or depth fails
- Greeks are unavailable
- costs exceed 40% of max profit
- the short strike is inside an opposing level
- reward/risk is too low (debit ≥ 1.1, credit/width ≥ 0.15, condor ≥ 0.12, IB ≥ 0.35)
- one lot exceeds the budget

Survivors are scored 0.25P+0.20L+0.15V+0.15G+0.10O+0.10R+0.05C, where P is the probability of profit under a lognormal at the legs' IV. Anything below `MIN_STRATEGY_SCORE` is rejected.

**Capital fit.** If no in-band structure fits the budget, a second pass keeps the probability-defining leg in its band and narrows the protective or profit-cap leg until the worst case fits (verticals and condor only). The rationale is labelled "width narrowed to fit capital".

**Debit reward/risk.** The requirement's 1.3 minimum is incompatible with its own delta bands. At fair prices, debit/width ≈ the average delta of the two legs, so rr ≈ 1.0–1.6 before bid/ask. The default is 1.1 and is configurable.

## Capital and risk (`capital_policy.py`)

Equity is dynamic:
- **Paper**: base capital (Kite start-of-day funds on first run, or `PAPER_START_CAPITAL`) plus realised paper P&L. It compounds.
- **Live**: Kite `opening_balance + intraday_payin`, plus the engine's realised and unrealised P&L.

| Tier | Equity | Risk/trade | Aggregate | Daily stop | Max positions | Drawdown bands |
|---|---|---|---|---|---|---|
| micro | < ₹1L | 4% | 8% | 6% | 2 | 2.0× spec |
| small | ₹1–3L | 2.5% | 6% | 5% | 3 | 1.5× |
| medium | ₹3–10L | 1.5% | 4% | 3% | 3 | 1.25× |
| standard | ≥ ₹10L | 1% (spec) | 2% | 2% | 3 | 1.0× |

The rupee budget is continuous across tier boundaries, so a larger account never gets a smaller budget. Spec 9.3 drawdown scaling (−25%, −50%, suspend, halt) applies on top. `RISK_SCALE` lowers every tier uniformly.

Lots = floor(min(budget/risk-per-lot, aggregate room, margin room, top-of-book depth, freeze qty)). Margin comes from Kite basket margin, or a conservative estimate. The existing `RiskEngine` is then re-run with the tier limits as the final gate.

What to expect (synthetic chain, IV 14%, 2 DTE):
- Under about ₹2L, the directional debit spreads, calendars and long butterflies usually cannot fund one SENSEX lot. A lot is about ₹16 lakh notional, and those structures need roughly ₹5–8k of worst case.
- Bull put credit, iron butterfly and narrow condors usually can.
- The engine reports this per family rather than forcing a trade.

## Exits (`exits.py`), intraday only

The engine applies these in order:
1. Daily loss stop: close everything and halt for the day.
2. Strategy invalidation: thesis level broken, range broken, VWAP/EMA alignment reversed, regime flip, or short-leg |Δ| above its limit.
3. 15:10 square-off.
4. Profit target: debit verticals 50% of max profit; credits 50%; condor 50%; IB 40%; calendar 15% of debit; long IB 40% of debit.
5. Stop: debit structures at 50% of max loss; credit structures at 1× credit (mark = 2× credit), capped by max loss; calendar 30% and long IB 35% of debit.
6. Long-vol time stop: 6 bars.

New entries stop at 14:30.

## Live mode

1. `/resume`: every readiness gate must pass:
   - Kite auth, persistence, static IP
   - contract master
   - fresh ATM CE/PE quotes ≤ 2 s
   - managed-scope reconciliation
   - tier risk limits and drawdown
2. `/live`, then a separate `/confirm_live`.

Live entries go through the durable, fill-confirmed `LiveMultiLegExecutionCoordinator`: protective legs first, each next leg only after confirmed fills, unwinds on failure, NRML LIMIT orders tagged `PF…`. Live exits use the same coordinator (shorts first).

Calendars are paper-only in live mode, because multi-expiry live sizing is not implemented. Every restart returns to PAPER.

**Shared account.** `RECONCILE_SCOPE=managed` ignores broker positions and orders on instruments this engine neither holds nor tagged. That covers the primary bot's MIS trades and manual trades. Any overlap on an engine-held instrument still fails closed.

## Telegram

`/status /strategies /positions /pnl /capital /risk /pause /resume /paper /live /confirm_live /close <id> confirm /setcapital <amount> /closeall /emergency`

`/setcapital 200000` (also `2.5L`, `50k`, `1cr`) sets current paper equity to that amount: paper base = amount − realised paper P&L, today's start-of-day/peak reset (older equity references removed so drawdown restarts), trade history kept. Refused while paper positions are open; ₹10k–₹10cr; live capital always comes from Kite funds.

You get alerts for entries, exits, failures, daily stops, stale data, broker-monitor health and a 15:30 daily summary. Without a bot token, alerts go to the container log.

## Deployment (sxbot VM, second container)

```
~/sxhedge/            repo archive
~/sxhedge/deploy/.env KITE_API_KEY copied from ~/sxbot/.env (+ Telegram when available)
~/sxhedge/deploy/data engine.db, portfolio.db, heartbeat
```

`cd ~/sxhedge/deploy && docker compose up -d --build`. The compose file mounts `~/sxbot/data` read-only at `/shared/sxbot` for `token.json`.
