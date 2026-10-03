# Notifications used to be an unsent placeholder outbox. Close those rows so the new sender never mails old events.
def migrate(cr, version):
    cr.execute("UPDATE otm_cw_notification SET state='skipped', error='Created before channel delivery existed' "
               "WHERE state='pending' AND recipient IS NULL")
