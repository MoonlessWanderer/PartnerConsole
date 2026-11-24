import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import jdatetime

base = "http://127.0.0.1:8741/api/"
def call(endpoint, body=None, token=None):
    headers = {"Content-Type":"application/json"}
    if token:
        headers["Authorization"] = "Bearer "+token
    req = urllib.request.Request(base+endpoint, data=None if body is None else json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)

password = Path(sys.argv[2]).read_text().strip()
admin = call("admin/auth/login", {"username":"admin","password":password})["access_token"]

def manage(endpoint, body):
    return call("admin/"+endpoint, body, admin)

if sys.argv[1] == "seed":
    panel = manage("panels", {"name":"Migration fixture","base_url":"http://127.0.0.1:18788",
        "secure_path":"labsecure","admin_email":"admin@panel.test","admin_password":"secret"})
    product = manage("products", {"panel_id":panel["id"],"plan_id":7,"name":"Migration plan",
        "email_domain":"migration.example.test","price_per_gb":1000,"price_per_day":100})["product"]
    partner = manage("partners", {"username":"migration_partner","password":password})
    manage(f"partners/{partner['id']}/adjust", {"asset":"toman","amount":100000})

token = call("partner/auth/login",{"username":"migration_partner","password":password})["access_token"]
def mine(endpoint, body=None):
    return call("partner/"+endpoint,body,token)

if sys.argv[1] == "seed":
    date = jdatetime.date.fromgregorian(date=datetime.now(ZoneInfo("Asia/Tehran")).date()+timedelta(days=31))
    order = {"product_id":product["id"],"username":"retained_subscriber","traffic_cap_gb":2,
             "end_date":f"{date.year:04d}/{date.month:02d}/{date.day:02d}"}
    quote = mine("subscriptions/quote", {**order,"action":"create"})
    assert quote["toman"] == 5100
    mine("subscriptions", {**order,"consent_token":quote["consent_token"]})

sub, = mine("subscriptions")["items"]
ledger = mine("ledger")["items"]
identity = mine("me")
wallet = identity["balance_toman"]
assert wallet == 94900 and len(ledger) == 2
assert sub["username"] == "retained_subscriber" and sub["v2_user_id"]
live = mine(f"subscriptions/{sub['id']}")
assert live["subscribe_url"] and not live.get("live_error")
print(json.dumps({"balance_toman":wallet,
    "admin":call("admin/me", token=admin),
    "partner":[identity[k] for k in ("id","username")],
    "ledger":[[row[k] for k in ("id","amount","asset","reason","subscription_id")] for row in ledger],
    "subscription":[sub[k] for k in ("id","username","product_id","v2_user_id","traffic_cap_gb","expired_at")]},sort_keys=True))
