# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

PAY_METHODS = [("cash", "Cash"), ("upi", "UPI"), ("card", "Card"), ("online", "Online")]
EVENTS = [
    ("booking_confirmed", "Booking Confirmed"),
    ("vehicle_arrived", "Vehicle Arrived"),
    ("vehicle_assigned", "Vehicle Assigned to Bay"),
    ("wash_started", "Wash Started"),
    ("vehicle_ready", "Vehicle Ready"),
    ("payment_pending", "Payment Pending"),
    ("payment_received", "Payment Received"),
    ("delivered", "Vehicle Delivered"),
    ("package_expiring", "Package Expiring"),
    ("service_reminder", "Service Reminder"),
]


class OtmCwPayment(models.Model):
    _name = "otm.cw.payment"
    _description = "Car Wash Payment"
    _order = "dt desc, id desc"

    job_id = fields.Many2one("otm.cw.job", required=True, ondelete="cascade", index=True)
    partner_id = fields.Many2one("res.partner", related="job_id.partner_id", store=True, index=True)
    amount = fields.Float(digits=(12, 2), required=True)
    method = fields.Selection(PAY_METHODS, required=True, default="cash")
    kind = fields.Selection([("payment", "Payment"), ("refund", "Refund")], default="payment", required=True)
    signed_amount = fields.Float(compute="_compute_signed", store=True, digits=(12, 2))
    reference = fields.Char()
    dt = fields.Datetime(string="Paid On", default=fields.Datetime.now, index=True)
    received_by = fields.Char()

    @api.depends("amount", "kind")
    def _compute_signed(self):
        for rec in self:
            rec.signed_amount = rec.amount if rec.kind == "payment" else -rec.amount

    @api.constrains("amount")
    def _check_amount(self):
        for rec in self:
            if rec.amount <= 0:
                raise ValidationError(_("Payment amount must be greater than zero."))


class OtmCwInspection(models.Model):
    _name = "otm.cw.inspection"
    _description = "Car Wash Vehicle Inspection"
    _order = "id desc"

    job_id = fields.Many2one("otm.cw.job", required=True, ondelete="cascade", index=True)
    kind = fields.Selection([("before", "Before Wash"), ("after", "After Wash")], required=True)
    photo_front = fields.Binary(attachment=True)
    photo_rear = fields.Binary(attachment=True)
    photo_left = fields.Binary(attachment=True)
    photo_right = fields.Binary(attachment=True)
    damage_notes = fields.Text()
    customer_notes = fields.Text()
    accessories = fields.Text()
    staff_confirmed = fields.Boolean()
    customer_confirmed = fields.Boolean()
    done_by = fields.Char()


class OtmCwAudit(models.Model):
    _name = "otm.cw.audit"
    _description = "Car Wash Audit Trail"
    _order = "id desc"

    res_model = fields.Char(index=True)
    res_id = fields.Integer(index=True)
    ref = fields.Char(string="Reference")
    action = fields.Char(required=True)
    detail = fields.Char()
    actor = fields.Char(string="Who")
    dt = fields.Datetime(string="When", default=fields.Datetime.now, index=True)

    @api.model
    def log(self, record, action, detail=""):
        actor = self.env.context.get("cw_actor") or self.env.user.name
        self.sudo().create({
            "res_model": record._name, "res_id": record.id,
            "ref": record.display_name, "action": action, "detail": detail, "actor": actor,
        })


class OtmCwFeedback(models.Model):
    _name = "otm.cw.feedback"
    _description = "Car Wash Customer Feedback"
    _order = "id desc"

    job_id = fields.Many2one("otm.cw.job", required=True, ondelete="cascade", index=True)
    partner_id = fields.Many2one("res.partner", index=True)
    rating = fields.Integer(required=True)
    comment = fields.Text()

    @api.constrains("rating")
    def _check_rating(self):
        for rec in self:
            if not 1 <= rec.rating <= 5:
                raise ValidationError(_("Rating must be between 1 and 5."))
