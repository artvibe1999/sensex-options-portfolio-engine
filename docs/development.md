# Development guide

## Local setup

1. Use Python 3.11 or newer.
2. Create a virtual environment and install `python -m pip install -e '.[dev]'`.
3. Copy `.env.example` to `.env` only for local development; never commit `.env` or real tokens.
4. Run `ruff check .` and `pytest --cov=sensex_engine --cov-report=term-missing` before opening or updating a pull request.

## Safety while developing

- Unit tests must never require broker credentials or place live orders.
- Keep the paper adapter isolated from the live broker integration.
- Live mode must never be restored from environment variables or persisted state on process restart.
- Any live execution feature requires dedicated tests for authorization, reconciliation, idempotency, partial fills, failure recovery, and explicit confirmation.
- Do not add backtesting/research engines to this production repository without an explicit scope change.
