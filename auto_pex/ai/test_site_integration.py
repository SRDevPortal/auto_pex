"""Rollback-only smoke check on an initialized site; never calls OpenAI or starts jobs.

Run: bench --site SITE execute auto_pex.ai.test_site_integration.run
"""

from unittest.mock import patch
from uuid import uuid4

import frappe
from frappe.utils import now_datetime

from auto_pex.ai import history, pasted_catalog, workflow
from auto_pex.ai.schema import TABLES


def run():
	prefix = "AUTO-PEX-TEST-" + uuid4().hex[:10]
	original_user = frappe.session.user
	frappe.set_user("Administrator")
	frappe.db.savepoint("auto_pex_smoke")
	try:
		content_checks = _check_pasted_settings(prefix)
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
			("FOLLOWUP", "2026-01-15"),
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
					"sr_encounter_type": "Followup" if suffix == "FOLLOWUP" else "Order",
					"sr_encounter_place": "Online",
					"sr_sales_type": "Repeat",
					"sr_encounter_status": "Draft",
					"sr_notes": "Synthetic clinical update",
					"sr_investigations": "Synthetic investigation result " + suffix,
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
							"auto_pex_dispensing_quantity": 42.5,
							"auto_pex_dispensing_uom": "Nos",
							"comment": "Synthetic administration instruction",
						},
					)
					child.db_insert()
		current = prefix + "-CURRENT"
		lab_checks = _check_laboratory_history(prefix, patient, current)
		report_checks = _check_attached_reports(prefix, current)
		result = history.get_history(current)
		assert len(result["previous"]) == 2
		previous = next(row for row in result["previous"] if row["id"].endswith("-PREVIOUS"))
		followup = next(row for row in result["previous"] if row["id"].endswith("-FOLLOWUP"))
		assert len(previous["prescriptions"]) == 3
		assert all(row["auto_pex_dispensing_quantity"] == 42.5 for row in previous["prescriptions"])
		assert followup["notes"]["sr_investigations"] == "Synthetic investigation result FOLLOWUP"
		assert followup["notes"]["sr_notes"] == "Synthetic clinical update"
		assert history.get_history(current, history_limit=1)["truncated"] is True
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
			assert enqueue.call_args.kwargs["timeout"] == workflow.GENERATION_TIMEOUT
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
			**content_checks,
			**lab_checks,
			**report_checks,
			"read_only_history": "passed",
			"orders_followups_investigations_and_history_limit": "passed",
			"prescription_quantity_storage": "passed",
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
		frappe.clear_cache(doctype="Auto Pex AI Settings")
		frappe.set_user(original_user)


def _check_pasted_settings(prefix):
	"""Exercise the real Singles storage and controller with rollback-only synthetic content."""
	import csv
	import io

	medicine = frappe.get_doc(
		{
			"doctype": "Medication",
			"name": prefix + "-MED",
			"medication_name": "Synthetic catalog test",
			"medication_class": "Ayurvedic Medicine",
		}
	)
	medicine.db_insert()
	stream = io.StringIO(newline="")
	writer = csv.writer(stream)
	writer.writerow(["code", "medication_id", "name", "formulation"])
	formulation = 'Synthetic, "quoted"\nMultiline text ' + "x" * 140_000
	writer.writerow(["TEST", medicine.name, "Synthetic", formulation])
	config = frappe.get_single("Auto Pex AI Settings")
	config.enabled, config.auto_generate = 0, 0
	config.medicine_catalog = stream.getvalue()
	config.prescribing_constraints = "Synthetic instructions\n" + "y" * 140_000
	config.additional_medications, config.medication_rules = "", "{}"
	config.save()
	saved = frappe.get_single("Auto Pex AI Settings")
	assert saved.medicine_catalog == stream.getvalue()
	assert saved.prescribing_constraints == config.prescribing_constraints
	assert pasted_catalog.parse_catalog(saved.medicine_catalog)[0]["formulation"] == formulation
	for invalid in ("medication_id,formulation\nMISSING,test", 'medication_id,formulation\nMED,"unclosed'):
		saved.medicine_catalog = invalid
		try:
			saved.save()
		except frappe.ValidationError:
			pass
		else:
			raise AssertionError("Invalid catalog was saved")
	assert frappe.get_single("Auto Pex AI Settings").medicine_catalog == stream.getvalue()
	return {"large_multiline_settings_save_and_parse": "passed", "invalid_catalog_save_rejected": "passed"}


def _check_laboratory_history(prefix, patient, current):
	"""Real permission-filtered queries and child-result snapshots, all rolled back by run()."""
	other = frappe.get_doc(
		{"doctype": "Patient", "name": prefix + "-OTHER", "first_name": "Other synthetic patient"}
	)
	other.db_insert()
	for suffix, overrides in (
		("LAB", {}),
		("UNDATED-RESULT", {"result_date": None, "date": "2026-01-11"}),
		("CREATION-DATE", {"result_date": None, "date": None}),
		("FUTURE", {"result_date": "2026-03-01"}),
		("OLD", {"result_date": "2020-01-01"}),
		("CANCELLED-LAB", {"docstatus": 2}),
		("REJECTED-LAB", {"status": "Rejected"}),
		("OTHER-LAB", {"patient": other.name}),
	):
		lab = frappe.get_doc(
			{
				"doctype": "Lab Test",
				"name": prefix + "-" + suffix,
				"patient": patient.name,
				"date": "2026-01-10",
				"result_date": "2026-01-20",
				"creation": "2026-01-10 09:00:00",
				"modified": "2026-01-20 09:00:00",
				"docstatus": 1,
				"status": "Approved",
				"lab_test_name": "Synthetic panel",
				**overrides,
			}
		)
		lab.db_insert()
		if suffix == "LAB":
			for table, values in (
				(
					"normal_test_items",
					{
						"lab_test_event": "Synthetic analyte",
						"result_value": "7.2",
						"lab_test_uom": "unit",
						"normal_range": "Synthetic reference",
					},
				),
				(
					"descriptive_test_items",
					{
						"lab_test_particulars": "Synthetic description",
						"result_value": "Synthetic text result",
					},
				),
				(
					"organism_test_items",
					{
						"organism": "Synthetic organism",
						"colony_population": "Synthetic count",
						"colony_uom": "unit",
					},
				),
				(
					"sensitivity_test_items",
					{"antibiotic": "Synthetic antibiotic", "antibiotic_sensitivity": "Synthetic finding"},
				),
			):
				child = lab.append(table, values)
				child.db_insert()
				if table == "normal_test_items":
					result_row = child.name
	for suffix, overrides in (
		("OBS", {}),
		("COMPONENT", {"parent_observation": prefix + "-OBS"}),
		("OTHER-OBS", {"patient": other.name}),
		("FUTURE-OBS", {"time_of_result": "2026-03-01 09:00:00"}),
		("NON-LAB-OBS", {"observation_category": "Vital Signs"}),
		("ERROR-OBS", {"status": "Entered in Error"}),
	):
		frappe.get_doc(
			{
				"doctype": "Observation",
				"name": prefix + "-" + suffix,
				"patient": patient.name,
				"observation_category": "Laboratory",
				"status": "Final",
				"docstatus": 1,
				"time_of_result": "2026-01-25 09:00:00",
				"posting_date": "2026-01-10",
				"creation": "2026-01-10 09:00:00",
				"modified": "2026-01-25 09:00:00",
				"permitted_data_type": "Numeric",
				"result_data": "0",
				"result_float": 0,
				"permitted_unit": "unit",
				"reference": "Synthetic reference",
				"result_interpretation": "Synthetic interpretation",
				**overrides,
			}
		).db_insert()
	doc = frappe.get_doc("Patient Encounter", current)
	raw = history.get_history(current)
	clinical = history.normalize_history(raw, doc, 365, 20)
	assert {row["name"] for row in clinical["investigations"]} == {
		prefix + "-" + suffix for suffix in ("LAB", "UNDATED-RESULT", "CREATION-DATE", "OBS", "COMPONENT")
	}
	lab = next(row for row in clinical["investigations"] if row["name"] == prefix + "-LAB")
	for table in (
		"normal_test_items",
		"descriptive_test_items",
		"organism_test_items",
		"sensitivity_test_items",
	):
		assert len(lab["details"][table]) == 1
	assert lab["details"]["normal_test_items"][0]["result_value"] == "7.2"
	assert lab["details"]["normal_test_items"][0]["normal_range"] == "Synthetic reference"
	assert history.get_history(current, history_limit=1)["investigations_truncated"] is True
	before = workflow.context_hash(clinical, {})
	# A child-only result correction must be detected even when the parent timestamp stays unchanged.
	frappe.db.set_value("Normal Test Result", result_row, "result_value", "8.4")
	after = history.normalize_history(history.get_history(current), doc, 365, 20)
	assert workflow.context_hash(after, {}) != before
	return {
		"lab_results_and_observation_components": "passed",
		"lab_patient_and_date_isolation": "passed",
		"lab_count_truncation": "passed",
		"lab_child_result_freshness": "passed",
	}


def _check_attached_reports(prefix, current):
	import json
	from pathlib import Path
	from tempfile import TemporaryDirectory

	from auto_pex.ai import provider, reports
	from auto_pex.ai.test_reports import extraction, pdf

	frappe.db.savepoint("auto_pex_reports")
	try:
		with TemporaryDirectory(
			prefix="auto-pex-test-", dir=frappe.get_site_path("private", "files")
		) as folder:
			root = Path(folder)
			for suffix, parent in (
				("CURRENT", current),
				("PREVIOUS", prefix + "-PREVIOUS"),
				("OTHER", prefix + "-OTHER-ENCOUNTER"),
			):
				path = root / (suffix + ".pdf")
				path.write_bytes(pdf())
				frappe.get_doc(
					{
						"doctype": "File",
						"name": prefix + "-FILE-" + suffix,
						"file_name": path.name,
						"file_url": "/private/files/" + root.name + "/" + path.name,
						"is_private": 1,
						"is_folder": 0,
						"attached_to_doctype": "Patient Encounter",
						"attached_to_name": parent,
					}
				).db_insert()
			doc = frappe.get_doc("Patient Encounter", current)
			config = frappe.get_single("Auto Pex AI Settings")
			config.history_source = "Local read-only API"
			config.model = "synthetic-model"
			clinical = history.fetch_history(doc, config)
			assert len(clinical["attached_reports"]) == 2
			before = workflow.context_hash(clinical, {})
			snapshots = []
			with patch.object(
				provider, "extract_report", return_value=(extraction(), "SYNTHETIC", {})
			) as reader:
				reports.extract(clinical, config, lambda: snapshots.append(json.dumps(clinical)))
			assert reader.call_count == 2
			assert len(snapshots) == 2
			reports.require_complete(clinical)
			assert workflow.context_hash(clinical, {}) == before
			assert any(source.startswith("File:") for source in history.source_ids(clinical))
			# Change only bytes on disk, without touching File.modified or content_hash.
			(root / "CURRENT.pdf").write_bytes(pdf(1))
			fresh = history.fetch_history(doc, config)
			assert workflow.context_hash(fresh, {}) != before
			config.history_source = "Remote read-only API"
			assert len(reports.manifest(doc, clinical, local_history=False)) == 1
		return {
			"encounter_report_files_and_patient_scope": "passed",
			"report_extraction_audit": "passed",
			"report_file_bytes_freshness": "passed",
			"remote_history_current_local_reports": "passed",
		}
	finally:
		frappe.db.rollback(save_point="auto_pex_reports")
