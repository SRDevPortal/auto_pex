"""One-time upgrade: require an explicit rollout after the team verifies its content."""

import frappe


def execute():
	if frappe.db.exists("DocType", "Auto Pex AI Settings"):
		frappe.db.set_single_value("Auto Pex AI Settings", "auto_generate", 0)
		frappe.clear_cache(doctype="Auto Pex AI Settings")
