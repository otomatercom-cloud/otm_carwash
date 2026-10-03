# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class OtmCwPackage(models.Model):
    _name = "otm.cw.package"
    _description = "Car Wash Package"
    _order = "name"

    name = fields.Char(required=True)
    price = fields.Float(digits=(12, 2), required=True)
    validity_days = fields.Integer(default=30, required=True)
    line_ids = fields.One2many("otm.cw.package.line", "package_id", string="Included Services")
    active = fields.Boolean(default=True)


class OtmCwPackageLine(models.Model):
    _name = "otm.cw.package.line"
    _description = "Car Wash Package Line"

    package_id = fields.Many2one("otm.cw.package", required=True, ondelete="cascade")
    service_id = fields.Many2one("otm.cw.service", required=True)
    qty = fields.Integer(string="Quantity", default=1, required=True)


class OtmCwSubscription(models.Model):
    _name = "otm.cw.subscription"
    _description = "Car Wash Package Subscription"
    _order = "id desc"

    name = fields.Char(default="New", readonly=True, copy=False)
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True)
    package_id = fields.Many2one("otm.cw.package", required=True)
    start_date = fields.Date(default=fields.Date.context_today, required=True)
    expiry_date = fields.Date(required=True, index=True)
    amount = fields.Float(digits=(12, 2))
    payment_method = fields.Selection(
        [("cash", "Cash"), ("upi", "UPI"), ("card", "Card"), ("online", "Online")], default="cash")
    line_ids = fields.One2many("otm.cw.subscription.line", "subscription_id", string="Service Balances")
    expiry_notified = fields.Boolean(default=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("name") or vals["name"] == "New":
                vals["name"] = self.env["ir.sequence"].sudo().next_by_code("otm.cw.subscription") or "New"
        return super().create(vals_list)

    @api.model
    def sell(self, partner, package, method="cash"):
        today = fields.Date.context_today(self)
        return self.create({
            "partner_id": partner.id,
            "package_id": package.id,
            "start_date": today,
            "expiry_date": today + timedelta(days=package.validity_days),
            "amount": package.price,
            "payment_method": method,
            "line_ids": [(0, 0, {"service_id": l.service_id.id, "total": l.qty}) for l in package.line_ids],
        })

    def is_usable(self):
        self.ensure_one()
        return self.expiry_date >= fields.Date.context_today(self)

    @api.model
    def cron_package_expiry(self):
        today = fields.Date.context_today(self)
        soon = self.search([("expiry_date", ">=", today), ("expiry_date", "<=", today + timedelta(days=3)),
                            ("expiry_notified", "=", False)])
        for sub in soon:
            if sum(sub.line_ids.mapped("remaining")) > 0:
                self.env["otm.cw.notification"].notify("package_expiring", sub.partner_id,
                                                       message=_("Package %s expires on %s") % (sub.name, sub.expiry_date))
            sub.expiry_notified = True


class OtmCwSubscriptionLine(models.Model):
    _name = "otm.cw.subscription.line"
    _description = "Car Wash Subscription Balance"

    subscription_id = fields.Many2one("otm.cw.subscription", required=True, ondelete="cascade", index=True)
    service_id = fields.Many2one("otm.cw.service", required=True)
    total = fields.Integer(required=True)
    used = fields.Integer(default=0)
    remaining = fields.Integer(compute="_compute_remaining", store=True)

    @api.depends("total", "used")
    def _compute_remaining(self):
        for rec in self:
            rec.remaining = rec.total - rec.used


class OtmCwPackageUsage(models.Model):
    _name = "otm.cw.package.usage"
    _description = "Car Wash Package Usage"

    line_id = fields.Many2one("otm.cw.subscription.line", required=True, ondelete="cascade")
    job_id = fields.Many2one("otm.cw.job", required=True, ondelete="cascade", index=True)
    dt = fields.Datetime(default=fields.Datetime.now)
