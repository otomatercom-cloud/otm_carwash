# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

CUSTOMER_TYPES = [
    ("standard", "Standard"),
    ("vip", "VIP"),
    ("corporate", "Corporate"),
    ("membership", "Membership"),
]
BAY_STATES = [
    ("available", "Available"),
    ("reserved", "Reserved"),
    ("busy", "Busy"),
    ("cleaning", "Cleaning"),
    ("maintenance", "Maintenance"),
    ("offline", "Offline"),
]


class OtmCwVehicleType(models.Model):
    _name = "otm.cw.vehicle.type"
    _description = "Car Wash Vehicle Type"
    _order = "sequence, name"

    name = fields.Char(required=True)
    code = fields.Char(required=True)
    description = fields.Text()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint("unique(code)", "Vehicle type code must be unique.")


class OtmCwService(models.Model):
    _name = "otm.cw.service"
    _description = "Car Wash Service"
    _order = "sequence, name"

    name = fields.Char(string="Service Name", required=True)
    code = fields.Char(required=True)
    description = fields.Text()
    duration = fields.Integer(string="Duration (min)", default=30, required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _code_uniq = models.Constraint("unique(code)", "Service code must be unique.")

    @api.constrains("duration")
    def _check_duration(self):
        for rec in self:
            if rec.duration <= 0:
                raise ValidationError(_("Duration must be greater than zero."))


class OtmCwPricing(models.Model):
    _name = "otm.cw.pricing"
    _description = "Car Wash Pricing Matrix Cell"
    _order = "service_id, vehicle_type_id, customer_type"

    service_id = fields.Many2one("otm.cw.service", required=True, ondelete="cascade", index=True)
    vehicle_type_id = fields.Many2one("otm.cw.vehicle.type", required=True, ondelete="cascade", index=True)
    customer_type = fields.Selection(CUSTOMER_TYPES, default="standard", required=True)
    price = fields.Float(digits=(12, 2), required=True)

    _cell_uniq = models.Constraint(
        "unique(service_id, vehicle_type_id, customer_type)",
        "A price already exists for this service, vehicle type and customer type.",
    )

    @api.constrains("price")
    def _check_price(self):
        for rec in self:
            if rec.price < 0:
                raise ValidationError(_("Price cannot be negative."))

    @api.model
    def price_for(self, partner, vehicle_type, service):
        """Return (price, source). Customer-type price wins, standard matrix is the default."""
        ctype = (partner.otm_cw_customer_type if partner else False) or "standard"
        domain = [("service_id", "=", service.id), ("vehicle_type_id", "=", vehicle_type.id)]
        cells = self.search(domain + [("customer_type", "in", list({ctype, "standard"}))])
        by_type = {c.customer_type: c.price for c in cells}
        if ctype in by_type:
            return by_type[ctype], ctype
        if "standard" in by_type:
            return by_type["standard"], "standard"
        return 0.0, "missing"


class OtmCwVehicle(models.Model):
    _name = "otm.cw.vehicle"
    _description = "Car Wash Vehicle"
    _rec_name = "reg_no"
    _order = "reg_no"

    reg_no = fields.Char(string="Registration Number", required=True)
    reg_key = fields.Char(compute="_compute_reg_key", store=True, index=True)
    vehicle_type_id = fields.Many2one("otm.cw.vehicle.type", required=True)
    brand = fields.Char()
    model = fields.Char()
    color = fields.Char()
    partner_id = fields.Many2one("res.partner", string="Customer", required=True, index=True, ondelete="restrict")
    notes = fields.Text()
    active = fields.Boolean(default=True)

    _reg_uniq = models.Constraint("unique(reg_key)", "A vehicle with this registration number already exists.")

    @api.depends("reg_no")
    def _compute_reg_key(self):
        for rec in self:
            rec.reg_key = self.normalize_reg(rec.reg_no)

    @staticmethod
    def normalize_reg(value):
        return re.sub(r"[^A-Z0-9]", "", (value or "").upper())

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("reg_no"):
                vals["reg_no"] = vals["reg_no"].strip().upper()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("reg_no"):
            vals["reg_no"] = vals["reg_no"].strip().upper()
        return super().write(vals)


class OtmCwBay(models.Model):
    _name = "otm.cw.bay"
    _description = "Car Wash Bay"
    _order = "number"

    name = fields.Char(string="Bay Name", required=True)
    number = fields.Integer(string="Bay Number", required=True)
    state = fields.Selection(BAY_STATES, default="available", required=True, index=True)
    vehicle_type_ids = fields.Many2many(
        "otm.cw.vehicle.type", "otm_cw_bay_vtype_rel", "bay_id", "vehicle_type_id",
        string="Supported Vehicle Types (empty = all)")
    service_ids = fields.Many2many(
        "otm.cw.service", "otm_cw_bay_service_rel", "bay_id", "service_id",
        string="Supported Services (empty = all)")
    staff_ids = fields.Many2many(
        "hr.employee", "otm_cw_bay_employee_rel", "bay_id", "employee_id", string="Default Staff")
    current_job_id = fields.Many2one("otm.cw.job", string="Current Job", ondelete="set null")
    active = fields.Boolean(default=True)

    _number_uniq = models.Constraint("unique(number)", "Bay number must be unique.")

    def is_eligible(self, job):
        self.ensure_one()
        if self.vehicle_type_ids and job.vehicle_type_id not in self.vehicle_type_ids:
            return False
        if self.service_ids and not (job.line_ids.mapped("service_id") <= self.service_ids):
            return False
        return True
