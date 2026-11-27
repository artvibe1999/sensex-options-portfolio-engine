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


## Telegram command transport

Install the optional transport with `python -m pip install -e '.[telegram]'`.
Set `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS`, and
`TELEGRAM_ALLOWED_CHAT_IDS` in the runtime environment. The two allowlists are
comma-separated numeric IDs; an empty allowlist authorizes nobody.

The transport can be created with `create_telegram_application` from
`sensex_engine.telegram_transport` and started by the process supervisor using
the returned application's `run_polling()` lifecycle. It persists an idempotent
command receipt before dispatch and records the outcome afterward. If audit
storage is unavailable, the command is not executed; if audit finalization
fails, runtime mode is forced to paper and entries are paused.

Pass a readiness provider that returns `(broker_ready, risk_clear)` only after
real broker reconciliation and current portfolio risk checks. Without that
provider, `/resume` and `/confirm_live` fail closed. The current repository
does not yet provide those production readiness services, portfolio query
handlers, live order execution, or managed position closure. The Telegram
transport must therefore remain paper-only in deployment until those services
are implemented and separately reviewed.
