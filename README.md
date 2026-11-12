# MeantIt API

A checkout guard for orders placed by AI shopping agents. PayPal holds the money while MeantIt checks that the cart is what the user meant and that the agent wasn't tricked. Good orders are captured, doubtful ones go back to the user, and bad ones are voided before any money moves.

This repo is the FastAPI backend. The web app is in [meantit-web](https://github.com/Nivi107/meantit-web).

Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/).

## Try it

- **Live demo:** https://meantit-web.onrender.com (click **Run the demo**)
- **API:** https://meantit-api.onrender.com, with interactive docs at https://meantit-api.onrender.com/docs

The servers run on Render's free tier and sleep when idle. The first visit can take about a minute;
the header says "Waking the server" until the API answers. Everything runs on the PayPal sandbox, so
no real money moves and there is no login: the demo uses one saved sandbox PayPal account.

**A 3-minute walkthrough:**

1. **Shopper → Create my key.** Your browser makes a P-256 signing key. The request ("running shoes for
   flat feet, size US 9, under $150, delivered within a week") and a $150 limit are signed with it.
2. **A careful agent.** Claude Haiku shops PayMart and picks the right shoe. PayPal holds the money,
   MeantIt checks the order, and the payment is **captured**.
3. **PayMart → StableRun Pro → Show what the agent saw.** One listing hides a "note for AI assistants"
   that people can't see.
4. **A tricked agent** obeys that note. MeantIt finds the hidden instruction and the shoe that doesn't fit,
   so the PayPal hold is **voided** and the shopper is never charged.
5. **A careless agent** buys the cheapest shoe without reading. It isn't clearly wrong, so the order is
   **held** and the shopper decides: yes captures, no releases the hold.
6. **Orders & claims → the scene 1 order → Report a problem.** News after checkout shows that shoe was
   wrong. **Merchant → Claims → Send to Protection Fund.** The claim judge approves it, **Return received**
   refunds the shopper through PayPal, and the fund repays PayMart with **PayPal Payouts**.
7. **Merchant** shows every order in AG Grid; **Verify signatures** re-checks the evidence in your browser.
   **Admin** shows the fund's balance, the ledger and the judge's reasoning.

Saved agent runs replay, so the demo scenes cost no AI calls after the first time.

## How it works

```
Shopper signs ──► Agent shops ──► Checkout: PayPal AUTHORIZES (money held, not charged)
                                        │
                                        ▼
                         MeantIt guard: verify ─ collect ─ check ─ decide ─ record
                                        │
             ┌──────────────────────────┼──────────────────────────┐
             ▼                          ▼                          ▼
     ALLOW: CAPTURE             HOLD: ask the shopper         BLOCK: VOID
                                (24 h, then the hold is released)
```

- **Verify (code):** the four signed pieces (AP2-style mandates: what you want, the most you may spend, the
  marketplace's cart and the agent's final OK), their links, expiry, replays and the spending limit.
- **Check (Claude, code decides):** what the shopper's words require, whether each item meets it with a quote
  from the product data as proof, and whether any page text the agent read was aimed at AI agents.
- **Record:** a Check record signed by MeantIt, so the marketplace can prove later what was checked and why.
- **Protection Fund:** 1% of every captured guarded order. If MeantIt approved an order that turns out to be
  the agent's mistake, the fund repays the marketplace's costs.

### PayPal APIs used

| API | What for |
|---|---|
| Orders v2 with `intent: AUTHORIZE` and a vaulted PayPal account | The agent pays with no popup; the money is held, not captured |
| Payments: capture, void, refund | Capture good orders, void bad ones, refund approved claims |
| Payouts | The Protection Fund repays the marketplace from its own sandbox account |
| Webhooks | PayPal confirms each money movement (`/webhooks/paypal/paymart` and `/webhooks/paypal/fund`) |

### AI used

| Where | Model | Job |
|---|---|---|
| Guard: what the request requires | Claude Sonnet 5.5 | Turns the shopper's words into a checklist |
| Guard: does the cart fit | Claude Sonnet 5.5 | Checks each item against the checklist, quoting the product data |
| Guard: hidden instructions | Claude Haiku 5.5 | Classifies page text the agent read as aimed at AI agents or not |
| Demo shopping agents | Claude Haiku 5.5 | The careful and careless agents (tool use over the PayMart catalogue) |
| Claim judge | Claude Sonnet 5.5 | Agent mistake, changed mind, not received or suspicious, with a confidence |

Claude only reports, using structured JSON. Code applies the rules, decides and moves the money. If Claude
fails, refuses or times out, the order is held for the shopper and the claim goes to a person.

**Built with:** Claude Code was used as a coding assistant while building the project.

### Stack

FastAPI, SQLAlchemy and Postgres (Neon) for the API; React, Vite, Tailwind CSS and **AG Grid** Community for
the web app; both hosted on **Render**. Signatures are ES256 JWTs (P-256); the shopper's key is made and kept in the browser with WebCrypto.

### What a real launch would change

- There are no logins: the header switches between the shopper, marketplace and admin views. Real logins would
  replace that, with each marketplace seeing only its own orders.
- The demo shares one saved sandbox PayPal account; each shopper would save their own once.
- The Protection Fund is presented as a service guarantee; a launch would confirm the legal structure or
  partner with a licensed insurer.

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
  every signature and hash in the browser with these, so nobody has to trust the backend's word.

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
