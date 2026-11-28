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

## Strategies (`strategies.py`) - winners only (owner decision 2026-11-28)

The owner asked for only the strategies that showed after-charge profits in the archive replays. The catalogue is now:

| # | Family | Entry rules | Strikes and management | Capital |
|---|---|---|---|---|
| 1-2 | **Buy call / buy put, lock with a hedge** (leg-in) | 09:35-13:30; EMA9>21>50 (calls) or EMA9<21<50 (puts); ADX ≥ 20 and rising; DI agrees; RSI 55-75 (25-45 for puts); break of structure or opening-range break; regime agrees | **Buy:** the highest-delta strike in 0.20-0.60 (0.30-0.60 from ₹1L) whose 20% stop risk fits the risk budget. **Before the hedge:** exit on a -25% premium stop or at 15:10 (no EMA21 exit). **At +40%:** sell the furthest-OTM same-expiry strike whose bid ≥ the long's entry price, so the spread can no longer lose money apart from charges. **After the hedge:** hold to the 15:10 square-off (no width cap). One entry per direction per day; 1 lot | All levels |
| 3 | Bull put credit | Confirmed higher low, support tested, close > previous high, EMA20>50, above VWAP, RSI 50-65, ADX > 18; short put below support | Short 0.20-0.30, wing 0.05-0.15, narrowed to fit capital | ≥ ₹2L |
| 4 | Bear call credit | Mirror image of the bull put; short call above resistance | Same as bull put | ≥ ₹2L |

How each piece works:
- **Leg-in sizing:** the stop risk (20% of premium plus charges) is budgeted against the capital tier's risk per trade. The full premium must fit deployable capital, which is 100% of equity by owner decision.
- **Live hedging:** the lock is placed through `LiveMultiLegExecutionCoordinator.execute_protected_short`. It refuses unless the durable ledger shows the long fully open, with enough quantity, the same series and a further-OTM strike. A live leg-in close buys back the short first, then sells the long.
- **Removed:** bull call debit and bear put debit spreads lost money in the replay. Iron condor, iron butterfly, calendar/diagonal and long iron butterfly never qualified in the replay period, so there is no evidence for them.

### Strategy selection evidence (20 sessions, 2026-10-27 to 2026-11-27, archive bid/ask)

Leg-in lock (+40% trigger, 20% stop), net of charges:

| Capital | Net P&L | Profit factor |
|---|---|---|
| ₹30k | +₹3,559 | 1.30 |
| ₹50k | +₹4,929 | 1.26 |
| ₹1L | +₹2,506 | – |
| ₹2L | +₹8,950 | – |
| ₹5L | +₹11,710 | – |

Win rate was about 41-45% of 22 trades; the strategy wins because its average win (₹2,644) is larger than its average loss (₹1,451).

The credit spreads used a proxy VWAP built from option-chain volume, because the archive has no futures data:
- **Bull put:** ₹2L +₹1,076 (5 of 6 won); ₹10L +₹4,726.
- **Bear call:** ₹2L +₹420; ₹10L +₹2,402. At ₹50k both lost money, because charges outweighed the edge.

### Engine validation replay (the integrated bot, 1-lot leg-in cap)

| Capital | Trades | Won | Net after charges | Profit factor | Max drawdown |
|---|---|---|---|---|---|
| ₹30k | 15 | 8 | +₹2,309 | 1.35 | ₹2,757 |
| ₹50k | 22 | 10 | +₹3,278 | 1.20 | ₹5,472 |
| ₹2L | 24 | 9 | +₹3,037 | 1.10 | ₹13,640 |
| ₹5L | 24 | 9 | +₹2,728 | 1.09 | ₹13,949 |
| ₹10L | 24 | 9 | +₹2,728 | 1.09 | ₹13,949 |

These figures are for the leg-in strategy only. Two settings came out of this replay:
- **1-lot cap.** Uncapped multi-lot sizing at ₹5L-10L turned the same signals negative (−₹29.6k and −₹8.9k), so leg-in is capped at 1 lot (`leg_in_max_lots`).
- **Credit spreads disabled by default.** Adding them helped at ₹2L (+₹6.2k) but hurt at ₹5L/₹10L (−₹4.8k, −₹1.2k). An open spread blocks same-direction leg-in winners, so the deploy default is `ENABLED_STRATEGIES=SENSEX_LEGIN_LOCK_CALL,SENSEX_LEGIN_LOCK_PUT`.

**Caveat:** in all results, the first half of the period lost money and the second half made it. The sample is small, so this is not proof of a durable edge. Monitor it in paper.

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


## 2-year walk-forward (2026-11-28) and current configuration

The test ran on modelled option prices, using real SENSEX and India VIX 5-minute history (Dec 2024 - Nov 2026). The model was calibrated on the archive (ATM IV ≈ 1.124 × VIX, 12% median error). Year 1 is the period the design never saw.

| Design | ₹30k: year 1 / year 2 | ₹50k: year 1 / year 2 | Profit factor |
|---|---|---|---|
| **Lock +40%, hold, -25% stop (deployed)** | +15,336 / +15,461 | +17,964 / +28,541 | 1.12-1.14 |
| Lock +60%, hold | +6,753 / +14,532 | -105 / +44,188 | 1.09-1.11 |
| Trailing stop | +11,734 / +36,748 | +5,407 / +51,099 | 1.15-1.21 |
| Old (lock +40%, cap 80%, EMA21 exit) | +1,942 / +8,388 | -4,011 / +1,381 | 0.99-1.05 |

What the test showed:
- **Thin edge.** Average profit is about ₹50-150 per trade.
- **Large drawdowns.** At ₹30k-₹50k the worst drawdown was over half the capital, with up to 11 losses in a row.
- **No ₹5k-per-trade strategy.** Only about 12% of trades reached ₹5k, even at ₹2L or more.

The engine stays PAPER only.
