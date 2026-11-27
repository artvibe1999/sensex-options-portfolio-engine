# Architecture

The application is split into domain, market-data/broker ports, strategy eligibility, portfolio risk, execution, persistence, and Telegram control boundaries.

## Data flow

1. Load the current BSE contract master and validate SENSEX option instruments, expiry, lot size, tick size, and tradingsymbol.
2. Build option-chain snapshots from supported broker quotes/instruments. Kite Connect capabilities and rate limits must be verified; do not assume a native complete option-chain endpoint.
3. Validate quote freshness, bid/ask spread, depth/volume, IV/Greeks availability, and contract identity.
4. Classify regime. Unknown/transition regimes are no-trade.
5. Generate only defined-risk multi-leg candidates and calculate payoff, margin, transaction costs, and stress loss.
6. Evaluate the candidate against portfolio-wide limits and current broker reconciliation.
7. Submit through the selected execution port. Persist intent before sending orders and reconcile every leg/fill.
8. Monitor positions and report state through Telegram.

## Capital accounting

Sizing uses net liquidation equity, not the initial deposit. Deposits and withdrawals are tracked separately from trading P&L. Realised/unrealised P&L, fees, taxes, margin, and open max loss are reconciled. Exposure contracts as equity falls and only scales when a qualifying strategy passes all gates.

## Strategy catalogue direction

Bull call debit spread, bull put credit spread, bear put debit spread, bear call credit spread, iron condor, iron butterfly, calendars/diagonals, and defined-debit long-volatility structures are candidates. Each must have explicit regime, liquidity, IV, Greek, expiry, and scenario gates. No profitability claim is made without independent validation.

Backtesting/research tooling is intentionally excluded from this production repository.
