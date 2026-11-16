"""Lab history regressions; no network calls or real patient fixtures."""

import copy
import json
from unittest.mock import MagicMock, patch

import frappe

from auto_pex.ai import history, investigations, provider, test_followup, workflow
from auto_pex.ai.schema import DraftError, validate_proposal
from auto_pex.ai.test_followup import Base, Doc, catalog, context, encounter, proposal, settings


def lab_source(doctype="Lab Test", name="LAB-1"):
	return {
		"id": f"{doctype}:{name}",
		"doctype": doctype,
		"name": name,
		"patient_id": "PATIENT-1",
		"date": "2026-09-30",
		"modified": "2026-09-30 10:00:00",
		"docstatus": 1,
		"status": "Approved",
		"details": {
			"lab_test_name": "Synthetic panel",
			"lab_test_comment": "Result comment",
			"normal_test_items": [
				{
					"lab_test_event": "Synthetic analyte",
					"result_value": "7.2",
					"lab_test_uom": "unit",
					"normal_range": "Team-entered reference",
				}
			],
		},
	}


class InvestigationTests(Base):
	def normalize(self, sources):
		data = context()
		data["investigations"] = sources
		return history.normalize_history(data, encounter(), 365, 20)

	def test_result_units_ranges_and_source_citations_reach_model(self):
		source = lab_source()
		source["email"] = "DO-NOT-SEND"
		source["details"]["patient_name"] = "DO-NOT-SEND"
		source["details"]["normal_test_items"][0]["extra"] = "DO-NOT-SEND"
		clinical = self.normalize([source])
		self.assertNotIn("DO-NOT-SEND", json.dumps(clinical))
		self.assertNotIn("patient_id", clinical["investigations"][0])
		row = clinical["investigations"][0]["details"]["normal_test_items"][0]
		self.assertEqual(
			(row["result_value"], row["lab_test_uom"], row["normal_range"]),
			("7.2", "unit", "Team-entered reference"),
		)
		value = proposal()
		value["items"][0]["source_ids"] = [source["id"]]
		validate_proposal(value, history.source_ids(clinical))
		response = {
			"status": "completed",
			"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}],
		}
		with patch.object(provider, "request_json", return_value=response) as request:
			provider.generate(clinical, catalog(), settings())
		self.assertEqual(
			json.loads(request.call_args.kwargs["json"]["input"])["clinical_context"]["investigations"],
			clinical["investigations"],
		)

	def test_other_patient_cancelled_future_invalid_and_duplicate_sources_rejected(self):
		for changes in (
			{"patient_id": "OTHER"},
			{"docstatus": 2},
			{"status": "Rejected"},
			{"status": "Entered in Error"},
			{"date": "2027-01-01"},
			{"id": "WRONG"},
			{"doctype": "User"},
		):
			with self.subTest(changes=changes), self.assertRaises(DraftError):
				self.normalize([{**lab_source(), **changes}])
		with self.assertRaises(DraftError):
			self.normalize([lab_source(), lab_source()])

	def test_old_remote_adapter_cannot_silently_omit_results(self):
		data = context()
		del data["investigations"]
		with self.assertRaisesRegex(DraftError, "Update the remote history adapter"):
			history.normalize_history(data, encounter())

	def test_limit_and_window_truncation_are_explicit(self):
		data = context()
		data["investigations"] = [
			lab_source(),
			{**lab_source(name="OLDER"), "date": "2025-01-01"},
			lab_source(name="SECOND"),
		]
		clinical = history.normalize_history(data, encounter(), 365, 1)
		self.assertEqual(len(clinical["investigations"]), 1)
		self.assertTrue(clinical["truncated"])
		self.assertTrue(clinical["investigations_truncated"])

	def test_observation_zero_value_attachment_and_pending_warnings(self):
		source = lab_source("Observation", "OBS-1")
		source.update(
			status="Preliminary",
			docstatus=0,
			details={
				"observation_category": "Laboratory",
				"permitted_data_type": "Numeric",
				"result_data": "0",
				"result_float": 0,
				"attachment_present": True,
				"result_attach": "/private/files/DO-NOT-SEND.pdf",
			},
		)
		clinical = self.normalize([source])
		self.assertEqual(clinical["investigations"][0]["details"]["result_data"], "0")
		self.assertNotIn("result_float", clinical["investigations"][0]["details"])
		self.assertNotIn("DO-NOT-SEND", json.dumps(clinical))
		self.assertEqual(len(investigations.warnings(clinical)), 2)
		source["details"]["observation_category"] = "Social History"
		with self.assertRaises(DraftError):
			self.normalize([source])

	def test_result_tables_never_silently_truncated(self):
		source = lab_source()
		source["details"]["normal_test_items"] *= 1001
		with self.assertRaisesRegex(DraftError, "Nothing was truncated"):
			self.normalize([source])
		source = lab_source()
		source["details"]["lab_test_comment"] = "x" * 150_001
		with self.assertRaisesRegex(DraftError, "Clinical history is too large"):
			self.normalize([source])

	def test_result_edits_additions_and_deletions_invalidate_hash(self):
		clinical = self.normalize([lab_source()])
		digest = workflow.context_hash(clinical, catalog())
		for change in ("edit", "add", "delete", "status"):
			changed = copy.deepcopy(clinical)
			if change == "edit":
				changed["investigations"][0]["details"]["normal_test_items"][0]["result_value"] = "8.4"
			elif change == "add":
				changed = self.normalize([lab_source(), lab_source(name="NEW")])
			elif change == "delete":
				changed["investigations"] = []
			else:
				changed["investigations"][0]["status"] = "Completed"
			self.assertNotEqual(digest, workflow.context_hash(changed, catalog()))

	def test_local_approval_locks_results_and_rejects_concurrent_change(self):
		doc, config, _, draft = test_followup.WorkflowTests.make_review(self)
		config.history_source = "Local read-only API"
		draft.settings_hash = workflow.config_hash(config)
		clinical = self.normalize([lab_source()])
		draft.context_hash = workflow.context_hash(clinical, catalog())
		changed = copy.deepcopy(clinical)
		changed["investigations"][0]["details"]["normal_test_items"][0]["result_value"] = "Changed"

		def get_doc(kind, *args, **kwargs):
			return doc if kind == "Patient Encounter" else draft if kind == workflow.DRAFT else MagicMock()

		with (
			patch.object(workflow, "_review_document", return_value=draft),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(history, "fetch_history", side_effect=[clinical, changed]),
			patch.object(history, "medication_catalog", return_value=catalog()),
			patch.object(frappe, "get_doc", side_effect=get_doc) as documents,
		):
			self.assertEqual(workflow.approve_draft("DRAFT", "v1")["status"], "Stale")
		documents.assert_any_call("Lab Test", "LAB-1", for_update=True)
		doc.save.assert_not_called()

	def test_stored_lab_snapshot_requires_source_read_permission(self):
		clinical = self.normalize([lab_source()])
		draft = Doc(encounter="CURRENT", patient="PATIENT-1", context_json=json.dumps(clinical))
		with (
			patch.object(frappe, "get_doc", return_value=encounter()),
			patch.object(
				frappe, "has_permission", side_effect=lambda kind, *args, **kwargs: kind != "Lab Test"
			),
		):
			self.assertFalse(workflow.draft_permission(draft, user="restricted", ptype="read"))
