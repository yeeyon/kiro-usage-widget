"""
Generate a tiny fixture state.vscdb that mirrors Kiro's real schema, so tests
and CI can run on any machine WITHOUT Kiro installed.

Run:  python tests/make_fixture.py [percent]
"""
import os
import sys
import json
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "state.vscdb")


def build(pct=15.6, limit=2000.0, reset="2026-07-01"):
    used = round(limit * pct / 100.0, 2)
    payload = {
        "kiro.resourceNotifications.usageState": {
            "usageBreakdowns": [
                {
                    "currency": {"code": "USD", "symbol": "$"},
                    "currentOverages": 0,
                    "currentUsage": used,
                    "displayName": "Credit",
                    "displayNamePlural": "Credits",
                    "percentageUsed": pct,
                    "overageCap": 10000,
                    "overageRate": 0.04,
                    "resetDate": f"{reset}T00:00:00.000Z",
                    "type": "CREDIT",
                    "unit": "INVOCATIONS",
                    "usageLimit": limit,
                }
            ],
            "timestamp": 1782727960650,
        }
    }
    os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
    if os.path.exists(FIXTURE):
        os.remove(FIXTURE)
    con = sqlite3.connect(FIXTURE)
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.execute("INSERT INTO ItemTable (key, value) VALUES (?, ?)",
                ("kiro.kiroAgent", json.dumps({"hasBeenInstalled": True, **payload})))
    con.commit()
    con.close()
    return FIXTURE


if __name__ == "__main__":
    pct = float(sys.argv[1]) if len(sys.argv) > 1 else 15.6
    path = build(pct)
    print(f"wrote {path} ({pct}%)")
