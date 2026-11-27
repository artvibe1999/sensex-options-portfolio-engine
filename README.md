# SENSEX Options Portfolio Engine

Independent Python platform for managing defined-risk SENSEX options portfolios through Zerodha Kite Connect, with Telegram operations and a paper-first safety model.

## Safety defaults

- Paper trading is the default on every process start.
- Live trading requires an authenticated Telegram request and a separate explicit confirmation.
- Broker state is reconciled before position-changing actions.
- Stale or invalid market data, unavailable margin, uncertain regime, or failed risk checks result in no new trade.
- Credentials and tokens are supplied through environment variables or a secret manager; never commit them.

## Scope

The initial implementation is being developed on a dedicated feature branch. It will include configuration, domain models, risk controls, a paper execution adapter, broker interfaces, Telegram authorization boundaries, persistence, tests, and operational documentation.

This project does not promise returns. Strategy eligibility is conditional on market data quality, liquidity, costs, margin, and portfolio risk.

## Development

See `docs/architecture.md` and `docs/safety-model.md` as the implementation is added. Backtesting/research tooling is intentionally excluded from the production repository.
