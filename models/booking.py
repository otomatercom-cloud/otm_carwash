# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

BOOKING_STATES = [
    ("pending", "Pending"), ("confirmed", "Confirmed"), ("arrived", "Arrived"),
    ("checked_in", "Checked In"), ("queued", "Queued"), ("assigned", "Assigned"),
    ("washing", "Washing"), ("quality_check", "Quality Check"), ("ready", "Ready"),
    ("completed", "Completed"), ("cancelled", "Cancelled"), ("no_show", "No Show"),
]
BOOKING_FLOW = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"arrived", "checked_in", "cancelled", "no_show"},
    "arrived": {"checked_in", "cancelled", "no_show"},
    "checked_in": {"queued"},
}


class OtmCwBookingLine(models.Model):
    _name = "otm.cw.booking.line"
    _description = "Car Wash Booking Service Line"

    booking_id = fields.Many2one("otm.cw.booking", required=True, ondelete="cascade", index=True)
    service_id = fields.Many2one("otm.cw.service", required=True)
    price = fields.Float(digits=(12, 2))


class OtmCwBooking(models.Model):
    _name = "otm.cw.booking"
    _description = "Car Wash Booking"
    _order = "slot_dt desc, id desc"

    name = fields.Char(string="Booking No.", default="New", readonly=True, copy=False, index=True)
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True)
    vehicle_id = fields.Many2one("otm.cw.vehicle", required=True, index=True)
    vehicle_type_id = fields.Many2one("otm.cw.vehicle.type", related="vehicle_id.vehicle_type_id", store=True)
    slot_dt = fields.Datetime(string="Slot", required=True, index=True)
    line_ids = fields.One2many("otm.cw.booking.line", "booking_id", string="Services")
    amount_total = fields.Float(compute="_compute_total", store=True, digits=(12, 2))
    state = fields.Selection(BOOKING_STATES, default="pending", required=True, index=True)
    source = fields.Selection([("portal", "Customer Portal"), ("reception", "Reception")], default="reception")
    job_id = fields.Many2one("otm.cw.job", ondelete="set null")
    notes = fields.Text()

    @api.depends("line_ids.price")
    def _compute_total(self):
        for rec in self:
            rec.amount_total = sum(rec.line_ids.mapped("price"))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == "New":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("otm.cw.booking") or "New"
        return super().create(vals_list)

    @api.model
    def create_booking(self, partner, vehicle, services, slot_dt, source="reception", notes=""):
        if not services:
            raise UserError(_("Select at least one service."))
        if slot_dt < fields.Datetime.now() - timedelta(minutes=5):
            raise UserError(_("The booking time is in the past."))
        Pricing = self.env["otm.cw.pricing"]
        lines = []
        for svc in services:
            price, source_type = Pricing.price_for(partner, vehicle.vehicle_type_id, svc)
            if source_type == "missing":
                raise UserError(_("No price is configured for %s.") % svc.name)
            lines.append((0, 0, {"service_id": svc.id, "price": price}))
        booking = self.create({
            "partner_id": partner.id, "vehicle_id": vehicle.id, "slot_dt": slot_dt, "line_ids": lines,
            "source": source, "notes": notes,
            "state": "confirmed" if source == "reception" else "pending"})
        self.env["otm.cw.audit"].log(booking, "Booking created", booking.slot_dt.isoformat())
        if booking.state == "confirmed":
            self.env["otm.cw.notification"].notify("booking_confirmed", partner, booking.name)
        return booking

    def _move(self, new, action, detail=""):
        for rec in self:
            if new not in BOOKING_FLOW.get(rec.state, set()):
                raise UserError(_("Booking %(n)s cannot move from %(a)s to %(b)s.") % {
                    "n": rec.name, "a": rec.state, "b": new})
            rec.state = new
            self.env["otm.cw.audit"].log(rec, action, detail)

    def action_confirm(self):
        self._move("confirmed", "Booking confirmed")
        for rec in self:
            self.env["otm.cw.notification"].notify("booking_confirmed", rec.partner_id, rec.name)

    def action_arrive(self):
        self._move("arrived", "Customer arrived")
        for rec in self:
            self.env["otm.cw.notification"].notify("vehicle_arrived", rec.partner_id, rec.name)

    def action_cancel(self, reason=""):
        self._move("cancelled", "Booking cancelled", reason)

    def action_no_show(self):
        self._move("no_show", "Marked no-show")

    def action_check_in(self):
        self.ensure_one()
        self._move("checked_in", "Vehicle checked in")
        job = self.env["otm.cw.job"].create_job(
            self.partner_id, self.vehicle_id, self.line_ids.mapped("service_id"),
            priority="prebooked", booking=self, notes=self.notes or "")
        self.job_id = job.id
        self._move("queued", "Added to queue", job.name)
        return job
