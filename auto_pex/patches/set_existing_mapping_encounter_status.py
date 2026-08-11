import frappe


PRX_READY_STATUS = "PRX Ready"


def execute():
	if not frappe.db.has_column("Auto Pex Item Mapping", "set_encounter_status"):
		return

	mappings = frappe.get_all(
		"Auto Pex Item Mapping",
		filters={
			"medication_template": ["is", "set"],
			"set_encounter_status": ["is", "not set"],
		},
		pluck="name",
	)
	if not mappings:
		return

	if not frappe.db.exists("SR Encounter Status", PRX_READY_STATUS):
		status = frappe.new_doc("SR Encounter Status")
		status.sr_status_name = PRX_READY_STATUS
		status.is_active = 1
		status.insert(ignore_permissions=True)

	for mapping_name in mappings:
		frappe.db.set_value(
			"Auto Pex Item Mapping",
			mapping_name,
			"set_encounter_status",
			PRX_READY_STATUS,
			update_modified=False,
		)
