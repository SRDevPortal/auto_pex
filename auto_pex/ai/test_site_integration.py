"""Rollback-only smoke check on an initialized site; never calls OpenAI or starts jobs.

Run: bench --site SITE execute auto_pex.ai.test_site_integration.run
"""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from auto_pex.ai import history, workflow
from auto_pex.ai.schema import TABLES


def run():
	prefix = "AUTO-PEX-TEST-" + uuid4().hex[:10]
	original_user = frappe.session.user
	frappe.set_user("Administrator")
	frappe.db.savepoint("auto_pex_smoke")
	try:
		# Low-level inserts isolate fixtures from unrelated patient/encounter messaging hooks.
		patient = frappe.get_doc(
			{
				"doctype": "Patient",
				"name": prefix,
				"first_name": "Synthetic AI Test",
				"patient_name": "Synthetic AI Test",
				"medical_history": "Synthetic history",
			}
		)
		patient.db_insert()
		for suffix, date in (
			("PREVIOUS", "2026-01-01"),
			("CURRENT", "2026-02-01"),
			("CANCELLED", "2026-01-02"),
		):
			record = frappe.get_doc(
				{
					"doctype": "Patient Encounter",
					"name": prefix + "-" + suffix,
					"patient": patient.name,
					"encounter_date": date,
					"creation": "2026-01-01" if suffix != "CURRENT" else "2026-02-01",
					"modified": now_datetime(),
					"docstatus": 2 if suffix == "CANCELLED" else 0,
					"sr_encounter_type": "Order",
					"sr_encounter_place": "Online",
					"sr_sales_type": "Repeat",
					"sr_encounter_status": "Draft",
					"sr_notes": "Synthetic clinical update",
					"auto_pex_notes_ready": 1,
				}
			)
			record.db_insert()
			if suffix == "PREVIOUS":
				for table in TABLES.values():
					child = record.append(
						table,
						{
							"medication": prefix + "-MED",
							"dosage": "Synthetic dose",
							"period": "Synthetic duration",
							"dosage_form": "Synthetic form",
						},
					)
					child.db_insert()
		current = prefix + "-CURRENT"
		result = history.get_history(current)
		assert len(result["previous"]) == 1
		assert len(result["previous"][0]["prescriptions"]) == 3
		assert result["patient_id"] == patient.name
		assert history.normalize_history(result, frappe.get_doc("Patient Encounter", current))["current"][
			"notes"
		]["sr_notes"]

		config = frappe.get_single("Auto Pex AI Settings")
		config.enabled, config.eligible_status, config.model = 1, "Draft", "test-model"
		with (
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(frappe, "enqueue") as enqueue,
		):
			name = workflow.request_draft(current)
			same_name = workflow.request_draft(current)
			assert name == same_name
			enqueue.assert_called_once()
			assert enqueue.call_args.kwargs["enqueue_after_commit"] is True
			draft = frappe.get_doc("Auto Pex AI Draft", name)
			assert draft.status == "Queued"
			assert workflow.protects_prescription(frappe.get_doc("Patient Encounter", current))
		draft.status = "Approved"
		try:
			draft.save()
		except frappe.ValidationError:
			pass
		else:
			raise AssertionError("Direct lifecycle edit was accepted")
		assert frappe.db.get_value("Auto Pex AI Draft", name, "status") == "Queued"
		assert workflow.draft_permission(draft, user="Guest", ptype="read") is False
		assert frappe.db.get_value("Patient Encounter", current, "sr_encounter_status") == "Draft"
		return {
			"read_only_history": "passed",
			"three_medication_tables": "passed",
			"cancelled_history_excluded": "passed",
			"queue_deduplication": "passed",
			"audit_fields_protected": "passed",
			"guest_access_denied": "passed",
			"real_api_calls": 0,
			"fixtures": "rolled back",
		}
	finally:
		frappe.db.rollback(save_point="auto_pex_smoke")
		frappe.set_user(original_user)
