# -*- coding: utf-8 -*-
"""Aggregated server-side API used by the Next.js server through Odoo's JSON-2 endpoint
(POST /json/2/otm.cw.api/<method>). Every public method is @api.model and takes kwargs only.
Each call returns everything one screen needs, so Next.js never chains requests."""
import secrets
from datetime import date, datetime, time, timedelta

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .job import ACTIVE_STATES, PARAM_DEFAULTS, fmt_local, get_param, local_today, local_tz
from .misc import EVENTS
from .notify import CHANNELS, EVENT_DEFAULTS, _digits
from .partner import mobile_key

RESOURCES = {
    "vehicle_types": dict(model="otm.cw.vehicle.type", order="sequence, name", search=["name", "code"],
                          fields=["name", "code", "description", "sequence", "active"]),
    "services": dict(model="otm.cw.service", order="sequence, name", search=["name", "code"],
                     fields=["name", "code", "description", "duration", "sequence", "active"]),
    "vehicles": dict(model="otm.cw.vehicle", order="reg_no", search=["reg_no", "reg_key", "brand", "model"],
                     fields=["reg_no", "vehicle_type_id", "brand", "model", "color", "partner_id", "notes", "active"],
                     scope="partner_id"),
    "customers": dict(model="res.partner", order="name", search=["name", "otm_cw_mobile", "email"],
                      domain=[("otm_cw_is_customer", "=", True)], defaults={"otm_cw_is_customer": True},
                      fields=["name", "otm_cw_mobile", "otm_cw_whatsapp", "email", "street",
                              "otm_cw_customer_type", "otm_cw_discount", "active"]),
    "bays": dict(model="otm.cw.bay", order="number", search=["name"],
                 fields=["name", "number", "state", "vehicle_type_ids", "service_ids", "staff_ids", "active"]),
    "staff": dict(model="hr.employee", order="name", search=["name", "otm_cw_mobile"],
                  domain=[("otm_cw_is_staff", "=", True)], defaults={"otm_cw_is_staff": True},
                  fields=["name", "otm_cw_mobile", "otm_cw_role", "otm_cw_shift", "otm_cw_available",
                          "otm_cw_service_ids", "active"]),
    "packages": dict(model="otm.cw.package", order="name", search=["name"],
                     fields=["name", "price", "validity_days", "active"]),
    "bookings": dict(model="otm.cw.booking", order="slot_dt desc, id desc", search=["name"], readonly=True,
                     fields=["name", "partner_id", "vehicle_id", "slot_dt", "state", "amount_total", "source"],
                     scope="partner_id", date_field="slot_dt"),
    "jobs": dict(model="otm.cw.job", order="id desc", search=["name"], readonly=True,
                 fields=["name", "token", "partner_id", "vehicle_id", "vehicle_type_id", "state", "priority",
                         "bay_id", "arrival_dt", "start_dt", "end_dt", "work_seconds", "wait_minutes",
                         "estimated_minutes", "amount_total", "paid_amount", "balance", "payment_state"],
                 scope="partner_id", date_field="arrival_dt"),
    "payments": dict(model="otm.cw.payment", order="dt desc, id desc", search=["reference", "job_id.name"], readonly=True,
                     fields=["job_id", "partner_id", "amount", "method", "kind", "reference", "dt", "received_by"],
                     scope="partner_id", date_field="dt"),
    "subscriptions": dict(model="otm.cw.subscription", order="id desc", search=["name"], readonly=True,
                          fields=["name", "partner_id", "package_id", "start_date", "expiry_date", "amount"],
                          scope="partner_id"),
    "notifications": dict(model="otm.cw.notification", order="id desc", search=["message", "recipient", "partner_id.name"],
                          readonly=True, date_field="create_date",
                          fields=["event", "partner_id", "job_id", "channel", "recipient", "message", "state", "error",
                                  "attempts", "sent_dt", "create_date"]),
    "audit": dict(model="otm.cw.audit", order="id desc", search=["ref", "action", "actor"], readonly=True,
                  fields=["ref", "res_model", "res_id", "action", "detail", "actor", "dt"], date_field="dt"),
}
PAGE_MAX = 100
NOTIFY_P = "otm_cw.notify."
NOTIFY_PLAIN = ("wa_enabled", "wa_phone_id", "wa_lang", "tg_enabled", "tg_bot", "sms_enabled", "sms_sid", "sms_from",
                "email_enabled", "shop_name")
NOTIFY_SECRET = ("wa_token", "tg_token", "sms_token")


def _np(env, key, default=""):
    v = env["ir.config_parameter"].sudo().get_param(NOTIFY_P + key)
    return v if v not in (None, False) else default



def ser(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _dn(rec):
    """display_name; employees are read with sudo because non-HR users only get hr.employee.public."""
    return (rec.sudo() if rec._name == "hr.employee" else rec).display_name


def rec_to_dict(rec, names):
    out = {"id": rec.id}
    for name in names:
        field = rec._fields[name]
        val = rec[name]
        if field.type == "many2one":
            out[name] = {"id": val.id, "name": _dn(val)} if val else None
        elif field.type in ("many2many", "one2many"):
            out[name] = [{"id": r.id, "name": _dn(r)} for r in val]
        elif field.type == "float":
            out[name] = round(val or 0.0, 2)
        else:
            out[name] = ser(val)
    return out


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return False


def to_ids(values):
    return [i for i in (to_int(v) for v in (values or [])) if i]


class OtmCwApi(models.AbstractModel):
    _name = "otm.cw.api"
    _description = "Car Wash Aggregated API"

    # ------------------------------------------------------------ helpers
    def _bounds(self, date_from=None, date_to=None):
        """Local-date range -> (start_utc, end_utc, [local dates]). Default: today."""
        tz = local_tz(self.env)
        today = local_today(self.env)
        d1 = fields.Date.to_date(date_from) if date_from else today
        d2 = fields.Date.to_date(date_to) if date_to else d1
        if d2 < d1:
            d1, d2 = d2, d1
        d2 = min(d2, d1 + timedelta(days=366))

        def utc(d):
            return tz.localize(datetime.combine(d, time.min)).astimezone(pytz.utc).replace(tzinfo=None)

        days = [d1 + timedelta(days=i) for i in range((d2 - d1).days + 1)]
        return utc(d1), utc(d2 + timedelta(days=1)), days

    def _sql_day(self, col):
        return "(%s AT TIME ZONE 'UTC' AT TIME ZONE %%(tz)s)::date" % col

    def _sql(self, query, params):
        self.env.flush_all()
        params = dict(params, tz=get_param(self.env, "tz"))
        self.env.cr.execute(query, params)
        return self.env.cr.dictfetchall()

    def _operating_seconds(self, days, start, end):
        tz = local_tz(self.env)
        now = fields.Datetime.now()
        open_h, close_h = int(get_param(self.env, "open_hour")), int(get_param(self.env, "close_hour"))
        total = 0.0
        for d in days:
            o = tz.localize(datetime.combine(d, time(open_h))).astimezone(pytz.utc).replace(tzinfo=None)
            c = tz.localize(datetime.combine(d, time(min(close_h, 23), 59 if close_h >= 24 else 0))).astimezone(
                pytz.utc).replace(tzinfo=None)
            lo, hi = max(o, start), min(c, end, now)
            if hi > lo:
                total += (hi - lo).total_seconds()
        return total

    # --------------------------------------------------------- serializers
    def _job_card(self, job, now, queue_pos=None, eligible=None):
        cust = job.partner_id
        live_wait = (now - job.arrival_dt).total_seconds() / 60.0 if job.state == "queued" else job.wait_minutes
        return {
            "id": job.id, "name": job.name, "token": job.token, "state": job.state, "priority": job.priority,
            "vehicle": {"id": job.vehicle_id.id, "reg_no": job.vehicle_id.reg_no,
                        "type": job.vehicle_type_id.name, "brand": job.vehicle_id.brand or "",
                        "model": job.vehicle_id.model or "", "color": job.vehicle_id.color or ""},
            "customer": {"id": cust.id, "name": cust.name, "mobile": cust.otm_cw_mobile or ""},
            "services": [l.service_id.name for l in job.line_ids],
            "estimated_minutes": job.estimated_minutes,
            "arrival_dt": ser(job.arrival_dt), "assigned_dt": ser(job.assigned_dt), "start_dt": ser(job.start_dt),
            "end_dt": ser(job.end_dt), "work_seconds": job.work_seconds, "last_resume_dt": ser(job.last_resume_dt),
            "wait_minutes": round(live_wait or 0.0, 1),
            "bay": {"id": job.bay_id.id, "name": job.bay_id.name} if job.bay_id else None,
            "staff": job.sudo().actual_staff_ids.mapped("name") or job.sudo().assigned_staff_ids.mapped("name"),
            "assigned_staff": [{"id": e.id, "name": e.name} for e in job.sudo().assigned_staff_ids],
            "actual_staff": [{"id": e.id, "name": e.name} for e in job.sudo().actual_staff_ids],
            "assign_reason": job.assign_reason or "", "qc_passed": job.qc_passed,
            "amount_total": job.amount_total, "paid_amount": job.paid_amount, "balance": job.balance,
            "payment_state": job.payment_state, "queue_position": queue_pos, "eligible_bays": eligible or [],
        }

    # -------------------------------------------------------------- config
    def _env(self, model):
        """Model handle. hr.employee goes through sudo: Odoo 19 redirects non-HR users to hr.employee.public
        (no custom fields). Access control happens in the Next.js role layer + whitelisted RESOURCES."""
        return self.env[model].sudo() if model == "hr.employee" else self.env[model]

    @api.model
    def whoami(self, uid=None):
        user = self.env["res.users"].browse(to_int(uid)).exists()
        if not user or not user.active:
            return {"ok": False}
        role = "customer"
        if user.has_group("otm_carwash.group_cw_manager") or user.has_group("base.group_system"):
            role = "manager"
        elif user.has_group("otm_carwash.group_cw_reception"):
            role = "reception"
        elif user.has_group("otm_carwash.group_cw_operator"):
            role = "operator"
        elif not user.has_group("base.group_portal"):
            return {"ok": False}
        emp = self._env("hr.employee").search([("user_id", "=", user.id)], limit=1) if role != "customer" else False
        partner = user.partner_id
        if role == "customer" and not partner.otm_cw_is_customer:
            partner.sudo().write({"otm_cw_is_customer": True})
        return {"ok": True, "uid": user.id, "name": user.name, "role": role,
                "partner_id": partner.id if role == "customer" else False,
                "employee_id": emp.id if emp else False}

    @api.model
    def static_config(self):
        """Rarely-changing data: Next.js caches this for a short TTL."""
        def rows(model, names, domain=None):
            return [rec_to_dict(r, names) for r in self._env(model).search(domain or [])]
        pricing = [{"service_id": p.service_id.id, "vehicle_type_id": p.vehicle_type_id.id,
                    "customer_type": p.customer_type, "price": p.price} for p in self.env["otm.cw.pricing"].search([])]
        return {
            "vehicle_types": rows("otm.cw.vehicle.type", ["name", "code"]),
            "services": rows("otm.cw.service", ["name", "code", "duration"]),
            "pricing": pricing,
            "bays": rows("otm.cw.bay", ["name", "number", "vehicle_type_ids", "service_ids", "staff_ids"]),
            "staff": rows("hr.employee", ["name", "otm_cw_role", "otm_cw_shift", "otm_cw_available"],
                          [("otm_cw_is_staff", "=", True)]),
            "packages": [dict(rec_to_dict(p, ["name", "price", "validity_days"]),
                              lines=[{"service_id": l.service_id.id, "service": l.service_id.name, "qty": l.qty}
                                     for l in p.line_ids]) for p in self.env["otm.cw.package"].search([])],
            "settings": {k: get_param(self.env, k) for k in PARAM_DEFAULTS},
        }

    @api.model
    def settings_set(self, values=None):
        ICP = self.env["ir.config_parameter"].sudo()
        for key, val in (values or {}).items():
            if key in PARAM_DEFAULTS:
                if key == "tz" and val not in pytz.all_timezones_set:
                    raise UserError(_("Unknown time zone."))
                ICP.set_param("otm_cw." + key, str(val))
        return True

    # ---------------------------------------------------------- operations
    @api.model
    def operations(self):
        now = fields.Datetime.now()
        Job, Bay = self.env["otm.cw.job"], self.env["otm.cw.bay"]
        start, end, days = self._bounds()
        jobs = Job.search([("state", "in", ACTIVE_STATES)], order="arrival_dt, id")
        bays = Bay.search([], order="number")
        queued = jobs.filtered(lambda j: j.state == "queued")
        live_bays = [b for b in bays if b.active and b.state != "offline"]
        cards = {}
        for pos, j in enumerate(queued, 1):
            elig = [b.number for b in live_bays if b.is_eligible(j)]
            cards[j.id] = self._job_card(j, now, pos, elig)
        for j in jobs - queued:
            cards[j.id] = self._job_card(j, now)
        bay_out = []
        for b in bays:
            job = b.current_job_id
            bay_out.append({"id": b.id, "name": b.name, "number": b.number, "state": b.state, "active": b.active,
                            "staff": b.staff_ids.mapped("name"), "job": cards.get(job.id) if job else None})
        q_cards = [cards[j.id] for j in queued]
        waits = [c["wait_minutes"] for c in q_cards]
        warn, crit = float(get_param(self.env, "warn_min")), float(get_param(self.env, "critical_min"))
        day_jobs = Job.search_count([("arrival_dt", ">=", start), ("arrival_dt", "<", end), ("state", "!=", "cancelled")])
        pay = self.env["otm.cw.payment"]._read_group(
            [("dt", ">=", start), ("dt", "<", end)], [], ["signed_amount:sum"])
        avg_wait = Job._read_group([("assigned_dt", ">=", start), ("assigned_dt", "<", end)], [], ["wait_minutes:avg"])
        avg_wash = Job._read_group([("end_dt", ">=", start), ("end_dt", "<", end), ("work_seconds", ">", 0)],
                                   [], ["work_seconds:avg"])
        return {
            "server_now": ser(now), "today": local_today(self.env).isoformat(),
            "bays": bay_out, "queue": q_cards,
            "ready": [cards[j.id] for j in jobs if j.state == "ready"],
            "quality": [cards[j.id] for j in jobs if j.state == "quality_check"],
            "metrics": {
                "vehicles_today": day_jobs, "revenue_today": round(pay[0][0] or 0.0, 2),
                "washing": len([j for j in jobs if j.state in ("washing", "paused")]),
                "waiting": len(q_cards), "ready": len([j for j in jobs if j.state == "ready"]),
                "available_bays": len([b for b in bays if b.active and b.state == "available"]),
                "total_bays": len([b for b in bays if b.active]),
                "avg_wait_today": round(avg_wait[0][0] or 0.0, 1),
                "avg_wash_minutes": round((avg_wash[0][0] or 0.0) / 60.0, 1),
                "current_avg_wait": round(sum(waits) / len(waits), 1) if waits else 0,
                "max_wait": round(max(waits), 1) if waits else 0,
                "longest_waiting": q_cards[waits.index(max(waits))]["vehicle"]["reg_no"] if waits else None,
                "over_warn": len([w for w in waits if w > warn]), "over_critical": len([w for w in waits if w > crit]),
                "warn_min": warn, "critical_min": crit,
            },
        }

    # ----------------------------------------------------------- dashboard
    @api.model
    def dashboard(self, date_from=None, date_to=None):
        ops = self.operations()
        start, end, days = self._bounds(date_from, date_to)
        trend_start = self._bounds(local_today(self.env) - timedelta(days=13), local_today(self.env))[0]
        trend_end = self._bounds()[1]
        rev = self._sql(f"""SELECT {self._sql_day('dt')} AS d, SUM(signed_amount) AS v FROM otm_cw_payment
                            WHERE dt >= %(s)s AND dt < %(e)s GROUP BY 1 ORDER BY 1""", {"s": trend_start, "e": trend_end})
        cnt = self._sql(f"""SELECT {self._sql_day('arrival_dt')} AS d, COUNT(*) AS v, AVG(wait_minutes) AS w
                            FROM otm_cw_job WHERE arrival_dt >= %(s)s AND arrival_dt < %(e)s AND state <> 'cancelled'
                            GROUP BY 1 ORDER BY 1""", {"s": trend_start, "e": trend_end})
        yesterday_rev = 0.0
        y0 = self._bounds(local_today(self.env) - timedelta(days=1))
        yrow = self._read_sum(y0[0], y0[1])
        yesterday_rev = yrow
        svc = self._sql("""SELECT s.name AS name, COUNT(*) AS count, SUM(l.price) AS revenue FROM otm_cw_job_line l
                           JOIN otm_cw_job j ON j.id = l.job_id JOIN otm_cw_service s ON s.id = l.service_id
                           WHERE j.arrival_dt >= %(s)s AND j.arrival_dt < %(e)s AND j.state <> 'cancelled'
                           GROUP BY s.name ORDER BY count DESC LIMIT 10""", {"s": start, "e": end})
        vtype = self._sql("""SELECT vt.name AS name, COUNT(*) AS count FROM otm_cw_job j
                             JOIN otm_cw_vehicle_type vt ON vt.id = j.vehicle_type_id
                             WHERE j.arrival_dt >= %(s)s AND j.arrival_dt < %(e)s AND j.state <> 'cancelled'
                             GROUP BY vt.name ORDER BY count DESC""", {"s": start, "e": end})
        return {
            "operations": ops,
            "revenue_vs_yesterday": round(((ops["metrics"]["revenue_today"] - yesterday_rev) / yesterday_rev * 100.0), 1)
            if yesterday_rev else None,
            "revenue_trend": [{"date": r["d"].isoformat(), "value": float(r["v"] or 0)} for r in rev],
            "vehicle_trend": [{"date": r["d"].isoformat(), "value": r["v"]} for r in cnt],
            "wait_trend": [{"date": r["d"].isoformat(), "value": round(float(r["w"] or 0), 1)} for r in cnt],
            "service_popularity": [{"name": r["name"], "count": r["count"], "revenue": float(r["revenue"] or 0)} for r in svc],
            "vehicle_types": vtype,
            "bay_utilization": self.bay_utilization(date_from=date_from, date_to=date_to),
            "staff_productivity": self.staff_performance(date_from=date_from, date_to=date_to)[:8],
        }

    def _read_sum(self, start, end):
        res = self.env["otm.cw.payment"]._read_group([("dt", ">=", start), ("dt", "<", end)], [], ["signed_amount:sum"])
        return res[0][0] or 0.0

    @api.model
    def bay_utilization(self, date_from=None, date_to=None):
        start, end, days = self._bounds(date_from, date_to)
        now = fields.Datetime.now()
        rows = self._sql("""SELECT bay_id, SUM(EXTRACT(EPOCH FROM (LEAST(COALESCE(end_dt, %(now)s), %(e)s)
                            - GREATEST(start_dt, %(s)s)))) AS busy FROM otm_cw_job
                            WHERE start_dt IS NOT NULL AND bay_id IS NOT NULL AND state <> 'cancelled'
                              AND start_dt < %(e)s AND COALESCE(end_dt, %(now)s) > %(s)s GROUP BY bay_id""",
                         {"s": start, "e": end, "now": now})
        busy = {r["bay_id"]: float(r["busy"] or 0) for r in rows}
        op = self._operating_seconds(days, start, end)
        out = []
        for b in self.env["otm.cw.bay"].search([("active", "=", True)], order="number"):
            pct = min(busy.get(b.id, 0.0) / op * 100.0, 100.0) if op else 0.0
            out.append({"bay": b.name, "number": b.number, "busy_minutes": round(busy.get(b.id, 0.0) / 60.0, 1),
                        "utilization": round(pct, 1)})
        return out

    @api.model
    def staff_performance(self, date_from=None, date_to=None, employee_id=None):
        start, end, days = self._bounds(date_from, date_to)
        rows = self._sql("""
            WITH j AS (
              SELECT j.id, j.work_seconds, j.wait_minutes, j.amount_total,
                     (SELECT COUNT(*) FROM otm_cw_job_line l WHERE l.job_id = j.id) AS svc,
                     (SELECT COUNT(*) FROM otm_cw_job_actual_rel x WHERE x.job_id = j.id) AS nstaff
              FROM otm_cw_job j WHERE j.state IN ('quality_check','ready','completed')
               AND j.end_dt >= %(s)s AND j.end_dt < %(e)s)
            SELECT r.employee_id AS emp, COUNT(*) AS jobs, SUM(j.svc) AS services, SUM(j.work_seconds) AS secs,
                   AVG(j.work_seconds) AS avg_secs, AVG(j.wait_minutes) AS avg_wait,
                   SUM(j.amount_total / GREATEST(j.nstaff, 1)) AS revenue
            FROM j JOIN otm_cw_job_actual_rel r ON r.job_id = j.id GROUP BY r.employee_id""", {"s": start, "e": end})
        by_emp = {r["emp"]: r for r in rows}
        domain = [("otm_cw_is_staff", "=", True)]
        if employee_id:
            domain.append(("id", "=", to_int(employee_id)))
        out = []
        for e in self._env("hr.employee").search(domain):
            r = by_emp.get(e.id)
            jobs = r["jobs"] if r else 0
            secs = float(r["secs"] or 0) if r else 0.0
            out.append({
                "id": e.id, "name": e.name, "role": e.otm_cw_role, "available": e.otm_cw_available,
                "vehicles_completed": jobs, "services_completed": int(r["services"] or 0) if r else 0,
                "total_work_minutes": round(secs / 60.0, 1),
                "avg_job_minutes": round(float(r["avg_secs"] or 0) / 60.0, 1) if r else 0.0,
                "avg_wait_minutes": round(float(r["avg_wait"] or 0), 1) if r else 0.0,
                "revenue": round(float(r["revenue"] or 0), 2) if r else 0.0,
                "jobs_per_day": round(jobs / max(len(days), 1), 2),
                "jobs_per_hour": round(jobs / (secs / 3600.0), 2) if secs else 0.0,
            })
        out.sort(key=lambda x: (-x["vehicles_completed"], x["name"]))
        return out

    @api.model
    def staff_summary(self, employee_id=None, date_from=None, date_to=None):
        emp = self._env("hr.employee").browse(to_int(employee_id)).exists()
        if not emp:
            raise UserError(_("Staff member not found."))
        perf = self.staff_performance(date_from=date_from, date_to=date_to, employee_id=emp.id)
        now = fields.Datetime.now()
        recent = self.env["otm.cw.job"].search([("actual_staff_ids", "in", emp.id)], order="id desc", limit=20)
        current = self.env["otm.cw.job"].search([("state", "in", ["assigned", "washing", "paused"]),
                                                 "|", ("actual_staff_ids", "in", emp.id),
                                                 ("assigned_staff_ids", "in", emp.id)], order="id")
        return {"staff": rec_to_dict(emp, ["name", "otm_cw_role", "otm_cw_shift", "otm_cw_mobile", "otm_cw_available",
                                           "otm_cw_service_ids"]),
                "performance": perf[0] if perf else None,
                "current_jobs": [self._job_card(j, now) for j in current],
                "recent_jobs": [self._job_card(j, now) for j in recent]}

    @api.model
    def reports(self, date_from=None, date_to=None):
        start, end, days = self._bounds(date_from, date_to)
        svc = self._sql("""SELECT s.name AS name, COUNT(*) AS count, SUM(l.price) AS revenue, AVG(l.duration) AS avg_duration
                           FROM otm_cw_job_line l JOIN otm_cw_job j ON j.id = l.job_id
                           JOIN otm_cw_service s ON s.id = l.service_id
                           WHERE j.arrival_dt >= %(s)s AND j.arrival_dt < %(e)s AND j.state <> 'cancelled'
                           GROUP BY s.name ORDER BY count DESC""", {"s": start, "e": end})
        matrix = self._sql("""SELECT s.name AS service, vt.name AS vehicle_type, COUNT(*) AS count
                              FROM otm_cw_job_line l JOIN otm_cw_job j ON j.id = l.job_id
                              JOIN otm_cw_service s ON s.id = l.service_id
                              JOIN otm_cw_vehicle_type vt ON vt.id = j.vehicle_type_id
                              WHERE j.arrival_dt >= %(s)s AND j.arrival_dt < %(e)s AND j.state <> 'cancelled'
                              GROUP BY 1, 2""", {"s": start, "e": end})
        cust = self._sql("""
            WITH per AS (SELECT partner_id, COUNT(*) AS visits, SUM(paid_amount) AS spent, MAX(arrival_dt) AS last_visit,
                                MIN(arrival_dt) AS first_visit FROM otm_cw_job WHERE state <> 'cancelled' GROUP BY partner_id)
            SELECT (SELECT COUNT(*) FROM per) AS total,
                   (SELECT COUNT(*) FROM per WHERE first_visit >= %(s)s AND first_visit < %(e)s) AS new_customers,
                   (SELECT COUNT(DISTINCT j.partner_id) FROM otm_cw_job j JOIN per ON per.partner_id = j.partner_id
                     WHERE j.arrival_dt >= %(s)s AND j.arrival_dt < %(e)s AND per.first_visit < %(s)s) AS returning_customers,
                   (SELECT AVG(spent) FROM per) AS avg_spend, (SELECT AVG(visits) FROM per) AS avg_visits""",
                         {"s": start, "e": end})[0]
        top = self._sql("""SELECT p.id, p.name, COUNT(*) AS visits, SUM(j.paid_amount) AS spent FROM otm_cw_job j
                           JOIN res_partner p ON p.id = j.partner_id WHERE j.state <> 'cancelled'
                           GROUP BY p.id, p.name ORDER BY spent DESC NULLS LAST LIMIT 10""", {})
        inactive = self._sql("""SELECT p.id, p.name, MAX(j.arrival_dt) AS last_visit FROM otm_cw_job j
                                JOIN res_partner p ON p.id = j.partner_id WHERE j.state <> 'cancelled'
                                GROUP BY p.id, p.name HAVING MAX(j.arrival_dt) < %(cut)s
                                ORDER BY last_visit LIMIT 20""", {"cut": fields.Datetime.now() - timedelta(days=60)})
        return {
            "services": [{"name": r["name"], "count": r["count"], "revenue": float(r["revenue"] or 0),
                          "avg_duration": round(float(r["avg_duration"] or 0), 1)} for r in svc],
            "service_by_vehicle": matrix,
            "customers": {"total": cust["total"], "new": cust["new_customers"], "returning": cust["returning_customers"],
                          "avg_spend": round(float(cust["avg_spend"] or 0), 2),
                          "avg_visits": round(float(cust["avg_visits"] or 0), 2)},
            "top_customers": [{"id": r["id"], "name": r["name"], "visits": r["visits"], "spent": float(r["spent"] or 0)} for r in top],
            "inactive_customers": [{"id": r["id"], "name": r["name"], "last_visit": ser(r["last_visit"])} for r in inactive],
            "bay_utilization": self.bay_utilization(date_from=date_from, date_to=date_to),
            "staff": self.staff_performance(date_from=date_from, date_to=date_to),
        }

    # ----------------------------------------------------- generic resources
    @api.model
    def list_records(self, resource=None, search="", offset=0, limit=25, filters=None, date_from=None, date_to=None,
                     scope_partner_id=None):
        cfg = RESOURCES.get(resource)
        if not cfg:
            raise UserError(_("Unknown resource."))
        Model = self._env(cfg["model"])
        domain = list(cfg.get("domain", []))
        if cfg.get("scope"):
            if scope_partner_id:
                domain.append((cfg["scope"], "=", to_int(scope_partner_id)))
        elif scope_partner_id:
            raise UserError(_("Not allowed."))
        if search:
            terms = [(f, "ilike", search) for f in cfg["search"]]
            domain += ["|"] * (len(terms) - 1) + terms
        for key, val in (filters or {}).items():
            if (key in cfg["fields"] or key == "id") and val not in (None, ""):
                domain.append((key, "=", to_int(val) if Model._fields[key].type in ("many2one", "integer") else val))
        if cfg.get("date_field") and (date_from or date_to):
            start, end, _days = self._bounds(date_from, date_to)
            domain += [(cfg["date_field"], ">=", start), (cfg["date_field"], "<", end)]
        offset, limit = max(to_int(offset) or 0, 0), min(max(to_int(limit) or 25, 1), PAGE_MAX)
        total = Model.search_count(domain)
        recs = Model.search(domain, order=cfg["order"], offset=offset, limit=limit)
        items = [rec_to_dict(r, cfg["fields"]) for r in recs]
        by_id = {i["id"]: i for i in items}
        if resource == "customers" and recs:
            Job = self.env["otm.cw.job"]
            for p, cnt, paid, last in Job._read_group(
                    [("partner_id", "in", recs.ids), ("state", "!=", "cancelled")], ["partner_id"],
                    ["__count", "paid_amount:sum", "arrival_dt:max"]):
                by_id[p.id].update(visits=cnt, revenue=round(paid or 0.0, 2), last_visit=ser(last))
            for p, cnt in self.env["otm.cw.vehicle"]._read_group(
                    [("partner_id", "in", recs.ids)], ["partner_id"], ["__count"]):
                by_id[p.id]["vehicle_count"] = cnt
        if resource == "vehicles" and recs:
            for v, cnt, last in self.env["otm.cw.job"]._read_group(
                    [("vehicle_id", "in", recs.ids), ("state", "!=", "cancelled")], ["vehicle_id"],
                    ["__count", "arrival_dt:max"]):
                by_id[v.id].update(total_washes=cnt, last_wash=ser(last))
        if resource == "packages":
            for p in recs:
                by_id[p.id]["lines"] = [{"service_id": l.service_id.id, "service": l.service_id.name, "qty": l.qty}
                                        for l in p.line_ids]
        if resource == "subscriptions":
            today = local_today(self.env)
            for s in recs:
                by_id[s.id]["lines"] = [{"service": l.service_id.name, "total": l.total, "used": l.used,
                                         "remaining": l.remaining} for l in s.line_ids]
                by_id[s.id]["expired"] = s.expiry_date < today
        return {"items": items, "total": total, "offset": offset, "limit": limit}

    @api.model
    def save_record(self, resource=None, record_id=None, values=None, scope_partner_id=None):
        cfg = RESOURCES.get(resource)
        if not cfg or cfg.get("readonly"):
            raise UserError(_("This resource cannot be edited here."))
        Model = self._env(cfg["model"])
        values = dict(values or {})
        vals = {}
        for name in cfg["fields"]:
            if name not in values:
                continue
            ftype = Model._fields[name].type
            v = values[name]
            if ftype == "many2one":
                vals[name] = to_int(v) or False
            elif ftype == "many2many":
                vals[name] = [(6, 0, to_ids(v))]
            elif ftype in ("integer",):
                vals[name] = to_int(v) or 0
            elif ftype == "float":
                vals[name] = float(v or 0)
            elif ftype == "boolean":
                vals[name] = bool(v)
            else:
                vals[name] = (v or False) if isinstance(v, str) else v
        if cfg.get("scope") == "partner_id" and scope_partner_id and resource == "vehicles":
            vals["partner_id"] = to_int(scope_partner_id)
        if record_id:
            rec = Model.browse(to_int(record_id)).exists()
            if not rec:
                raise UserError(_("Record not found."))
            if scope_partner_id and rec.partner_id.id != to_int(scope_partner_id):
                raise UserError(_("Not allowed."))
            if resource == "bays" and "state" in vals:
                self._check_bay_state(rec, vals["state"])
            rec.write(vals)
        else:
            vals.update(cfg.get("defaults", {}))
            if resource == "customers":
                vals.setdefault("phone", vals.get("otm_cw_mobile"))
            rec = Model.create(vals)
        if resource == "packages" and "lines" in values:
            rec.line_ids.unlink()
            rec.write({"line_ids": [(0, 0, {"service_id": to_int(l.get("service_id")), "qty": max(to_int(l.get("qty")) or 1, 1)})
                                    for l in values["lines"] if to_int(l.get("service_id"))]})
        if resource == "bays":
            self.env["otm.cw.job"].dispatch()
        return rec_to_dict(rec, cfg["fields"])

    def _check_bay_state(self, bay, state):
        if state not in ("available", "cleaning", "maintenance", "offline"):
            raise UserError(_("Bay status can only be set to available, cleaning, maintenance or offline."))
        if bay.current_job_id and bay.current_job_id.state in ("assigned", "washing", "paused"):
            raise UserError(_("%s has an active job. Finish or return it to the queue first.") % bay.name)

    @api.model
    def delete_record(self, resource=None, record_id=None):
        cfg = RESOURCES.get(resource)
        if not cfg or cfg.get("readonly") or resource in ("customers", "staff"):
            raise UserError(_("This record cannot be deleted. Archive it instead."))
        rec = self.env[cfg["model"]].browse(to_int(record_id)).exists()
        try:
            with self.env.cr.savepoint():
                rec.unlink()
        except Exception as exc:
            raise UserError(_("This record is in use. Archive it instead.")) from exc
        return True

    # ---------------------------------------------------- pricing + quoting
    @api.model
    def pricing_set(self, cells=None):
        Pricing = self.env["otm.cw.pricing"]
        for c in cells or []:
            domain = [("service_id", "=", to_int(c["service_id"])), ("vehicle_type_id", "=", to_int(c["vehicle_type_id"])),
                      ("customer_type", "=", c.get("customer_type") or "standard")]
            existing = Pricing.search(domain, limit=1)
            if c.get("price") in (None, ""):
                existing.unlink()
            elif existing:
                existing.price = float(c["price"])
            else:
                Pricing.create(dict((d[0], d[2]) for d in domain) | {"price": float(c["price"])})
        return True

    @api.model
    def quote(self, partner_id=None, vehicle_type_id=None, service_ids=None):
        partner = self.env["res.partner"].browse(to_int(partner_id)).exists()
        vtype = self.env["otm.cw.vehicle.type"].browse(to_int(vehicle_type_id)).exists()
        if not vtype:
            raise UserError(_("Select a vehicle type."))
        lines, subtotal, minutes = [], 0.0, 0
        for svc in self.env["otm.cw.service"].browse(to_ids(service_ids)).exists():
            price, source = self.env["otm.cw.pricing"].price_for(partner, vtype, svc)
            lines.append({"service_id": svc.id, "name": svc.name, "price": price, "source": source, "duration": svc.duration})
            subtotal += price
            minutes += svc.duration
        disc = subtotal * (partner.otm_cw_discount or 0.0) / 100.0 if partner else 0.0
        return {"lines": lines, "subtotal": subtotal, "discount": round(disc, 2), "total": round(subtotal - disc, 2),
                "minutes": minutes, "missing": [l["name"] for l in lines if l["source"] == "missing"]}

    # ------------------------------------------------------------ walk-in
    @api.model
    def lookup(self, reg_no=None, mobile=None):
        out = {"vehicle": None, "customer": None}
        key = self.env["otm.cw.vehicle"].normalize_reg(reg_no)
        veh = self.env["otm.cw.vehicle"].search([("reg_key", "=", key)], limit=1) if key else False
        partner = veh.partner_id if veh else False
        if not partner and mobile_key(mobile):
            partner = self.env["res.partner"].search([("otm_cw_mobile_key", "=", mobile_key(mobile)),
                                                     ("otm_cw_is_customer", "=", True)], limit=1)
        if veh:
            out["vehicle"] = rec_to_dict(veh, ["reg_no", "vehicle_type_id", "brand", "model", "color"])
        if partner:
            out["customer"] = rec_to_dict(partner, ["name", "otm_cw_mobile", "otm_cw_customer_type", "otm_cw_discount"])
            today = local_today(self.env)
            out["subscriptions"] = [
                {"id": s.id, "name": s.name, "package": s.package_id.name,
                 "lines": [{"service_id": l.service_id.id, "service": l.service_id.name, "remaining": l.remaining}
                           for l in s.line_ids if l.remaining > 0]}
                for s in self.env["otm.cw.subscription"].search([("partner_id", "=", partner.id), ("expiry_date", ">=", today)])]
        return out

    def _get_or_create_customer(self, name, mobile):
        Partner = self.env["res.partner"]
        key = mobile_key(mobile)
        if not key:
            raise UserError(_("A valid mobile number is required."))
        partner = Partner.search([("otm_cw_mobile_key", "=", key), ("otm_cw_is_customer", "=", True)], limit=1)
        if partner:
            return partner
        if not (name or "").strip():
            raise UserError(_("Customer name is required for a new customer."))
        return Partner.create({"name": name.strip(), "otm_cw_mobile": mobile.strip(), "phone": mobile.strip(),
                               "otm_cw_is_customer": True})

    def _get_or_create_vehicle(self, partner, reg_no, vehicle_type_id, brand="", model="", color=""):
        Vehicle = self.env["otm.cw.vehicle"]
        key = Vehicle.normalize_reg(reg_no)
        if not key:
            raise UserError(_("Registration number is required."))
        veh = Vehicle.search([("reg_key", "=", key)], limit=1)
        if veh:
            if veh.partner_id != partner:
                raise UserError(_("Vehicle %s is registered to another customer.") % veh.reg_no)
            return veh
        if not to_int(vehicle_type_id):
            raise UserError(_("Select a vehicle type."))
        return Vehicle.create({"reg_no": reg_no, "vehicle_type_id": to_int(vehicle_type_id), "partner_id": partner.id,
                               "brand": brand or False, "model": model or False, "color": color or False})

    @api.model
    def walkin(self, name=None, mobile=None, reg_no=None, vehicle_type_id=None, service_ids=None, brand="", model="",
               color="", priority="normal", subscription_id=None, notes="", notify_opt_in=None):
        partner = self._get_or_create_customer(name, mobile or "")
        if notify_opt_in is not None and partner.otm_cw_notify_opt_in != bool(notify_opt_in):
            partner.sudo().otm_cw_notify_opt_in = bool(notify_opt_in)
        vehicle = self._get_or_create_vehicle(partner, reg_no, vehicle_type_id, brand, model, color)
        services = self.env["otm.cw.service"].browse(to_ids(service_ids)).exists()
        sub = self.env["otm.cw.subscription"].browse(to_int(subscription_id)).exists() if subscription_id else None
        if priority not in ("normal", "priority", "vip"):
            priority = "normal"
        job = self.env["otm.cw.job"].create_job(partner, vehicle, services, priority, None, sub, notes)
        self.env["otm.cw.job"].dispatch()
        return self.job_detail(job_id=job.id)

    # ------------------------------------------------------- notifications
    @api.model
    def notify_settings_get(self):
        env = self.env
        out = {k: _np(env, k) for k in NOTIFY_PLAIN}
        out["country_code"] = _np(env, "wa_country", "91")
        for k in NOTIFY_SECRET:
            out[k + "_set"] = bool(_np(env, k))            # secrets are write-only: never returned
        out["events"] = {e: {"enabled": env["otm.cw.notification"].event_enabled(e),
                             "template": _np(env, "tpl_" + e) or EVENT_DEFAULTS[e][1],
                             "wa_template": _np(env, "wa_tpl_" + e)} for e in EVENT_DEFAULTS}
        out["event_labels"] = dict(EVENTS)
        out["active_channels"] = env["otm.cw.notification"]._enabled_channels()
        return out

    @api.model
    def notify_settings_set(self, values=None):
        ICP, v = self.env["ir.config_parameter"].sudo(), values or {}
        for k in NOTIFY_PLAIN + ("wa_country",):
            if k in v and k != "tg_bot":   # bot username is learned from Telegram, not typed in
                ICP.set_param(NOTIFY_P + k, str(v[k] or "").strip()[:200])
        for k in NOTIFY_SECRET:
            if v.get(k):                                    # empty = keep existing
                ICP.set_param(NOTIFY_P + k, str(v[k]).strip()[:500])
            if v.get(k + "_clear"):
                ICP.set_param(NOTIFY_P + k, "")
        for e, cfg in (v.get("events") or {}).items():
            if e not in EVENT_DEFAULTS:
                continue
            if "enabled" in cfg:
                ICP.set_param(NOTIFY_P + "ev_" + e, "1" if cfg["enabled"] else "0")
            if "template" in cfg:
                ICP.set_param(NOTIFY_P + "tpl_" + e, str(cfg["template"] or "").strip()[:500])
            if "wa_template" in cfg:
                ICP.set_param(NOTIFY_P + "wa_tpl_" + e, str(cfg["wa_template"] or "").strip()[:100])
        if v.get("tg_token"):
            self._telegram_refresh_bot()
        return self.notify_settings_get()

    def _telegram_refresh_bot(self):
        Note = self.env["otm.cw.notification"]
        try:
            r = Note._http("GET", "https://api.telegram.org/bot%s/getMe" % _np(self.env, "tg_token"))
            self.env["ir.config_parameter"].sudo().set_param(NOTIFY_P + "tg_bot", r.json().get("result", {}).get("username", ""))
        except Exception:  # noqa: BLE001 - invalid token is reported by the test button
            self.env["ir.config_parameter"].sudo().set_param(NOTIFY_P + "tg_bot", "")

    @api.model
    def notify_test(self, channel=None, to=None):
        Note = self.env["otm.cw.notification"].sudo()
        if channel not in dict(CHANNELS) or not (to or "").strip():
            raise UserError(_("Choose a channel and a recipient."))
        text = _("Test message from %s. Notifications are working.") % (_np(self.env, "shop_name") or "Car Wash")
        to = to.strip()
        try:
            if channel == "whatsapp":
                Note._send_whatsapp(_digits(to, _np(self.env, "wa_country", "91")), text)
            elif channel == "telegram":
                Note._send_telegram(to, text)
            elif channel == "sms":
                Note._send_sms(to if to.startswith("+") else "+" + _digits(to, _np(self.env, "wa_country", "91")), text)
            else:
                Note._send_email(to, text)
        except UserError as exc:
            raise UserError(Note._clean_error(exc)) from exc
        return {"ok": True}

    @api.model
    def notify_job(self, job_id=None, event="vehicle_ready", channels=None):
        job = self.env["otm.cw.job"].browse(to_int(job_id)).exists()
        if not job or event not in ("vehicle_assigned", "wash_started", "vehicle_ready", "delivered"):
            raise UserError(_("Cannot send this notification."))
        rows = self.env["otm.cw.notification"].notify(event, job.partner_id, "", job=job, force=True, channels=channels or None)
        return {"queued": len(rows.filtered(lambda r: r.state == "pending")),
                "skipped": [r.error for r in rows if r.state == "skipped"]}

    @api.model
    def notify_flush(self):
        return self.env["otm.cw.notification"].process_queue()

    @api.model
    def notify_resend(self, notification_id=None):
        self.env["otm.cw.notification"].browse(to_int(notification_id)).exists().action_retry()
        return self.env["otm.cw.notification"].process_queue()

    @api.model
    def telegram_link_code(self, partner_id=None):
        p = self.env["res.partner"].browse(to_int(partner_id)).exists()
        if not p:
            raise UserError(_("Customer not found."))
        bot = _np(self.env, "tg_bot")
        if not bot or _np(self.env, "tg_enabled") != "1":
            raise UserError(_("Telegram updates are not enabled yet."))
        code = secrets.token_urlsafe(8).replace("-", "x").replace("_", "y")
        p.sudo().write({"otm_cw_telegram_code": code})
        return {"code": code, "bot": bot, "link": "https://t.me/%s?start=%s" % (bot, code),
                "linked": bool(p.otm_cw_telegram_chat_id)}

    @api.model
    def telegram_link(self, code=None, chat_id=None):
        code = (code or "").strip()
        if not code or not chat_id:
            return {"ok": False}
        p = self.env["res.partner"].sudo().search([("otm_cw_telegram_code", "=", code)], limit=1)
        if not p:
            return {"ok": False}
        p.write({"otm_cw_telegram_chat_id": str(chat_id), "otm_cw_telegram_code": False, "otm_cw_notify_opt_in": True})
        try:
            self.env["otm.cw.notification"]._send_telegram(str(chat_id), _("Hi %s, Telegram is now connected. We'll message you about your vehicle here.") % p.name)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    @api.model
    def telegram_unlink(self, partner_id=None):
        self.env["res.partner"].sudo().browse(to_int(partner_id)).write({"otm_cw_telegram_chat_id": False})
        return True

    @api.model
    def telegram_set_webhook(self, url=None, secret=None):
        Note = self.env["otm.cw.notification"]
        if not _np(self.env, "tg_token"):
            raise UserError(_("Save the Telegram bot token first."))
        try:
            Note._http("POST", "https://api.telegram.org/bot%s/setWebhook" % _np(self.env, "tg_token"),
                       json={"url": url, "secret_token": secret, "allowed_updates": ["message"]})
        except UserError as exc:
            raise UserError(Note._clean_error(exc)) from exc
        return {"ok": True}

    @api.model
    def job_notifications(self, job_id=None):
        return [rec_to_dict(n, ["event", "channel", "recipient", "state", "error", "sent_dt", "create_date"])
                for n in self.env["otm.cw.notification"].search([("job_id", "=", to_int(job_id))], limit=20)]

    # ---------------------------------------------------------------- jobs
    @api.model
    def job_detail(self, job_id=None, scope_partner_id=None):
        job = self.env["otm.cw.job"].browse(to_int(job_id)).exists()
        if not job or (scope_partner_id and job.partner_id.id != to_int(scope_partner_id)):
            raise UserError(_("Job not found."))
        now = fields.Datetime.now()
        queued = self.env["otm.cw.job"].search([("state", "=", "queued")], order="arrival_dt, id")
        pos = (queued.ids.index(job.id) + 1) if job.id in queued.ids else None
        card = self._job_card(job, now, pos)
        card["server_now"] = ser(now)
        card["lines"] = [{"service": l.service_id.name, "price": l.price, "duration": l.duration} for l in job.line_ids]
        card["subtotal"], card["discount"] = job.subtotal, job.discount_amount
        card["payments"] = [rec_to_dict(p, ["amount", "method", "kind", "reference", "dt", "received_by"]) for p in job.payment_ids]
        card["invoice"] = job.sudo().invoice_id.name if job.invoice_id else None
        card["notes"], card["qc_notes"] = job.notes or "", job.qc_notes or ""
        card["delivered_dt"] = ser(job.delivered_dt)
        card["inspections"] = [{"id": i.id, "kind": i.kind, "damage_notes": i.damage_notes or "",
                                "customer_notes": i.customer_notes or "", "accessories": i.accessories or "",
                                "staff_confirmed": i.staff_confirmed, "customer_confirmed": i.customer_confirmed,
                                "photos": {k: bool(i["photo_" + k]) for k in ("front", "rear", "left", "right")}}
                               for i in self.env["otm.cw.inspection"].search([("job_id", "=", job.id)])]
        if not scope_partner_id:
            card["audit"] = [rec_to_dict(a, ["action", "detail", "actor", "dt"]) for a in
                             self.env["otm.cw.audit"].search([("res_model", "=", "otm.cw.job"), ("res_id", "=", job.id)], limit=50)]
        card["feedback"] = self.env["otm.cw.feedback"].search_count([("job_id", "=", job.id)]) > 0
        return card

    @api.model
    def job_action(self, job_id=None, action=None, params=None):
        params = params or {}
        job = self.env["otm.cw.job"].browse(to_int(job_id)).exists()
        if not job:
            raise UserError(_("Job not found."))
        actor = self.env.context.get("cw_actor") or ""
        if action == "assign":
            bay = self.env["otm.cw.bay"].browse(to_int(params.get("bay_id"))).exists()
            if not bay:
                raise UserError(_("Select a bay."))
            job.action_assign(bay, actor)
        elif action == "unassign":
            job.action_unassign()
            self.env["otm.cw.job"].dispatch()
        elif action == "start":
            job.action_start(to_ids(params.get("employee_ids")))
        elif action == "pause":
            job.action_pause()
        elif action == "resume":
            job.action_resume()
        elif action == "finish":
            job.action_finish_wash()
            self.env["otm.cw.job"].dispatch()
        elif action == "qc_pass":
            job.action_qc(True, params.get("notes", ""), actor)
        elif action == "qc_fail":
            job.action_qc(False, params.get("notes", ""), actor)
        elif action == "ready":
            job.action_ready()
        elif action == "deliver":
            job.action_deliver()
        elif action == "cancel":
            job.action_cancel(params.get("reason", ""))
            self.env["otm.cw.job"].dispatch()
        elif action == "payment":
            job.action_add_payment(float(params.get("amount") or 0), params.get("method") or "cash",
                                   params.get("reference", ""), "payment", actor)
        elif action == "refund":
            job.action_add_payment(float(params.get("amount") or 0), params.get("method") or "cash",
                                   params.get("reference", ""), "refund", actor)
        elif action == "invoice":
            job.action_create_invoice()
        else:
            raise UserError(_("Unknown action."))
        return self.job_detail(job_id=job.id)

    @api.model
    def inspection_save(self, job_id=None, kind=None, values=None):
        job = self.env["otm.cw.job"].browse(to_int(job_id)).exists()
        if not job or kind not in ("before", "after"):
            raise UserError(_("Invalid inspection."))
        values = values or {}
        vals = {k: values.get(k) for k in ("damage_notes", "customer_notes", "accessories") if k in values}
        for side in ("front", "rear", "left", "right"):
            raw = values.get("photo_" + side)
            if raw:
                if "," in raw:
                    raw = raw.split(",", 1)[1]
                if len(raw) > 4_000_000:
                    raise UserError(_("Photo is too large."))
                vals["photo_" + side] = raw
        for flag in ("staff_confirmed", "customer_confirmed"):
            if flag in values:
                vals[flag] = bool(values[flag])
        insp = self.env["otm.cw.inspection"].search([("job_id", "=", job.id), ("kind", "=", kind)], limit=1)
        vals["done_by"] = self.env.context.get("cw_actor") or self.env.user.name
        if insp:
            insp.write(vals)
        else:
            insp = self.env["otm.cw.inspection"].create(dict(vals, job_id=job.id, kind=kind))
        self.env["otm.cw.audit"].log(job, "Inspection saved", kind)
        return self.job_detail(job_id=job.id)

    @api.model
    def inspection_photo(self, inspection_id=None, side=None, scope_partner_id=None):
        insp = self.env["otm.cw.inspection"].browse(to_int(inspection_id)).exists()
        if not insp or side not in ("front", "rear", "left", "right"):
            raise UserError(_("Photo not found."))
        if scope_partner_id and insp.job_id.partner_id.id != to_int(scope_partner_id):
            raise UserError(_("Not allowed."))
        data = insp["photo_" + side]
        return {"data": data.decode() if isinstance(data, bytes) else (data or "")}

    # ------------------------------------------------------------- bookings
    @api.model
    def booking_create(self, partner_id=None, vehicle_id=None, service_ids=None, slot=None, source="reception", notes=""):
        partner = self.env["res.partner"].browse(to_int(partner_id)).exists()
        vehicle = self.env["otm.cw.vehicle"].browse(to_int(vehicle_id)).exists()
        if not partner or not vehicle or vehicle.partner_id != partner:
            raise UserError(_("Select a valid customer and vehicle."))
        try:
            slot_dt = fields.Datetime.to_datetime(slot.replace("T", " ").replace("Z", "")[:19])
        except Exception:
            raise UserError(_("Invalid booking time."))
        services = self.env["otm.cw.service"].browse(to_ids(service_ids)).exists()
        b = self.env["otm.cw.booking"].create_booking(partner, vehicle, services, slot_dt, source, notes)
        return rec_to_dict(b, RESOURCES["bookings"]["fields"])

    @api.model
    def booking_action(self, booking_id=None, action=None, scope_partner_id=None, reason=""):
        b = self.env["otm.cw.booking"].browse(to_int(booking_id)).exists()
        if not b or (scope_partner_id and b.partner_id.id != to_int(scope_partner_id)):
            raise UserError(_("Booking not found."))
        if scope_partner_id and action != "cancel":
            raise UserError(_("Not allowed."))
        if action == "confirm":
            b.action_confirm()
        elif action == "arrive":
            b.action_arrive()
        elif action == "cancel":
            b.action_cancel(reason)
        elif action == "no_show":
            b.action_no_show()
        elif action == "check_in":
            b.action_check_in()
            self.env["otm.cw.job"].dispatch()
        else:
            raise UserError(_("Unknown action."))
        return rec_to_dict(b, RESOURCES["bookings"]["fields"] + ["job_id"])

    # ------------------------------------------------------------- packages
    @api.model
    def package_sell(self, partner_id=None, package_id=None, method="cash"):
        partner = self.env["res.partner"].browse(to_int(partner_id)).exists()
        package = self.env["otm.cw.package"].browse(to_int(package_id)).exists()
        if not partner or not package:
            raise UserError(_("Select a customer and package."))
        sub = self.env["otm.cw.subscription"].sell(partner, package, method)
        self.env["otm.cw.audit"].log(sub, "Package sold", "%s %.2f" % (method, sub.amount))
        return rec_to_dict(sub, RESOURCES["subscriptions"]["fields"])

    @api.model
    def feedback_add(self, job_id=None, rating=None, comment="", scope_partner_id=None):
        job = self.env["otm.cw.job"].browse(to_int(job_id)).exists()
        if not job or (scope_partner_id and job.partner_id.id != to_int(scope_partner_id)):
            raise UserError(_("Job not found."))
        if job.state != "completed":
            raise UserError(_("Feedback is available after delivery."))
        self.env["otm.cw.feedback"].create({"job_id": job.id, "partner_id": job.partner_id.id,
                                            "rating": to_int(rating) or 0, "comment": comment})
        return True

    # ----------------------------------------------------- aggregated views
    @api.model
    def customer_summary(self, partner_id=None):
        p = self.env["res.partner"].browse(to_int(partner_id)).exists()
        if not p:
            raise UserError(_("Customer not found."))
        Job = self.env["otm.cw.job"]
        now = fields.Datetime.now()
        agg = Job._read_group([("partner_id", "=", p.id), ("state", "!=", "cancelled")], [],
                              ["__count", "paid_amount:sum", "amount_total:sum", "arrival_dt:max"])[0]
        jobs = Job.search([("partner_id", "=", p.id)], limit=20)
        invoices = self.env["account.move"].sudo().search([("partner_id", "=", p.id), ("invoice_origin", "like", "CW-%")], limit=20)
        today = local_today(self.env)
        return {
            "customer": rec_to_dict(p, ["name", "otm_cw_mobile", "otm_cw_whatsapp", "email", "street",
                                        "otm_cw_customer_type", "otm_cw_discount"]),
            "totals": {"visits": agg[0], "revenue": round(agg[1] or 0.0, 2), "billed": round(agg[2] or 0.0, 2),
                       "last_visit": ser(agg[3])},
            "vehicles": [rec_to_dict(v, ["reg_no", "vehicle_type_id", "brand", "model", "color"]) for v in p.otm_cw_vehicle_ids],
            "jobs": [self._job_card(j, now) for j in jobs],
            "bookings": [rec_to_dict(b, RESOURCES["bookings"]["fields"]) for b in
                         self.env["otm.cw.booking"].search([("partner_id", "=", p.id)], limit=10)],
            "payments": [rec_to_dict(x, RESOURCES["payments"]["fields"]) for x in
                         self.env["otm.cw.payment"].search([("partner_id", "=", p.id)], limit=20)],
            "invoices": [{"id": m.id, "name": m.name, "total": m.amount_total, "state": m.state,
                          "payment_state": m.payment_state, "date": ser(m.invoice_date)} for m in invoices],
            "packages": [{"name": s.name, "package": s.package_id.name, "expiry": ser(s.expiry_date),
                          "expired": s.expiry_date < today,
                          "lines": [{"service": l.service_id.name, "total": l.total, "used": l.used, "remaining": l.remaining}
                                    for l in s.line_ids]} for s in
                         self.env["otm.cw.subscription"].search([("partner_id", "=", p.id)])],
            "feedback": [rec_to_dict(f, ["job_id", "rating", "comment"]) for f in
                         self.env["otm.cw.feedback"].search([("partner_id", "=", p.id)], limit=10)],
        }

    @api.model
    def vehicle_summary(self, vehicle_id=None):
        v = self.env["otm.cw.vehicle"].browse(to_int(vehicle_id)).exists()
        if not v:
            raise UserError(_("Vehicle not found."))
        Job = self.env["otm.cw.job"]
        now = fields.Datetime.now()
        agg = Job._read_group([("vehicle_id", "=", v.id), ("state", "!=", "cancelled")], [],
                              ["__count", "paid_amount:sum", "arrival_dt:max"])[0]
        jobs = Job.search([("vehicle_id", "=", v.id)], limit=30)
        current = jobs.filtered(lambda j: j.state in ACTIVE_STATES)[:1]
        insp = self.env["otm.cw.inspection"].search([("job_id.vehicle_id", "=", v.id), ("damage_notes", "!=", False)], limit=10)
        return {
            "vehicle": rec_to_dict(v, ["reg_no", "vehicle_type_id", "brand", "model", "color", "partner_id", "notes"]),
            "totals": {"total_washes": agg[0], "total_spent": round(agg[1] or 0.0, 2), "last_wash": ser(agg[2])},
            "current_job": self._job_card(current, now) if current else None,
            "jobs": [self._job_card(j, now) for j in jobs],
            "damage_history": [{"job": i.job_id.name, "kind": i.kind, "notes": i.damage_notes} for i in insp],
            "bookings": [rec_to_dict(b, RESOURCES["bookings"]["fields"]) for b in
                         self.env["otm.cw.booking"].search([("vehicle_id", "=", v.id), ("state", "in", ["pending", "confirmed", "arrived"])], limit=5)],
        }

    @api.model
    def search_all(self, q=""):
        q = (q or "").strip()
        if len(q) < 2:
            return {"vehicles": [], "customers": [], "jobs": [], "bookings": [], "invoices": [], "top": None}
        Vehicle, Partner, Job = self.env["otm.cw.vehicle"], self.env["res.partner"], self.env["otm.cw.job"]
        key = Vehicle.normalize_reg(q)
        vehicles = Vehicle.search([("reg_key", "ilike", key)], limit=5) if key else Vehicle
        mk = mobile_key(q)
        pdom = ["|", ("name", "ilike", q), ("otm_cw_mobile_key", "like", mk)] if mk and len(mk) >= 4 else [("name", "ilike", q)]
        customers = Partner.search([("otm_cw_is_customer", "=", True)] + pdom, limit=5)
        jdom = [("name", "ilike", q)]
        if q.lstrip("#").isdigit():
            jdom = ["|", ("name", "ilike", q), "&", ("token", "=", int(q.lstrip("#"))), ("token_date", "=", local_today(self.env))]
        jobs = Job.search(jdom, limit=5, order="id desc")
        bookings = self.env["otm.cw.booking"].search([("name", "ilike", q)], limit=5)
        invoices = self.env["account.move"].sudo().search([("move_type", "=", "out_invoice"), ("name", "ilike", q),
                                                    ("invoice_origin", "like", "CW-%")], limit=5)
        top = None
        if vehicles and key and vehicles[0].reg_key == key:
            top = self.vehicle_summary(vehicle_id=vehicles[0].id)
        return {
            "vehicles": [rec_to_dict(v, ["reg_no", "vehicle_type_id", "partner_id"]) for v in vehicles],
            "customers": [rec_to_dict(c, ["name", "otm_cw_mobile"]) for c in customers],
            "jobs": [rec_to_dict(j, ["name", "token", "state", "vehicle_id", "partner_id"]) for j in jobs],
            "bookings": [rec_to_dict(b, ["name", "state", "vehicle_id", "slot_dt"]) for b in bookings],
            "invoices": [{"id": m.id, "name": m.name, "total": m.amount_total} for m in invoices],
            "top": top,
        }

    @api.model
    def portal_home(self, partner_id=None):
        p = self.env["res.partner"].browse(to_int(partner_id)).exists()
        if not p:
            raise UserError(_("Customer not found."))
        now = fields.Datetime.now()
        Job = self.env["otm.cw.job"]
        active = Job.search([("partner_id", "=", p.id), ("state", "in", ACTIVE_STATES)], order="id desc")
        queued = Job.search([("state", "=", "queued")], order="arrival_dt, id")
        cards = [self._job_card(j, now, (queued.ids.index(j.id) + 1) if j.id in queued.ids else None) for j in active]
        for c, j in zip(cards, active):
            if j.state in ("washing", "paused") and j.start_dt:
                remaining = max(j.estimated_minutes * 60 - j.work_seconds, 0)
                c["eta"] = ser(now + timedelta(seconds=remaining))
        today = local_today(self.env)
        return {
            "name": p.name, "mobile": p.otm_cw_mobile or "",
            "notify": {"opt_in": p.otm_cw_notify_opt_in, "channel": p.otm_cw_notify_channel or "auto",
                       "telegram_linked": bool(p.otm_cw_telegram_chat_id),
                       "telegram_enabled": _np(self.env, "tg_enabled") == "1" and bool(_np(self.env, "tg_bot")),
                       "channels": self.env["otm.cw.notification"]._enabled_channels()},
            "active_jobs": cards,
            "vehicles": [rec_to_dict(v, ["reg_no", "vehicle_type_id", "brand", "model", "color"]) for v in p.otm_cw_vehicle_ids],
            "upcoming": [rec_to_dict(b, RESOURCES["bookings"]["fields"]) for b in self.env["otm.cw.booking"].search(
                [("partner_id", "=", p.id), ("state", "in", ["pending", "confirmed", "arrived"])], order="slot_dt", limit=5)],
            "packages": [{"name": s.name, "package": s.package_id.name, "expiry": ser(s.expiry_date),
                          "expired": s.expiry_date < today,
                          "lines": [{"service": l.service_id.name, "total": l.total, "used": l.used, "remaining": l.remaining}
                                    for l in s.line_ids]} for s in self.env["otm.cw.subscription"].search([("partner_id", "=", p.id)])],
            "balance_due": round(sum(Job.search([("partner_id", "=", p.id), ("state", "!=", "cancelled")]).mapped("balance")), 2),
        }

    @api.model
    def portal_profile_save(self, partner_id=None, values=None):
        p = self.env["res.partner"].browse(to_int(partner_id)).exists()
        if not p:
            raise UserError(_("Customer not found."))
        v = values or {}
        p.sudo().write({k: v[k] for k in ("name", "otm_cw_mobile", "otm_cw_whatsapp", "email", "street",
                                          "otm_cw_notify_opt_in", "otm_cw_notify_channel") if k in v})
        return True

    @api.model
    def portal_invoices(self, partner_id=None, offset=0, limit=25):
        domain = [("partner_id", "=", to_int(partner_id)), ("invoice_origin", "like", "CW-%")]
        Move = self.env["account.move"].sudo()
        return {"total": Move.search_count(domain),
                "items": [{"id": m.id, "name": m.name, "origin": m.invoice_origin, "total": m.amount_total,
                           "payment_state": m.payment_state, "date": ser(m.invoice_date)}
                          for m in Move.search(domain, order="id desc", offset=max(to_int(offset) or 0, 0),
                                               limit=min(to_int(limit) or 25, PAGE_MAX))]}
