# -*- coding: utf-8 -*-
import pytz

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from .misc import PAY_METHODS

JOB_STATES = [
    ("queued", "Queued"),
    ("assigned", "Assigned"),
    ("washing", "Washing"),
    ("paused", "Paused"),
    ("quality_check", "Quality Check"),
    ("ready", "Ready"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
]
JOB_FLOW = {
    "queued": {"assigned", "cancelled"},
    "assigned": {"washing", "queued", "cancelled"},
    "washing": {"paused", "quality_check"},
    "paused": {"washing", "cancelled"},
    "quality_check": {"ready", "washing"},
    "ready": {"completed"},
    "completed": set(),
    "cancelled": set(),
}
ACTIVE_STATES = ["queued", "assigned", "washing", "paused", "quality_check", "ready"]
JOB_TO_BOOKING = {
    "queued": "queued", "assigned": "assigned", "washing": "washing", "paused": "washing",
    "quality_check": "quality_check", "ready": "ready", "completed": "completed", "cancelled": "cancelled",
}
PRIORITIES = [("normal", "Normal"), ("priority", "Priority"), ("vip", "VIP"), ("prebooked", "Pre-booked")]
PARAM_DEFAULTS = {
    "warn_min": "15", "critical_min": "30", "open_hour": "8", "close_hour": "20",
    "tz": "Asia/Kolkata", "priority_mode": "fifo", "priority_order": "vip,prebooked,priority,normal",
    "auto_assign": "1",
}


def get_param(env, key):
    value = env["ir.config_parameter"].sudo().get_param("otm_cw." + key)
    return value if value not in (None, False, "") else PARAM_DEFAULTS[key]


def local_tz(env):
    try:
        return pytz.timezone(get_param(env, "tz"))
    except Exception:
        return pytz.timezone("Asia/Kolkata")


def local_today(env):
    return pytz.utc.localize(fields.Datetime.now()).astimezone(local_tz(env)).date()


def fmt_local(env, dt):
    if not dt:
        return ""
    return pytz.utc.localize(dt).astimezone(local_tz(env)).strftime("%I:%M %p")


class OtmCwJobLine(models.Model):
    _name = "otm.cw.job.line"
    _description = "Car Wash Job Service Line"

    job_id = fields.Many2one("otm.cw.job", required=True, ondelete="cascade", index=True)
    service_id = fields.Many2one("otm.cw.service", required=True)
    price = fields.Float(digits=(12, 2))
    duration = fields.Integer(string="Duration (min)")


class OtmCwJob(models.Model):
    _name = "otm.cw.job"
    _description = "Car Wash Job Card"
    _order = "id desc"

    name = fields.Char(string="Job No.", default="New", readonly=True, copy=False, index=True)
    token_date = fields.Date(index=True)
    token = fields.Integer(index=True)
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True)
    vehicle_id = fields.Many2one("otm.cw.vehicle", required=True, index=True)
    vehicle_type_id = fields.Many2one("otm.cw.vehicle.type", required=True, index=True)
    booking_id = fields.Many2one("otm.cw.booking", ondelete="set null")
    subscription_id = fields.Many2one("otm.cw.subscription", ondelete="set null")
    line_ids = fields.One2many("otm.cw.job.line", "job_id", string="Services")
    state = fields.Selection(JOB_STATES, default="queued", required=True, index=True)
    priority = fields.Selection(PRIORITIES, default="normal", required=True)
    notes = fields.Text()

    arrival_dt = fields.Datetime(string="Queue Entry", default=fields.Datetime.now, required=True, index=True)
    assigned_dt = fields.Datetime(index=True)
    start_dt = fields.Datetime(string="Wash Started", index=True)
    end_dt = fields.Datetime(string="Wash Finished", index=True)
    delivered_dt = fields.Datetime()
    last_resume_dt = fields.Datetime()
    work_seconds = fields.Integer(string="Work Duration (s)")
    wait_minutes = fields.Float(digits=(12, 2))
    bay_id = fields.Many2one("otm.cw.bay", index=True, ondelete="set null")
    assign_reason = fields.Char()
    assigned_staff_ids = fields.Many2many(
        "hr.employee", "otm_cw_job_assigned_rel", "job_id", "employee_id", string="Assigned Staff")
    actual_staff_ids = fields.Many2many(
        "hr.employee", "otm_cw_job_actual_rel", "job_id", "employee_id", string="Actual Staff")

    qc_passed = fields.Boolean(string="Quality Check Passed")
    qc_notes = fields.Text()

    discount_amount = fields.Float(digits=(12, 2))
    subtotal = fields.Float(compute="_compute_amounts", store=True, digits=(12, 2))
    amount_total = fields.Float(compute="_compute_amounts", store=True, digits=(12, 2))
    paid_amount = fields.Float(compute="_compute_amounts", store=True, digits=(12, 2))
    balance = fields.Float(compute="_compute_amounts", store=True, digits=(12, 2))
    estimated_minutes = fields.Integer(compute="_compute_amounts", store=True)
    payment_state = fields.Selection(
        [("unpaid", "Unpaid"), ("partial", "Partial"), ("paid", "Paid"), ("refunded", "Refunded")],
        compute="_compute_amounts", store=True, index=True)
    payment_ids = fields.One2many("otm.cw.payment", "job_id", string="Job Payments")
    invoice_id = fields.Many2one("account.move", string="Invoice", ondelete="set null")

    @api.depends("line_ids.price", "line_ids.duration", "discount_amount", "payment_ids.amount", "payment_ids.kind")
    def _compute_amounts(self):
        for job in self:
            sub = sum(job.line_ids.mapped("price"))
            total = max(sub - job.discount_amount, 0.0)
            paid = sum(p.amount for p in job.payment_ids if p.kind == "payment")
            refunded = sum(p.amount for p in job.payment_ids if p.kind == "refund")
            net = paid - refunded
            job.subtotal = sub
            job.amount_total = total
            job.paid_amount = net
            job.balance = max(total - net, 0.0)
            job.estimated_minutes = sum(job.line_ids.mapped("duration"))
            if refunded and net <= 0:
                job.payment_state = "refunded"
            elif total <= 0:
                job.payment_state = "paid"
            elif net <= 0:
                job.payment_state = "unpaid"
            elif net < total:
                job.payment_state = "partial"
            else:
                job.payment_state = "paid"

    # ------------------------------------------------------------------ ORM
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == "New":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("otm.cw.job") or "New"
        return super().create(vals_list)

    def write(self, vals):
        if "state" in vals and not self.env.context.get("cw_state_ok"):
            raise UserError(_("Job status can only be changed through workflow actions."))
        return super().write(vals)

    # ------------------------------------------------------------- workflow
    def _go(self, new, action, detail="", extra=None):
        for job in self:
            if new not in JOB_FLOW[job.state]:
                raise UserError(_("Job %(job)s cannot move from %(a)s to %(b)s.") % {
                    "job": job.name, "a": job.state, "b": new})
            vals = {"state": new}
            vals.update(extra or {})
            job.with_context(cw_state_ok=True).write(vals)
            if job.booking_id and JOB_TO_BOOKING.get(new):
                job.booking_id.with_context(cw_state_ok=True).write({"state": JOB_TO_BOOKING[new]})
            self.env["otm.cw.audit"].log(job, action, detail)

    @api.model
    def _next_token(self, day):
        self.env.cr.execute("SELECT pg_advisory_xact_lock(%s)", [48210731])
        last = self.search([("token_date", "=", day)], order="token desc", limit=1)
        return (last.token or 0) + 1

    @api.model
    def create_job(self, partner, vehicle, services, priority="normal", booking=None, subscription=None, notes=""):
        if not services:
            raise UserError(_("Select at least one service."))
        active = self.search([("vehicle_id", "=", vehicle.id), ("state", "in", ACTIVE_STATES)], limit=1)
        if active:
            raise UserError(_("This vehicle already has an active job (%s).") % active.name)
        Pricing = self.env["otm.cw.pricing"]
        lines = []
        for svc in services:
            price, source = Pricing.price_for(partner, vehicle.vehicle_type_id, svc)
            if source == "missing":
                raise UserError(_("No price is configured for %(s)s on %(v)s.") % {
                    "s": svc.name, "v": vehicle.vehicle_type_id.name})
            lines.append((0, 0, {"service_id": svc.id, "price": price, "duration": svc.duration}))
        subtotal = sum(l[2]["price"] for l in lines)
        today = local_today(self.env)
        job = self.create({
            "partner_id": partner.id, "vehicle_id": vehicle.id, "vehicle_type_id": vehicle.vehicle_type_id.id,
            "booking_id": booking.id if booking else False, "priority": priority, "notes": notes,
            "line_ids": lines, "discount_amount": subtotal * (partner.otm_cw_discount or 0.0) / 100.0,
            "token_date": today, "token": self._next_token(today),
        })
        if subscription:
            job._consume_package(subscription)
        self.env["otm.cw.audit"].log(job, "Vehicle checked in", "Token %s, priority %s" % (job.token, priority))
        self.env["otm.cw.notification"].notify("vehicle_arrived", partner, _("Vehicle %s checked in") % vehicle.reg_no, job=job)
        return job

    def _consume_package(self, sub):
        self.ensure_one()
        if sub.partner_id != self.partner_id:
            raise UserError(_("This package belongs to a different customer."))
        if not sub.is_usable():
            raise UserError(_("Package %s has expired.") % sub.name)
        picks = []
        for line in self.line_ids:
            sline = sub.line_ids.filtered(lambda l: l.service_id == line.service_id and l.remaining > 0)[:1]
            if not sline:
                raise UserError(_("Package has no remaining balance for %s.") % line.service_id.name)
            picks.append(sline)
        for sline in picks:
            sline.used += 1
            self.env["otm.cw.package.usage"].create({"line_id": sline.id, "job_id": self.id})
        self.write({"subscription_id": sub.id, "discount_amount": self.subtotal})
        self.env["otm.cw.audit"].log(self, "Package used", sub.name)

    def _release_package(self):
        for job in self:
            for usage in self.env["otm.cw.package.usage"].search([("job_id", "=", job.id)]):
                usage.line_id.used = max(usage.line_id.used - 1, 0)
                usage.unlink()

    # ------------------------------------------------------------- dispatch
    @api.model
    def _rank(self, job):
        order = [p.strip() for p in get_param(self.env, "priority_order").split(",") if p.strip()]
        return order.index(job.priority) if job.priority in order else len(order)

    @api.model
    def _pick_for_bay(self, bay):
        queued = self.search([("state", "=", "queued")], order="arrival_dt, id")
        eligible = queued.filtered(lambda j: bay.is_eligible(j))
        if not eligible:
            return None, ""
        first = eligible[0]
        reason = _("First eligible vehicle in queue (arrived %s)") % fmt_local(self.env, first.arrival_dt)
        if get_param(self.env, "priority_mode") != "priority":
            return first, reason
        best = min(eligible, key=lambda j: (self._rank(j), j.arrival_dt, j.id))
        if best != first:
            reason = _("%s priority: placed ahead of %s who arrived earlier") % (
                dict(PRIORITIES)[best.priority], first.vehicle_id.reg_no)
        else:
            reason = _("First eligible vehicle in queue (arrived %s)") % fmt_local(self.env, best.arrival_dt)
        return best, reason

    @api.model
    def dispatch(self):
        """Give each free bay the next eligible queued vehicle."""
        if get_param(self.env, "auto_assign") != "1":
            return 0
        count = 0
        for bay in self.env["otm.cw.bay"].search([("state", "=", "available")], order="number"):
            job, reason = self._pick_for_bay(bay)
            if job:
                job._assign_bay(bay, reason)
                count += 1
        return count

    def _assign_bay(self, bay, reason):
        self.ensure_one()
        if self.state != "queued":
            raise UserError(_("Only queued jobs can be assigned to a bay."))
        if bay.state != "available":
            raise UserError(_("%s is not available.") % bay.name)
        if not bay.is_eligible(self):
            raise UserError(_("%s cannot serve this vehicle type / service.") % bay.name)
        now = fields.Datetime.now()
        staff = bay.sudo().staff_ids.filtered("otm_cw_available")
        self._go("assigned", "Bay assigned", "%s — %s" % (bay.name, reason), {
            "bay_id": bay.id, "assigned_dt": now, "assign_reason": reason,
            "wait_minutes": (now - self.arrival_dt).total_seconds() / 60.0,
            "assigned_staff_ids": [(6, 0, staff.ids)],
        })
        bay.write({"state": "reserved", "current_job_id": self.id})
        self.env["otm.cw.notification"].notify("vehicle_assigned", self.partner_id, "", job=self)
        if staff:
            self.env["otm.cw.audit"].log(self, "Staff assigned", ", ".join(staff.mapped("name")))

    def _free_bay(self):
        for job in self:
            if job.bay_id and job.bay_id.current_job_id == job:
                job.bay_id.write({"state": "available", "current_job_id": False})

    # -------------------------------------------------------------- actions
    def action_assign(self, bay, manual_by=""):
        self.ensure_one()
        self._assign_bay(bay, _("Assigned manually by %s") % (manual_by or self.env.user.name))

    def action_unassign(self):
        self.ensure_one()
        if self.state != "assigned":
            raise UserError(_("Only assigned jobs can be returned to the queue."))
        bay = self.bay_id
        self._go("queued", "Returned to queue", bay.name, {
            "bay_id": False, "assigned_dt": False, "assign_reason": False, "assigned_staff_ids": [(5, 0, 0)]})
        if bay:
            bay.write({"state": "available", "current_job_id": False})

    def action_start(self, employee_ids=None):
        self.ensure_one()
        staff = self.env["hr.employee"].sudo().browse(employee_ids or self.assigned_staff_ids.ids).exists()
        staff = staff.filtered("otm_cw_available")
        if not staff:
            raise UserError(_("No available staff selected for this wash."))
        now = fields.Datetime.now()
        self._go("washing", "Wash started", ", ".join(staff.mapped("name")), {
            "start_dt": now, "last_resume_dt": now, "actual_staff_ids": [(6, 0, staff.ids)],
            "end_dt": False, "qc_passed": False})
        self.bay_id.write({"state": "busy"})
        self.env["otm.cw.notification"].notify("wash_started", self.partner_id, "", job=self)

    def _bank_time(self):
        self.ensure_one()
        if self.last_resume_dt:
            self.work_seconds += int((fields.Datetime.now() - self.last_resume_dt).total_seconds())

    def action_pause(self):
        self.ensure_one()
        if self.state != "washing":
            raise UserError(_("Only a running wash can be paused."))
        self._bank_time()
        self._go("paused", "Wash paused", "", {"last_resume_dt": False})

    def action_resume(self):
        self.ensure_one()
        self._go("washing", "Wash resumed", "", {"last_resume_dt": fields.Datetime.now()})

    def action_finish_wash(self):
        """Stop the timer and send the vehicle to quality check; frees the bay."""
        self.ensure_one()
        if self.state != "washing":
            raise UserError(_("Resume the wash before completing it."))
        self._bank_time()
        self._go("quality_check", "Wash completed", "%s min actual / %s min estimated" % (
            round(self.work_seconds / 60.0, 1), self.estimated_minutes),
            {"end_dt": fields.Datetime.now(), "last_resume_dt": False})
        self._free_bay()

    def action_qc(self, passed, notes="", by=""):
        self.ensure_one()
        if self.state != "quality_check":
            raise UserError(_("Job is not in quality check."))
        if not passed:
            if self.bay_id.state != "available":
                raise UserError(_("%s is occupied; re-wash needs a free bay.") % self.bay_id.name)
            self._go("washing", "Quality check failed — re-wash", notes, {
                "last_resume_dt": fields.Datetime.now(), "qc_passed": False, "qc_notes": notes, "end_dt": False})
            self.bay_id.write({"state": "busy", "current_job_id": self.id})
            return
        self.write({"qc_passed": True, "qc_notes": notes})
        self.env["otm.cw.audit"].log(self, "Quality check passed", by or notes)

    def action_ready(self):
        self.ensure_one()
        if not self.qc_passed:
            raise UserError(_("Quality check must pass before marking ready."))
        self._go("ready", "Vehicle ready", "")
        self.env["otm.cw.notification"].notify("vehicle_ready", self.partner_id, "", job=self)
        if self.balance > 0:
            self.env["otm.cw.notification"].notify("payment_pending", self.partner_id, "", job=self)

    def action_deliver(self):
        self.ensure_one()
        if self.payment_state != "paid":
            raise UserError(_("Payment is pending. Collect the balance before delivery."))
        self._go("completed", "Vehicle delivered", "", {"delivered_dt": fields.Datetime.now()})
        self.env["otm.cw.notification"].notify("delivered", self.partner_id, "", job=self)

    def action_cancel(self, reason=""):
        self.ensure_one()
        bay = self.bay_id
        was_assigned = self.state == "assigned"
        self._go("cancelled", "Job cancelled", reason)
        self._release_package()
        if was_assigned and bay:
            bay.write({"state": "available", "current_job_id": False})

    def action_add_payment(self, amount, method, reference="", kind="payment", by=""):
        self.ensure_one()
        if self.state == "cancelled":
            raise UserError(_("Cannot take payment on a cancelled job."))
        if kind == "payment" and amount > self.balance + 0.005:
            raise UserError(_("Amount exceeds the outstanding balance (%.2f).") % self.balance)
        if kind == "refund" and amount > self.paid_amount + 0.005:
            raise UserError(_("Refund exceeds the amount paid (%.2f).") % self.paid_amount)
        pay = self.env["otm.cw.payment"].create({
            "job_id": self.id, "amount": amount, "method": method, "kind": kind,
            "reference": reference, "received_by": by or self.env.context.get("cw_actor") or self.env.user.name})
        self.env["otm.cw.audit"].log(self, "Payment received" if kind == "payment" else "Refund issued",
                                     "%s %.2f" % (method, amount))
        if kind == "payment":
            self.env["otm.cw.notification"].notify("payment_received", self.partner_id, _("Received %.2f") % amount)
        return pay

    def action_create_invoice(self):
        self.ensure_one()
        if self.invoice_id:
            return self.invoice_id
        lines = [(0, 0, {"name": "%s — %s" % (l.service_id.name, self.vehicle_id.reg_no),
                         "quantity": 1, "price_unit": l.price}) for l in self.line_ids]
        if self.discount_amount:
            lines.append((0, 0, {"name": _("Discount"), "quantity": 1, "price_unit": -self.discount_amount}))
        try:
            with self.env.cr.savepoint():
                move = self.env["account.move"].sudo().create({
                    "move_type": "out_invoice", "partner_id": self.partner_id.id,
                    "invoice_origin": self.name, "invoice_line_ids": lines})
                move.action_post()
        except Exception as exc:  # accounting may not be configured
            raise UserError(_("Could not create the invoice. Check the accounting configuration.")) from exc
        self.invoice_id = move.id
        self.env["otm.cw.audit"].log(self, "Invoice created", move.name)
        return move
