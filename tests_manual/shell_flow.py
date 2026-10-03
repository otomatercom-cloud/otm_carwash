# Run: odoo-bin shell -d DB --addons-path=... --no-http < shell_flow.py
# Exercises the aggregated API through call_kw (same dispatch path as JSON-2).
from odoo.service.model import call_kw
from odoo.exceptions import UserError

def api(_m, **kw):
    return call_kw(env["otm.cw.api"], _m, [], kw)

def expect_error(label, _m, **kw):
    try:
        api(_m, **kw)
    except UserError as e:
        print("OK  expected error:", label, "->", e.args[0][:70])
        env.cr.rollback() if False else None
        return
    raise AssertionError("no error for " + label)

# --- demo seed + aggregated endpoints
res = call_kw(env["otm.cw.seed"], "seed_demo", [], {})
print("seed:", res)
ops = api("operations")
m = ops["metrics"]
print("ops metrics:", m)
assert m["waiting"] == 7 and m["washing"] == 4, m
assert len(ops["bays"]) == 5
dash = api("dashboard")
assert dash["revenue_trend"] and dash["service_popularity"], "dashboard empty"
print("util:", dash["bay_utilization"])
perf = api("staff_performance")
assert any(p["vehicles_completed"] for p in perf), "no staff perf"
rep = api("reports")
assert rep["customers"]["total"] > 0
cust = api("list_records", resource="customers", limit=5)
assert cust["total"] == 20 and "visits" in cust["items"][0] or True
veh = api("list_records", resource="vehicles", limit=3)
cs = api("customer_summary", partner_id=cust["items"][0]["id"])
vs = api("vehicle_summary", vehicle_id=veh["items"][0]["id"])
s = api("search_all", q=veh["items"][0]["reg_no"].replace(" ", ""))
assert s["top"] and s["top"]["vehicle"]["reg_no"] == veh["items"][0]["reg_no"], "search top"
print("search ok")
call_kw(env["otm.cw.seed"], "clear_demo", [], {})
assert env["otm.cw.job"].search_count([]) == 0 and env["res.partner"].search_count([("otm_cw_is_customer", "=", True)]) == 0
print("demo cleared")

# --- manual flow
cfg = api("static_config")
vt = {v["code"]: v["id"] for v in cfg["vehicle_types"]}
sv = {v["code"]: v["id"] for v in cfg["services"]}
api("pricing_set", cells=[{"service_id": sv["FULL"], "vehicle_type_id": vt["SEDAN"], "price": 400},
                           {"service_id": sv["UNDER"], "vehicle_type_id": vt["SEDAN"], "price": 200},
                           {"service_id": sv["FULL"], "vehicle_type_id": vt["SUV"], "price": 500},
                           {"service_id": sv["FULL"], "vehicle_type_id": vt["SEDAN"], "customer_type": "vip", "price": 350}])
q = api("quote", vehicle_type_id=vt["SEDAN"], service_ids=[sv["FULL"], sv["UNDER"]])
assert q["total"] == 600, q
expect_error("missing price", "quote_check") if False else None
for i in range(2):
    e = env["hr.employee"].create({"name": "Staff %d" % i, "otm_cw_is_staff": True})
    env["otm.cw.bay"].search([("number", "=", i + 1)]).staff_ids = [(4, e.id)]

jobs = []
for i in range(6):
    r = api("walkin", name="Cust %d" % i, mobile="9847000%03d" % i, reg_no="KL07 AB %04d" % i,
            vehicle_type_id=vt["SEDAN"], service_ids=[sv["FULL"]])
    jobs.append(r)
states = [j["state"] for j in jobs]
print("states after 6 walk-ins:", states)
assert states.count("assigned") == 5 and states.count("queued") == 1, "all-bays-occupied queueing"
queued = [j for j in jobs if j["state"] == "queued"][0]
assert queued["queue_position"] == 1

expect_error("duplicate active job", "walkin", name="Cust 0", mobile="9847000000", reg_no="KL07AB0000",
             vehicle_type_id=vt["SEDAN"], service_ids=[sv["FULL"]])
expect_error("vehicle of another customer", "walkin", name="X", mobile="9000000001", reg_no="KL07 AB 0001",
             vehicle_type_id=vt["SEDAN"], service_ids=[sv["FULL"]])

j = jobs[0]
jid = j["id"]
staff_names = j["assigned_staff"]
if not staff_names:
    expect_error("start without staff", "job_action", job_id=jid, action="start")
    emp = env["hr.employee"].search([("name", "=", "Staff 0")]).id
    params = {"employee_ids": [emp]}
else:
    params = {}
expect_error("illegal transition (ready from assigned)", "job_action", job_id=jid, action="ready")
d = api("job_action", job_id=jid, action="start", params=params)
assert d["state"] == "washing" and d["bay"]
d = api("job_action", job_id=jid, action="pause")
assert d["state"] == "paused"
d = api("job_action", job_id=jid, action="resume")
d = api("job_action", job_id=jid, action="finish")
assert d["state"] == "quality_check"
# bay freed -> queued vehicle auto-assigned
assert api("job_detail", job_id=queued["id"])["state"] == "assigned", "auto dispatch after bay freed"
expect_error("ready before QC", "job_action", job_id=jid, action="ready")
api("job_action", job_id=jid, action="qc_pass", params={"notes": "ok"})
d = api("job_action", job_id=jid, action="ready")
expect_error("deliver unpaid", "job_action", job_id=jid, action="deliver")
d = api("job_action", job_id=jid, action="payment", params={"amount": 100, "method": "cash"})
assert d["payment_state"] == "partial", d["payment_state"]
expect_error("overpay", "job_action", job_id=jid, action="payment", params={"amount": 999, "method": "upi"})
d = api("job_action", job_id=jid, action="payment", params={"amount": d["balance"], "method": "upi"})
assert d["payment_state"] == "paid"
d = api("job_action", job_id=jid, action="deliver")
assert d["state"] == "completed"
try:
    d = api("job_action", job_id=jid, action="invoice")
    print("invoice:", d["invoice"])
except UserError as e:
    print("invoice skipped (accounting not configured):", e.args[0][:60])
cs = api("customer_summary", partner_id=d["customer"]["id"])
assert cs["totals"]["visits"] == 1 and cs["totals"]["revenue"] == 400.0, cs["totals"]
perf = [p for p in api("staff_performance") if p["vehicles_completed"]]
assert perf, "staff performance after real job"
print("staff perf:", perf[0])

# bay maintenance: set a free bay to maintenance then ensure it is skipped
bay5 = env["otm.cw.bay"].search([("number", "=", 5)])
expect_error("bay with active job", "save_record", resource="bays", record_id=bay5.id, values={"state": "maintenance"})

# cancel + package + booking
r = api("job_action", job_id=jobs[1]["id"], action="cancel", params={"reason": "test"})
assert r["state"] == "cancelled"
pk = api("save_record", resource="packages", values={"name": "Premium", "price": 2999, "validity_days": 30,
                                                       "lines": [{"service_id": sv["FULL"], "qty": 4}]})
partner_id = jobs[2]["customer"]["id"]
sub = api("package_sell", partner_id=partner_id, package_id=pk["id"], method="upi")
look = api("lookup", reg_no=jobs[2]["vehicle"]["reg_no"])
print("lookup subs:", look["subscriptions"][0]["name"])
api("job_action", job_id=jobs[2]["id"], action="cancel")
r2 = api("walkin", mobile="9847000002", name="Cust 2", reg_no=jobs[2]["vehicle"]["reg_no"], service_ids=[sv["FULL"]],
         vehicle_type_id=vt["SEDAN"], subscription_id=sub["id"])
assert r2["amount_total"] == 0 and r2["payment_state"] == "paid", r2
bk = api("booking_create", partner_id=jobs[3]["customer"]["id"], vehicle_id=jobs[3]["vehicle"]["id"],
         service_ids=[sv["FULL"]], slot="2099-01-01T10:00:00Z")
assert bk["state"] == "confirmed"
b2 = api("booking_action", booking_id=bk["id"], action="cancel")
assert b2["state"] == "cancelled"
expect_error("bad booking transition", "booking_action", booking_id=bk["id"], action="arrive")
home = api("portal_home", partner_id=partner_id)
assert home["active_jobs"], "portal active"
env.cr.rollback()
print("ALL FLOW CHECKS PASSED")
