import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def setup():
	if not frappe.db.exists("Role", "Auto Pex AI Reviewer"):
		frappe.get_doc({"doctype": "Role", "role_name": "Auto Pex AI Reviewer", "desk_access": 1}).insert(
			ignore_permissions=True
		)
	if frappe.db.exists("DocType", "Patient Encounter"):
		create_custom_fields(
			{
				"Patient Encounter": [
					{
						"fieldname": "auto_pex_notes_ready",
						"label": "Clinical notes ready for AI",
						"fieldtype": "Check",
						"default": "0",
						"no_copy": 1,
						"insert_after": "sr_notes",
						"module": "Auto Pex",
						"depends_on": 'eval:doc.sr_encounter_type=="Order" && doc.sr_encounter_place=="Online" && doc.sr_sales_type=="Repeat"',
						"description": "Mark after reviewing the updated clinical notes. AI drafts require clinician approval.",
					}
				]
			}
		)
