# -*- coding: utf-8 -*-
"""Customer notifications: WhatsApp Cloud API, Telegram bot, SMS (Twilio) and Email (Odoo outgoing mail).

Design
* Business code only calls ``otm.cw.notification.notify(event, partner, message, job)``; that renders the message and
  writes an outbox row per channel. Nothing is sent inside the job transaction.
* ``process_queue`` (cron every minute + fire-and-forget call from the web app) sends pending rows with
  ``FOR UPDATE SKIP LOCKED`` so two workers never double-send. Failures retry up to MAX_TRIES, then fall back to the next
  channel (auto mode).
* Credentials live in ir.config_parameter (otm_cw.notify.*); the API never returns them (write-only, masked).
"""
import logging
import re
import secrets

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .misc import EVENTS

_logger = logging.getLogger(__name__)
CHANNELS = [("whatsapp", "WhatsApp"), ("telegram", "Telegram"), ("sms", "SMS"), ("email", "Email")]
ORDER = ["whatsapp", "telegram", "sms", "email"]
MAX_TRIES = 3
TIMEOUT = 10
P = "otm_cw.notify."

# events that are sent to customers + default on/off + default text
EVENT_DEFAULTS = {
    "vehicle_assigned": ("1", "Hi {customer}, your vehicle {vehicle} has been assigned to {bay}{staff_part}. Token #{token}. — {shop}"),
    "wash_started": ("0", "Hi {customer}, we have started washing {vehicle} ({services}). Token #{token}. — {shop}"),
    "vehicle_ready": ("1", "Hi {customer}, your vehicle {vehicle} is ready for pickup!{due_part} — {shop}"),
    "delivered": ("0", "Thank you {customer}! {vehicle} has been delivered. We hope to see you again. — {shop}"),
    "booking_confirmed": ("0", "Hi {customer}, your booking {ref} is confirmed. — {shop}"),
    "vehicle_arrived": ("0", "Hi {customer}, {vehicle} is checked in. Token #{token}. — {shop}"),
    "payment_received": ("0", "Hi {customer}, we received your payment. Thank you! — {shop}"),
    "payment_pending": ("0", "Hi {customer}, a balance is pending for {vehicle}. — {shop}"),
    "package_expiring": ("0", "Hi {customer}, your wash package is expiring soon. — {shop}"),
}
SECRET_KEYS = ("wa_token", "tg_token", "sms_token")
PLAIN_KEYS = ("wa_enabled", "wa_phone_id", "wa_lang", "wa_country", "tg_enabled", "tg_bot", "sms_enabled", "sms_sid", "sms_from",
              "email_enabled", "shop_name")


def _param(env, key, default=""):
    v = env["ir.config_parameter"].sudo().get_param(P + key)
    return v if v not in (None, False) else default


def _digits(number, country="91"):
    d = re.sub(r"\D", "", number or "")
    if len(d) == 10:
        d = (country or "91") + d
    return d if 8 <= len(d) <= 15 else ""


class OtmCwNotification(models.Model):
    _name = "otm.cw.notification"
    _description = "Car Wash Notification Outbox"
    _order = "id desc"

    event = fields.Selection(EVENTS, required=True, index=True)
    partner_id = fields.Many2one("res.partner", index=True)
    job_id = fields.Many2one("otm.cw.job", index=True, ondelete="set null")
    channel = fields.Selection(CHANNELS, default="whatsapp", required=True)
    recipient = fields.Char()
    message = fields.Text()
    state = fields.Selection([("pending", "Pending"), ("sent", "Sent"), ("failed", "Failed"), ("skipped", "Skipped")],
                             default="pending", index=True)
    error = fields.Char()
    attempts = fields.Integer(default=0)
    sent_dt = fields.Datetime()
    fallbacks = fields.Char(help="Remaining channels to try if this one fails (auto mode).")

    # ------------------------------------------------------------ settings
    @api.model
    def event_enabled(self, event):
        default = EVENT_DEFAULTS.get(event, ("0", ""))[0]
        return _param(self.env, "ev_" + event, default) == "1"

    @api.model
    def _enabled_channels(self):
        env = self.env
        out = []
        if _param(env, "wa_enabled") == "1" and _param(env, "wa_token") and _param(env, "wa_phone_id"):
            out.append("whatsapp")
        if _param(env, "tg_enabled") == "1" and _param(env, "tg_token"):
            out.append("telegram")
        if _param(env, "sms_enabled") == "1" and _param(env, "sms_token") and _param(env, "sms_sid") and _param(env, "sms_from"):
            out.append("sms")
        if _param(env, "email_enabled") == "1":
            out.append("email")
        return out

    @api.model
    def _recipient(self, partner, channel):
        country = _param(self.env, "wa_country", "91")
        if channel == "whatsapp":
            return _digits(partner.otm_cw_whatsapp or partner.otm_cw_mobile, country)
        if channel == "sms":
            d = _digits(partner.otm_cw_mobile, country)
            return ("+" + d) if d else ""
        if channel == "telegram":
            return partner.otm_cw_telegram_chat_id or ""
        if channel == "email":
            return partner.email or ""
        return ""

    @api.model
    def _context(self, event, partner, job=None, message=""):
        shop = _param(self.env, "shop_name") or self.env.company.name or "Car Wash"
        ctx = {"customer": partner.name or "", "shop": shop, "ref": message, "vehicle": "", "bay": "", "token": "",
               "services": "", "staff": "", "staff_part": "", "due_part": "", "amount_due": ""}
        if job:
            staff = ", ".join(job.sudo().assigned_staff_ids.mapped("name"))
            ctx.update(vehicle=job.vehicle_id.reg_no or "", bay=job.bay_id.name or "", token=str(job.token or ""),
                       services=", ".join(job.line_ids.mapped("service_id.name")), staff=staff,
                       staff_part=(" (attendant: %s)" % staff) if staff else "",
                       amount_due="%.2f" % job.balance,
                       due_part=(" Balance due: ₹%.0f." % job.balance) if job.balance > 0 else "")
        return ctx

    @api.model
    def render(self, event, partner, job=None, message=""):
        tpl = _param(self.env, "tpl_" + event) or EVENT_DEFAULTS.get(event, ("0", ""))[1]
        if not tpl:
            return message or ""

        class _Safe(dict):
            def __missing__(self, key):
                return ""
        try:
            return tpl.format_map(_Safe(self._context(event, partner, job, message))).strip()
        except (ValueError, IndexError, KeyError):
            return message or tpl

    # ------------------------------------------------------------ enqueue
    @api.model
    def notify(self, event, partner, message="", job=None, force=False, channels=None):
        """Queue a customer message. Returns the created rows (possibly empty)."""
        if not partner or event not in dict(EVENTS):
            return self.browse()
        if not force and not self.event_enabled(event):
            return self.browse()
        pref = partner.otm_cw_notify_channel or "auto"
        if (not partner.otm_cw_notify_opt_in or pref == "none") and not force:
            return self.browse()
        enabled = self._enabled_channels()
        # order of attempt: explicit channels > customer preference > auto
        if channels:
            wanted = [c for c in channels if c in enabled]
        elif pref in dict(CHANNELS):
            wanted = [pref] if pref in enabled else []
        else:
            wanted = list(enabled)
        text = self.render(event, partner, job, message)
        Note = self.sudo()
        jid = job.id if job else False
        if not wanted:
            return Note.create({"event": event, "partner_id": partner.id, "job_id": jid, "channel": (enabled or ["whatsapp"])[0],
                                "message": text, "state": "skipped",
                                "error": _("No notification channel is configured/enabled for this customer.")})
        reachable = [c for c in wanted if self._recipient(partner, c)]
        if not reachable:
            return Note.create({"event": event, "partner_id": partner.id, "job_id": jid, "channel": wanted[0], "message": text,
                                "state": "skipped", "error": _("Customer has no contact for the enabled channel(s).")})
        targets = reachable if pref == "all" else reachable[:1]
        rows = Note.browse()
        for ch in targets:
            rest = reachable[1:] if (pref == "auto" and not channels) else []   # fall back down the list if this one fails
            rows |= Note.create({"event": event, "partner_id": partner.id, "job_id": jid, "channel": ch,
                                 "recipient": self._recipient(partner, ch), "message": text, "fallbacks": ",".join(rest)})
        return rows

    # ------------------------------------------------------------ sending
    @api.model
    def process_queue(self, limit=40):
        self.env.cr.execute("""SELECT id FROM otm_cw_notification WHERE state = 'pending' AND attempts < %s
                               ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""", (MAX_TRIES, limit))
        rows = self.sudo().browse([r[0] for r in self.env.cr.fetchall()])
        sent = 0
        for row in rows:
            row.attempts += 1
            try:
                with self.env.cr.savepoint():
                    row._deliver()
                row.write({"state": "sent", "sent_dt": fields.Datetime.now(), "error": False})
                sent += 1
            except Exception as exc:  # noqa: BLE001 - provider errors must never break the caller
                msg = self._clean_error(exc)
                _logger.warning("Notification %s via %s failed: %s", row.id, row.channel, msg)
                row.error = msg[:250]
                if row.attempts >= MAX_TRIES or getattr(exc, "permanent", False):
                    row.state = "failed"
                    row._fallback()
        return {"processed": len(rows), "sent": sent}

    def _fallback(self):
        self.ensure_one()
        rest = [c for c in (self.fallbacks or "").split(",") if c]
        if not rest or not self.partner_id:
            return
        nxt = rest[0]
        to = self._recipient(self.partner_id, nxt)
        if to:
            self.create({"event": self.event, "partner_id": self.partner_id.id, "job_id": self.job_id.id, "channel": nxt,
                         "recipient": to, "message": self.message, "fallbacks": ",".join(rest[1:])})

    @staticmethod
    def _clean_error(exc):
        text = str(getattr(exc, "args", [exc])[0] if getattr(exc, "args", None) else exc)
        text = re.sub(r"bot\d+:[\w-]+", "bot***", text)                       # telegram token inside URLs
        text = re.sub(r"(?i)(bearer|token|key)[=: ]+\S+", r"\1 ***", text)
        return text

    def _deliver(self):
        self.ensure_one()
        env = self.env
        if self.channel == "whatsapp":
            return self._send_whatsapp(self.recipient, self.message, self.event, self.job_id, self.partner_id)
        if self.channel == "telegram":
            return self._send_telegram(self.recipient, self.message)
        if self.channel == "sms":
            return self._send_sms(self.recipient, self.message)
        if self.channel == "email":
            return self._send_email(self.recipient, self.message, self.event)
        raise UserError(_("Unknown channel."))

    def _http(self, method, url, **kw):
        try:
            r = requests.request(method, url, timeout=TIMEOUT, **kw)
        except requests.RequestException as exc:
            raise UserError(_("Network error contacting the provider.")) from exc
        if r.status_code >= 400:
            try:
                body = r.json()
                detail = (body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("description") or body.get("message")) or ""
            except ValueError:
                detail = ""
            err = UserError("HTTP %s %s" % (r.status_code, str(detail)[:160]))
            err.permanent = r.status_code in (400, 401, 403, 404)   # bad token / blocked / unknown chat: retrying will not help
            raise err
        return r

    def _send_whatsapp(self, to, text, event="", job=None, partner=None):
        env = self.env
        token, phone_id = _param(env, "wa_token"), _param(env, "wa_phone_id")
        if not (token and phone_id and to):
            raise UserError(_("WhatsApp is not configured."))
        tpl = _param(env, "wa_tpl_" + event) if event else ""
        if tpl:   # approved template (needed to start a conversation outside the 24h window)
            ctx = self._context(event, partner or env["res.partner"], job)
            params = [ctx["customer"] or "-", ctx["vehicle"] or "-", ctx["bay"] or "-", ctx["token"] or "-"]
            payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                       "template": {"name": tpl, "language": {"code": _param(env, "wa_lang", "en")},
                                    "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]}}
        else:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": False, "body": text[:4000]}}
        self._http("POST", "https://graph.facebook.com/v21.0/%s/messages" % phone_id,
                   headers={"Authorization": "Bearer " + token}, json=payload)

    def _send_telegram(self, chat_id, text):
        token = _param(self.env, "tg_token")
        if not (token and chat_id):
            raise UserError(_("Telegram is not configured or the customer has not linked Telegram."))
        self._http("POST", "https://api.telegram.org/bot%s/sendMessage" % token, json={"chat_id": chat_id, "text": text[:4000]})

    def _send_sms(self, to, text):
        env = self.env
        sid, token, sender = _param(env, "sms_sid"), _param(env, "sms_token"), _param(env, "sms_from")
        if not (sid and token and sender and to):
            raise UserError(_("SMS is not configured."))
        self._http("POST", "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid, auth=(sid, token),
                   data={"To": to, "From": sender, "Body": text[:1500]})

    def _send_email(self, to, text, event=""):
        if not to:
            raise UserError(_("Customer has no email address."))
        label = dict(EVENTS).get(event, "Update")
        mail = self.env["mail.mail"].sudo().create({
            "subject": "%s — %s" % (_param(self.env, "shop_name") or self.env.company.name, label),
            "body_html": "<p>%s</p>" % (text or "").replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br/>"),
            "email_to": to, "auto_delete": True})
        mail.send(raise_exception=True)

    @api.model
    def cron_process(self):
        self.process_queue(80)

    # ------------------------------------------------------------ admin API helpers
    def action_retry(self):
        self.sudo().filtered(lambda r: r.state in ("failed", "skipped")).write({"state": "pending", "attempts": 0, "error": False})
