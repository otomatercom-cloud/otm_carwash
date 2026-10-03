# odoo shell < notify_flow.py   (HTTP to providers is mocked; nothing leaves the machine)
import requests
from unittest import mock
api = env["otm.cw.api"]
ICP = env["ir.config_parameter"].sudo()
calls = []
class R:
    def __init__(s, code=200, body=None): s.status_code, s._b = code, body or {}
    def json(s): return s._b
fail = {"whatsapp": False}
def fake(method, url, **kw):
    calls.append((url.split("/")[2], url, kw))
    if "graph.facebook.com" in url and fail["whatsapp"]:
        return R(500, {"error": {"message": "temporary"}})
    if "getMe" in url: return R(200, {"result": {"username": "TestWashBot"}})
    return R(200, {"ok": True})
with mock.patch.object(requests, "request", fake):
    # nothing configured -> skipped, no crash
    p = env["res.partner"].create({"name": "Notif Cust", "otm_cw_is_customer": True, "otm_cw_mobile": "9876543210", "email": "n@example.com"})
    veh = env["otm.cw.vehicle"].create({"reg_no": "KL07NT0001", "vehicle_type_id": env["otm.cw.vehicle.type"].search([], limit=1).id, "partner_id": p.id})
    svc = env["otm.cw.service"].search([], limit=1)
    price_ok = env["otm.cw.pricing"].search_count([("service_id", "=", svc.id), ("vehicle_type_id", "=", veh.vehicle_type_id.id)])
    if not price_ok:
        env["otm.cw.pricing"].create({"service_id": svc.id, "vehicle_type_id": veh.vehicle_type_id.id, "customer_type": "standard", "price": 100})
    api.notify_settings_set(values={"shop_name": "Shine Wash"})
    job = env["otm.cw.job"].create_job(p, veh, svc)
    env["otm.cw.job"].dispatch()
    rows = env["otm.cw.notification"].search([("job_id", "=", job.id)])
    assert rows and rows[0].state == "skipped", rows.mapped("state")
    print("unconfigured -> skipped OK")
    # configure whatsapp + telegram
    cfg = api.notify_settings_set(values={"wa_enabled": "1", "wa_phone_id": "123", "wa_token": "SECRET-WA", "tg_enabled": "1", "tg_token": "12345:ABC-secret", "wa_country": "91"})
    assert cfg["wa_token_set"] and "SECRET" not in str(cfg) and "ABC-secret" not in str(cfg), "secret leaked"
    assert cfg["tg_bot"] == "TestWashBot" and set(cfg["active_channels"]) == {"whatsapp", "telegram"}
    print("settings masked OK; channels", cfg["active_channels"])
    job2 = env["otm.cw.job"].create_job(p, env["otm.cw.vehicle"].create({"reg_no": "KL07NT0002", "vehicle_type_id": veh.vehicle_type_id.id, "partner_id": p.id}), svc)
    env["otm.cw.job"].dispatch()
    r2 = env["otm.cw.notification"].search([("job_id", "=", job2.id), ("event", "=", "vehicle_assigned")])
    assert len(r2) == 1 and r2.channel == "whatsapp" and r2.recipient == "919876543210" and "Shine Wash" in r2.message and "Bay" in r2.message, (r2.read(), )
    print("assigned message:", r2.message)
    api.notify_flush()
    assert r2.state == "sent", (r2.state, r2.error)
    wa = [c for c in calls if "graph.facebook.com" in c[1]][-1]
    assert wa[2]["json"]["to"] == "919876543210" and wa[2]["headers"]["Authorization"] == "Bearer SECRET-WA"
    print("whatsapp sent OK")
    # whatsapp down -> retries then falls back to telegram once linked
    p.otm_cw_telegram_chat_id = "555"
    fail["whatsapp"] = True
    job2.action_start([env["hr.employee"].sudo().search([("otm_cw_is_staff", "=", True)], limit=1).id or False]) if False else None
    n = env["otm.cw.notification"].notify("vehicle_ready", p, "", job=job2, force=True)
    for _ in range(3): api.notify_flush()
    assert n.state == "failed" and n.attempts == 3, (n.state, n.attempts)
    tg = env["otm.cw.notification"].search([("job_id", "=", job2.id), ("channel", "=", "telegram")])
    assert len(tg) == 1, tg.read()
    api.notify_flush()
    assert tg.state == "sent" and "ABC-secret" not in (n.error or ""), (tg.state, n.error)
    print("retry -> fallback to telegram OK; error text:", n.error)
    # opt-out and per-channel preference
    p.otm_cw_notify_opt_in = False
    assert not env["otm.cw.notification"].notify("vehicle_ready", p, "", job=job2)
    p.write({"otm_cw_notify_opt_in": True, "otm_cw_notify_channel": "all"})
    fail["whatsapp"] = False
    both = env["otm.cw.notification"].notify("vehicle_ready", p, "", job=job2)
    assert set(both.mapped("channel")) == {"whatsapp", "telegram"}
    print("opt-out + 'all' channels OK")
    # event toggle
    assert not env["otm.cw.notification"].notify("wash_started", p, "", job=job2), "wash_started default is off"
    api.notify_settings_set(values={"events": {"wash_started": {"enabled": True, "template": "Washing {vehicle} now, {customer}"}}})
    ws = env["otm.cw.notification"].notify("wash_started", p, "", job=job2)
    assert ws and "Washing KL07NT0002 now" in ws[0].message
    print("event toggle + template OK")
    # telegram linking
    p.otm_cw_telegram_chat_id = False
    lc = api.telegram_link_code(partner_id=p.id)
    assert lc["link"].startswith("https://t.me/TestWashBot?start=")
    assert api.telegram_link(code=lc["code"], chat_id=777)["ok"] and p.otm_cw_telegram_chat_id == "777"
    assert not api.telegram_link(code=lc["code"], chat_id=888)["ok"], "code is single-use"
    print("telegram link OK")
env.cr.rollback()
print("ALL NOTIFY CHECKS PASSED")
