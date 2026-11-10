# MeantIt API

A checkout guard for orders placed by AI shopping agents. PayPal holds the money while MeantIt checks that the cart is what the user meant and that the agent wasn't tricked. Good orders are captured, doubtful ones go back to the user, and bad ones are voided before any money moves.

This repo is the FastAPI backend. The web app is in the `meantit-web` repo.

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/).

> **Work in progress.** This README will grow as the project does.

## Run it locally

You need Python 3.10+.

1. Copy `.env.example` to `.env` and fill in your PayPal sandbox credentials.
2. Install and start:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements-dev.txt
   .venv/bin/uvicorn app.main:app --reload
   ```

   API docs: http://localhost:8000/docs

3. Run the tests:

   ```bash
   .venv/bin/pytest
   ```

## PayPal sandbox check

`scripts/paypal_vault_auth_test.py` checks that a saved PayPal account (Vault) works with `intent: AUTHORIZE`, which the guard depends on:

```bash
python3 scripts/paypal_vault_auth_test.py
```

Copy the vault ID it prints into `.env` as `SANDBOX_VAULT_ID`, then check that the app's own PayPal code (`app/paypal.py`) can hold, capture, refund and void with no popup:

```bash
.venv/bin/python scripts/paypal_lifecycle_check.py
```

## Signed slips

Every agent order carries four signed pieces (AP2 mandates plus the user's own words): `app/slips.py` creates them and `app/guard/verify.py` checks them (signatures, links between pieces, expiry, replays, spending limits). To see an honest order pass and five kinds of cheating get blocked:

```bash
.venv/bin/python scripts/slips_demo.py
```

The marketplace, demo agent and Check records need signing keys. Make them once:

```bash
.venv/bin/python scripts/generate_keys.py >> .env
```

## AI checks

The guard asks Claude three things: what the shopper's words require (Sonnet), whether each
cart item meets that with a quote from the product data (Sonnet), and whether hidden page text
is aimed at AI agents (Haiku). Claude only reports; code decides. If Claude fails, refuses or
times out, the order is held for the shopper. Without `ANTHROPIC_API_KEY`, every order is held.

Tests never call Claude. These two scripts do, and print what they cost:

```bash
.venv/bin/python scripts/ai_smoke.py   # demo scenes 1-3, 5 calls, ~$0.02
.venv/bin/python scripts/ai_eval.py    # 39 labelled cases, ~$0.10 (or: ai_eval.py classify)
```

## Demo shopping agent

`POST /agent/runs` takes the user's signed slips 1 and 2 and a `mode`, shops PayMart in the
background, then sends the cart through the guard. Poll `GET /agent/runs/{id}` for its steps.

| mode | agent | demo scene |
|---|---|---|
| `careful` | Claude Haiku, treats page text as information only | 1: right shoe, ALLOW |
| `naive` | a scraper with no AI, obeys notes for AI assistants | 2: poisoned page, BLOCK |
| `skim` | Claude Haiku told to buy the cheapest shoe without reading pages | 3: wrong shoe, HOLD |

Claude wouldn't follow the poisoned note even when told to, so scene 2 uses the naive scraper.
A run's picks are saved, so the same scene again costs nothing (`"fresh": true` asks again).

```bash
.venv/bin/python scripts/agent_smoke.py           # each mode once, no PayPal, ~$0.02
.venv/bin/python scripts/agent_sandbox_check.py   # all scenes end to end with the PayPal sandbox
```

## Deploy

`render.yaml` is a Render Blueprint for the API. The database runs on Neon. After the frontend is deployed, set `FRONTEND_URL` to its URL so the browser is allowed to call the API. The free server sleeps when idle, so the first request can take up to a minute.

## License

MIT
