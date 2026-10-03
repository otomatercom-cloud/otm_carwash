# -*- coding: utf-8 -*-
"""Optional DEMO data. Everything created here is registered under the xml-id module
'otm_carwash_demo' so clear_demo() removes exactly what seed_demo() made."""
import random
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError

DEMO = "otm_carwash_demo"
DEMO_PASSWORD = "Demo@12345"
STAFF = ["Arun", "Rahul", "Vishnu", "Anil", "Sajeev", "Manoj", "Biju", "Nikhil", "Shibu", "Jose"]
CUSTOMERS = ["Rahul Menon", "Anita Nair", "Faisal Khan", "Sreeja Pillai", "Joseph Mathew", "Divya Raj", "Nikhil Das",
             "Meera Iyer", "Suresh Kumar", "Priya Varma", "Thomas George", "Lakshmi S", "Ajmal Rahman", "Neha Joseph",
             "Vinod Kurian", "Asha Menon", "Basil Paul", "Remya K", "Gokul Krishna", "Fathima Beevi"]
BRANDS = [("Maruti", "Swift", "Hatchback"), ("Honda", "City", "Sedan"), ("Hyundai", "Creta", "SUV"),
          ("Toyota", "Fortuner", "SUV"), ("Hyundai", "i20", "Hatchback"), ("Skoda", "Octavia", "Sedan"),
          ("BMW", "X5", "Luxury"), ("Maruti", "Eeco", "Van"), ("Tata", "Nexon", "SUV")]
BASE = {"FULL": 300, "UNDER": 150, "INT": 250, "FOAM": 200, "EXT": 180, "VAC": 100, "WAX": 350, "POLISH": 800}
FACTOR = {"HATCH": 1.0, "SEDAN": 1.33, "SUV": 1.66, "LUX": 2.33, "VAN": 1.5, "OTHER": 1.2}


class OtmCwSeed(models.AbstractModel):
    _name = "otm.cw.seed"
    _description = "Car Wash Demo Data"

    def _reg(self, rec, key):
        self.env["ir.model.data"].sudo().create({
            "module": DEMO, "name": "%s_%s" % (rec._name.replace(".", "_"), key), "model": rec._name,
            "res_id": rec.id, "noupdate": True})
        return rec

    def _demo_users(self, operator_emp, customer_partner):
        """One login per role (password DEMO_PASSWORD). Tagged demo, removed by clear_demo()."""
        env, Users = self.env, self.env["res.users"].sudo()
        g = lambda x: env.ref(x).id
        specs = [("manager", "Demo Manager", "otm_carwash.group_cw_manager"),
                 ("reception", "Demo Reception", "otm_carwash.group_cw_reception"),
                 ("operator", "Demo Operator", "otm_carwash.group_cw_operator"),
                 ("customer", "Demo Customer", "base.group_portal")]
        out = []
        for key, name, grp in specs:
            login = "%s@carwash.demo" % key
            vals = {"name": name, "login": login, "email": login, "password": DEMO_PASSWORD,
                    "group_ids": [(6, 0, [g(grp)])]}
            if key == "customer" and customer_partner:
                vals.update(partner_id=customer_partner.id)
            user = Users.search([("login", "=", login)], limit=1) or Users.create(vals)
            if key == "operator":
                operator_emp.sudo().write({"user_id": user.id})
            if key == "customer" and customer_partner:
                customer_partner.sudo().write({"otm_cw_is_customer": True})
            self._reg(user, key)
            out.append(login)
        return out

    @api.model
    def seed_demo(self):
        if self.env["ir.model.data"].sudo().search_count([("module", "=", DEMO)]):
            raise UserError(_("Demo data is already loaded. Clear it first."))
        self = self.sudo()  # API service user has no HR rights
        rnd = random.Random(7)
        env = self.env  # (self is sudo: demo creates hr.employee/resource records)
        Svc, VT = env["otm.cw.service"], env["otm.cw.vehicle.type"]
        services, vtypes = {s.code: s for s in Svc.search([])}, {v.code: v for v in VT.search([])}
        # pricing matrix (real config rows, not tagged demo, only created where missing)
        Pricing = env["otm.cw.pricing"]
        for code, price in BASE.items():
            for vcode, factor in FACTOR.items():
                if code in services and vcode in vtypes and not Pricing.search_count([
                        ("service_id", "=", services[code].id), ("vehicle_type_id", "=", vtypes[vcode].id),
                        ("customer_type", "=", "standard")]):
                    Pricing.create({"service_id": services[code].id, "vehicle_type_id": vtypes[vcode].id,
                                    "customer_type": "standard", "price": round(price * factor / 10) * 10})
        staff = [self._reg(env["hr.employee"].create({
            "name": n, "otm_cw_is_staff": True, "otm_cw_role": "washer", "otm_cw_shift": "09:00 - 17:00",
            "otm_cw_mobile": "98470001%02d" % i,
            "otm_cw_service_ids": [(6, 0, [s.id for s in services.values()])]}), "s%d" % i)
            for i, n in enumerate(STAFF)]
        bays = env["otm.cw.bay"].search([("active", "=", True)], order="number")
        for i, bay in enumerate(bays):
            bay.staff_ids = [(6, 0, [staff[(2 * i) % 10].id, staff[(2 * i + 1) % 10].id])]
        partners = [self._reg(env["res.partner"].create({
            "name": n, "otm_cw_is_customer": True, "otm_cw_mobile": "98460%05d" % (1000 + i),
            "phone": "98460%05d" % (1000 + i), "otm_cw_customer_type": "vip" if i % 9 == 0 else "standard"}), "c%d" % i)
            for i, n in enumerate(CUSTOMERS)]
        vehicles = []
        for i in range(25):
            brand, model, vt = BRANDS[i % len(BRANDS)]
            vehicles.append(self._reg(env["otm.cw.vehicle"].create({
                "reg_no": "KL07 %s %04d" % (chr(65 + i % 26) + chr(66 + i % 24), 1200 + i * 37),
                "vehicle_type_id": vtypes[{"Hatchback": "HATCH", "Sedan": "SEDAN", "SUV": "SUV", "Luxury": "LUX",
                                           "Van": "VAN"}[vt]].id,
                "brand": brand, "model": model, "color": rnd.choice(["White", "Black", "Silver", "Red", "Blue"]),
                "partner_id": partners[i % 20].id}), "v%d" % i))
        Job = env["otm.cw.job"]
        now = fields.Datetime.now()
        combos = [["FULL"], ["FULL", "UNDER"], ["INT"], ["FOAM"], ["EXT", "VAC"], ["FULL", "INT"]]
        jobs = []
        for i in range(15):
            veh = vehicles[i]
            svcs = env["otm.cw.service"].browse([services[c].id for c in combos[i % len(combos)]])
            job = Job.create_job(veh.partner_id, veh, svcs, "vip" if i == 3 else "normal")
            jobs.append(self._reg(job, "j%d" % i))
        state_plan = ["done"] * 3 + ["ready"] + ["washing"] * 4 + ["queued"] * 7
        active_bays = [b for b in bays if b.number != 4]
        wi = 0
        for i, (job, plan) in enumerate(zip(jobs, state_plan)):
            arrival = now - timedelta(minutes=300 - i * 17)
            job.arrival_dt = arrival
            if plan == "queued":
                job.arrival_dt = now - timedelta(minutes=5 + (14 - i) * 4)
                continue
            if plan == "washing":
                bay = active_bays[wi % len(active_bays)]
                wi += 1
                job._assign_bay(bay, _("Demo assignment"))
                job.action_start()
                job.write({"assigned_dt": arrival + timedelta(minutes=6), "wait_minutes": 6.0,
                           "start_dt": now - timedelta(minutes=8 + i), "last_resume_dt": now - timedelta(minutes=8 + i)})
                continue
            bay = active_bays[i % len(active_bays)]
            job._assign_bay(bay, _("Demo assignment"))
            job.action_start()
            job.action_finish_wash()
            job.action_qc(True, "OK")
            job.action_ready()
            minutes = job.estimated_minutes + rnd.randint(-5, 5)
            job.write({"assigned_dt": arrival + timedelta(minutes=8 + i), "wait_minutes": 8.0 + i,
                       "start_dt": arrival + timedelta(minutes=10 + i),
                       "end_dt": arrival + timedelta(minutes=10 + i + minutes), "work_seconds": minutes * 60})
            if plan == "done":
                job.action_add_payment(job.balance, rnd.choice(["cash", "upi", "card"]))
                job.action_deliver()
                job.write({"delivered_dt": job.end_dt + timedelta(minutes=10)})
            for pay in job.payment_ids:
                pay.dt = job.end_dt + timedelta(minutes=8)
        bays.filtered(lambda b: b.number == 4).write({"state": "maintenance"})
        users = self._demo_users(staff[0], partners[0])
        return {"staff": len(staff), "customers": len(partners), "vehicles": len(vehicles), "jobs": len(jobs), "users": users}

    @api.model
    def clear_demo(self):
        self = self.sudo()
        data = self.env["ir.model.data"].sudo().search([("module", "=", DEMO)])
        ids = {}
        for d in data:
            ids.setdefault(d.model, []).append(d.res_id)
        job_ids = ids.get("otm.cw.job", [])
        self.env["otm.cw.audit"].sudo().search([("res_model", "=", "otm.cw.job"), ("res_id", "in", job_ids)]).unlink()
        self.env["otm.cw.notification"].sudo().search([("partner_id", "in", ids.get("res.partner", []))]).unlink()
        jobs = self.env["otm.cw.job"].browse(job_ids).exists()
        jobs.mapped("bay_id").filtered(lambda b: b.current_job_id in jobs).write({"state": "available", "current_job_id": False})
        jobs.unlink()
        self.env["otm.cw.vehicle"].browse(ids.get("otm.cw.vehicle", [])).exists().unlink()
        self.env["hr.employee"].browse(ids.get("hr.employee", [])).exists().unlink()
        users = self.env["res.users"].sudo().browse(ids.get("res.users", [])).exists()
        users.write({"active": False})
        users.unlink()
        self.env["res.partner"].browse(ids.get("res.partner", [])).exists().unlink()
        data.unlink()
        self.env["otm.cw.bay"].search([("state", "=", "maintenance")]).write({"state": "available"})
        return True

    @api.model
    def demo_loaded(self):
        return self.env["ir.model.data"].sudo().search_count([("module", "=", DEMO)]) > 0
