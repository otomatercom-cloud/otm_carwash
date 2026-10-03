# -*- coding: utf-8 -*-
{
    "name": "Otomater Car Wash Management",
    "summary": "Car wash operations: bays, queue, jobs, pricing matrix, packages, payments, staff performance",
    "version": "19.0.1.1.0",
    "category": "Services",
    "author": "Otomater",
    "website": "https://otomater.com",
    "license": "OPL-1",
    "depends": ["base", "hr", "account"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/sequence_data.xml",
        "data/default_data.xml",
        "data/cron_data.xml",
        "views/master_views.xml",
        "views/operation_views.xml",
        "views/menus.xml",
    ],
    "application": True,
    "installable": True,
}
