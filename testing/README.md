# Synthetic-caller testing

Automated end-to-end tests that place **real phone calls** at this
deployment's inbound number. The caller is an AI persona ("caller bot") whose
SWML is served by the backend's test harness; after each call the harness
collects the bot's structured verdict, and the runner asserts against backend
ground truth (Call rows, `ai_context`, transcripts, contact memory).

```
testing/
  run_scenario.py                    # the orchestrator
  scenarios/fred_returning_caller.json
  results/<timestamp>-<scenario>/    # verdicts, reports, assertion output
  .state.json                        # demo-session persistence between runs
```

## One-time setup

1. `.env` (repo root):
   - `TESTING_HARNESS_ENABLED=true`
   - `TEST_CALLER_NUMBER=+1...` — a number **owned by the project** that is
     NOT the inbound demo number. It is the bot's caller ID; caller memory
     keys off it.
2. Rebuild the backend so the flag takes effect:
   `docker compose up -d --build backend`
3. ngrok running and `EXTERNAL_URL` current.

## Run

```bash
python testing/run_scenario.py testing/scenarios/fred_returning_caller.json
```

What happens:

1. Preflight (backend health, harness ping locally and through the tunnel).
2. The previous run's demo workspace is released, a fresh one provisioned
   via `POST /api/demo/start` — the run exercises real demo onboarding.
3. The test number is paired via the real SMS pairing-code flow; if carrier
   filtering (10DLC) blocks the SMS, the runner injects the same MO payload
   at the signed `sms-inbound` webhook instead.
4. Each mission dials the demo line via the Calling API with the bot
   persona; the runner polls the bot's verdict, then the call-report until
   the awaited artifacts (summary / memory digest / transcript) exist.
5. Assertions print as PASS / FAIL / soft-FAIL; full artifacts land in
   `results/`. Exit code is non-zero on hard failures.

By default the demo workspace is **kept** after a run so you can inspect the
calls in the dashboard; the next run releases it. Pass `--release` to clean
up immediately.

## One run at a time

Only ONE scenario run may be active against the stack at a time, and no
container restarts while one is in flight. The test number's demo-workspace
binding is exclusive — a concurrent run (or another session's verification)
re-pairs the number and later calls land in the wrong workspace; a mid-run
`docker compose up`/restart of backend or ai-agents breaks whichever call
is on the line. The runner heartbeats its workspace to hold the binding,
but that does not protect against restarts.

## Costs & limits

- Each mission is a real call (per-minute billing) plus one SMS.
- The hosted-demo inbound ratelimit is 10 calls/hour per caller number —
  at 2 calls per Fred run that caps ~5 runs/hour.
- Keep `DEBUG_WEBHOOK_ENABLED=false` during suite runs unless you are
  debugging a specific failure; level 2 multiplies tunnel traffic.

## Writing scenarios

A scenario is JSON: `missions[]` (persona + post_prompt + await gates) and
`assertions[]` (`target` dotted path into `verdictN` / `reportN` /
`reportN.derived`, `op` in `eq | truthy | non_empty | contains |
contains_any`, `level` `hard` | `soft`). See the Fred scenario for the
shape. The bot always has one tool: `end_call` — persona text must tell it
when to use it, or the leg idles until `inactivity_timeout`.
