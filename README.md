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

## Dashboard and evidence

- `GET /dashboard/orders`: every agent order PayMart received, with the reasons and which demo agent placed it.
- `GET /orders/{id}/evidence`: the four signed pieces as received, the signed Check record and the shopper's public key.
- `GET /keys`: PayMart's and MeantIt's public keys. The dashboard's **Verify signatures** button re-checks
  every signature and hash in the browser with these, so nobody has to trust our backend's word.

## Protection Fund and claims

The second layer. If an order MeantIt approved turns out to be the agent's mistake, the shopper sends it
back, PayMart refunds them, and the Protection Fund repays PayMart's costs with **PayPal Payouts**.

1. `POST /orders/{id}/claims` (shopper): the problem goes to **PayMart first**, like any return. No AI runs.
2. `POST /claims/{id}/merchant` (PayMart) with `action`:
   - `accept_return`: a normal return under PayMart's own policy. PayMart refunds and carries the costs.
   - `decline`: PayMart says no. PayPal's buyer protection still applies.
   - `send_to_fund`: PayMart believes the agent got it wrong and hands the claim over. The request returns at
     once with `SENT_TO_FUND`; MeantIt checks it afterwards on its own side, code first: only
     orders MeantIt approved, with a signed request; a HOLD the shopper approved themselves isn't covered; repeat
     claimers go to a person. Then the claim judge (Claude Sonnet) compares the claim with the signed request,
     the Check record and anything published after checkout, and returns `AGENT_ERROR`, `CHANGED_MIND`,
     `NOT_RECEIVED` or `SUSPICIOUS` with a confidence. Below 0.7, suspicious, or no AI goes to the admin's
     review queue; `NOT_RECEIVED` belongs to PayPal Seller Protection, so the fund doesn't pay twice.
     If the fund says no, PayMart can still accept or decline under its own policy. PayMart and the shopper
     see only the outcome; the judge's reasoning shows in the admin console.
3. `POST /claims/{id}/return-received` (PayMart): refunds the capture in full. For a fund claim it also reads
   the PayPal fee from the capture and pays PayMart's losses (return label, the fee, the item if it can't be
   resold) from the fund, never more than the order total or the fund's balance.
4. `POST /claims/{id}/review` (admin) decides queued claims, or a `SENT_TO_FUND` claim the judge never
   answered (a judge that answers late doesn't overwrite a person). `GET /fund` shows the balance and ledger:
   an opening sandbox reserve plus 1% of every captured order, minus reimbursements.

The AI never moves money: it returns a verdict, and code checks the rules and calls PayPal.

## Deploy

`render.yaml` is a Render Blueprint for the API. The database runs on Neon. After the frontend is deployed, set `FRONTEND_URL` to its URL so the browser is allowed to call the API. The free server sleeps when idle, so the first request can take up to a minute.

## License

MIT
