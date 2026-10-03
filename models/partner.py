# -*- coding: utf-8 -*-
import re

from odoo import api, fields, models

from .master import CUSTOMER_TYPES

STAFF_ROLES = [
    ("washer", "Washer"),
    ("detailer", "Detailer"),
    ("supervisor", "Supervisor"),
    ("operator", "Bay Operator"),
]


def mobile_key(value):
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if digits else False


class ResPartner(models.Model):
    _inherit = "res.partner"

    otm_cw_is_customer = fields.Boolean(string="Car Wash Customer", index=True)
    otm_cw_customer_type = fields.Selection(CUSTOMER_TYPES, string="Car Wash Customer Type", default="standard")
    otm_cw_discount = fields.Float(string="Car Wash Discount %")
    otm_cw_mobile = fields.Char(string="Car Wash Mobile")
    otm_cw_mobile_key = fields.Char(compute="_compute_otm_cw_mobile_key", store=True, index=True)
    otm_cw_whatsapp = fields.Char(string="Car Wash WhatsApp")
    otm_cw_vehicle_ids = fields.One2many("otm.cw.vehicle", "partner_id", string="Registered Vehicles")

    @api.depends("otm_cw_mobile")
    def _compute_otm_cw_mobile_key(self):
        for rec in self:
            rec.otm_cw_mobile_key = mobile_key(rec.otm_cw_mobile)


class HrEmployee(models.Model):
    _inherit = "hr.employee"

    otm_cw_is_staff = fields.Boolean(string="Car Wash Staff", index=True)
    otm_cw_role = fields.Selection(STAFF_ROLES, string="Car Wash Role", default="washer")
    otm_cw_shift = fields.Char(string="Car Wash Shift")
    otm_cw_available = fields.Boolean(string="Car Wash Available", default=True)
    otm_cw_mobile = fields.Char(string="Car Wash Staff Mobile")
    otm_cw_service_ids = fields.Many2many(
        "otm.cw.service", "otm_cw_emp_service_rel", "employee_id", "service_id",
        string="Car Wash Skills")
