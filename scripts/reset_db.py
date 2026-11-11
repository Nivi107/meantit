"""Empty the database for a clean demo, keeping the agents' saved runs.

Deletes every order, check, claim, fund ledger line, agent run and shopper.
Keeps agent_plans, so the demo scenes still replay without calling Claude.
The fund starts again from its opening reserve the next time it's read.
PayPal's sandbox history isn't touched.

    .venv/bin/python scripts/reset_db.py          # asks first
    .venv/bin/python scripts/reset_db.py --yes
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from app import db  # noqa: E402

# Children before parents, so no foreign key is left pointing at a deleted row.
TABLES = [db.used_nonces, db.webhook_events, db.fund_ledger, db.claims, db.agent_runs,
          db.checks, db.mandates, db.orders, db.users]


def main():
    db.init()
    with db.engine.connect() as conn:
        counts = {t.name: conn.execute(select(func.count()).select_from(t)).scalar() for t in TABLES}
        waiting = conn.execute(select(func.count()).select_from(db.orders)
                               .where(db.orders.c.payment_status == "AUTHORIZED")).scalar()
        plans = conn.execute(select(func.count()).select_from(db.agent_plans)).scalar()

    print("Rows to delete:", ", ".join(f"{name} {n}" for name, n in counts.items()))
    print(f"Kept: agent_plans {plans} (saved agent runs, so replays stay free)")
    if waiting:
        print(f"Warning: {waiting} order(s) still hold money at PayPal; those holds stay open until they expire.")
    if "--yes" not in sys.argv and input('Type "yes" to delete: ').strip() != "yes":
        print("Nothing deleted.")
        return

    with db.engine.begin() as conn:
        for table in TABLES:
            conn.execute(delete(table))
    print("Done. The database is empty except for saved agent runs.")


if __name__ == "__main__":
    main()
