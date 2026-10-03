# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

PAY_METHODS = [("cash", "Cash"), ("upi", "UPI"), ("card", "Card"), ("online", "Online")]
EVENTS = [
    ("booking_confirmed", "Booking Confirmed"),
    ("vehicle_arrived", "Vehicle Arrived"),
    ("wash_started", "Wash Started"),
    ("vehicle_ready", "Vehicle Ready"),
    ("payment_pending", "Payment Pending"),
    ("payment_received", "Payment Received"),
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


class OtmCwNotification(models.Model):
    """Provider-agnostic notification outbox. A WhatsApp/SMS/email provider can
    be plugged into _send() later without touching business code."""
    _name = "otm.cw.notification"
    _description = "Car Wash Notification Outbox"
    _order = "id desc"

    event = fields.Selection(EVENTS, required=True, index=True)
    partner_id = fields.Many2one("res.partner", index=True)
    channel = fields.Selection([("whatsapp", "WhatsApp"), ("sms", "SMS"), ("email", "Email")], default="whatsapp")
    message = fields.Char()
    state = fields.Selection([("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed")], default="pending")

    @api.model
    def notify(self, event, partner, message=""):
        if not partner:
            return False
        return self.sudo().create({"event": event, "partner_id": partner.id, "message": message})

    def _send(self):
        """Provider hook. No provider is configured: records stay 'pending'."""
        return False


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
