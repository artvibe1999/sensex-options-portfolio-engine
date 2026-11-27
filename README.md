# SENSEX Options Portfolio Engine

Independent Python platform for managing defined-risk SENSEX options portfolios through Zerodha Kite Connect, with Telegram operations and a paper-first safety model.

## Safety defaults

- Paper trading is the default on every process start.
- Live trading requires an authenticated Telegram request and a separate explicit confirmation.
- Broker state is reconciled before position-changing actions.
- Stale or invalid market data, unavailable margin, uncertain regime, or failed risk checks result in no new trade.
- Credentials and tokens are supplied through environment variables or a secret manager; never commit them.

## Scope

SENSEX index options only. The target system supports defined-risk multi-leg strategies, intraday positions, and explicitly approved overnight positions. The initial capital value is not a fixed ceiling: sizing and eligibility use current verified net equity, deposits/withdrawals, open risk and margin.

This project does not promise returns. Strategy eligibility is conditional on market data quality, liquidity, costs, margin, and portfolio risk. Strategy rules are candidate specifications and require forward validation; they are not claims of proven profitability.

## Requirements and architecture

- [Complete product, architecture, strategy, indicator, entry/exit, risk, execution and operations requirements](docs/requirements.md)
- [Architecture and current component design](docs/architecture.md)
- [Safety model](docs/safety-model.md)
- [Development guide](docs/development.md)

Backtesting/research tooling is intentionally excluded from the production repository unless explicitly authorized.

## Development

The implementation is developed on a dedicated feature branch and reviewed through a draft pull request. The project is not live-deployable while execution, reconciliation, recovery, and live-readiness gaps remain. The Telegram environment entrypoint now attempts read-only Kite startup recovery before polling when both `KITE_API_KEY` and `KITE_ACCESS_TOKEN` are supplied; missing credentials or any recovery failure keeps resume and live confirmation blocked. This startup check does not enable live orders or replace recurring fill supervision and full live-readiness checks. See the requirements document's implementation-status section for the current gap list.
