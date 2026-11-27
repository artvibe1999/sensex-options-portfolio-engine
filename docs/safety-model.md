# Safety model

- Every process start is paper mode, regardless of persisted state or environment values.
- Live mode is runtime-only and requires an allowlisted Telegram user to request and separately confirm it.
- The confirming user must match the requester. Broker authentication, reconciliation, and risk checks must pass.
- All Telegram commands require both user-ID and chat-ID allowlisting. Dangerous actions need explicit confirmation and audit records.
- Stale data, invalid option chain, stale contract master, uncertain regime, missing margin, unreconciled broker state, or risk-limit breach means no new trade.
- Emergency close is a managed workflow: halt entries, reconcile, inspect outstanding orders and all legs, protect short exposure, cancel/replace where appropriate, close in controlled sequence, then verify broker positions. Never report success until broker state confirms it.
- Partial fills, rejected orders, rate limits, disconnects, and restart recovery are first-class states.
- Secrets must be injected at runtime and must never be committed.
- Defined-risk payoff at expiry does not eliminate legging, liquidity, slippage, assignment/settlement, or overnight gap risks.
