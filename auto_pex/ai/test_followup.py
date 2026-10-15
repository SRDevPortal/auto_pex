"""Run without a database: python -m unittest auto_pex.ai.test_followup -v."""

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
import requests

from auto_pex.ai import history, provider, workflow
from auto_pex.ai.schema import DraftError, encounter_snapshot, fingerprint, matches_route, validate_proposal


def proposal():
	return {
		"outcome": "draft",
		"summary": "Follow-up",
		"instruction": "Reviewed instructions",
		"warnings": [],
		"missing_information": [],
		"items": [
			{
				"medication_class": "Ayurvedic Medicine",
				"medication": "MED-1",
				"drug_code": "ITEM-1",
				"dosage": "DOSAGE-1",
				"period": "PERIOD-1",
				"dosage_form": "FORM-1",
				"instruction": "Instructions",
				"rationale": "Based on documented follow-up",
				"source_ids": ["PREV-1"],
			}
		],
	}


def catalog():
	return {
		"medications": [
			{
				"medication": "MED-1",
				"medication_class": "Ayurvedic Medicine",
				"drug_codes": ["ITEM-1"],
				"dosage_form": "FORM-1",
				"strength": 10,
				"strength_uom": "mg",
			}
		],
		"dosages": ["DOSAGE-1"],
		"periods": ["PERIOD-1"],
		"dosage_forms": ["FORM-1"],
	}


def context():
	source = {
		"id": "CURRENT",
		"date": "2026-10-01",
		"modified": "2026-10-01 10:00:00",
		"docstatus": 0,
		"status": "Draft",
		"notes": {"sr_notes": "Updated medical history"},
		"prescriptions": [],
	}
	return {
		"patient_id": "PATIENT-1",
		"encounter_id": "CURRENT",
		"truncated": False,
		"patient_history": {"modified": "2026-09-01", "allergies": "Unknown", "age_years": 30},
		"current": source,
		"previous": [{**source, "id": "PREV-1", "docstatus": 1, "prescriptions": proposal()["items"]}],
	}


class Doc(SimpleNamespace):
	def get(self, key, default=None):
		return getattr(self, key, default)

	def set(self, key, value):
		setattr(self, key, value)

	def append(self, key, value):
		getattr(self, key).append(value)


def encounter():
	return Doc(
		name="CURRENT",
		patient="PATIENT-1",
		docstatus=0,
		sr_encounter_type="Order",
		sr_encounter_place="Online",
		sr_sales_type="Repeat",
		sr_encounter_status="Draft",
		auto_pex_notes_ready=1,
		encounter_date="2026-10-01",
		sr_notes="Updated medical history",
		check_permission=MagicMock(),
		save=MagicMock(),
		meta=MagicMock(),
	)


def settings():
	return Doc(
		enabled=1,
		eligible_status="Draft",
		review_status="AI Review",
		approved_status="Approved",
		history_source="Remote read-only API",
		model="configured-model",
		request_timeout=10,
		retry_limit=1,
		max_medications=20,
		prescribing_constraints="Clinician rules",
		get_password=MagicMock(return_value="test-secret"),
	)


class Base(unittest.TestCase):
	def setUp(self):
		frappe.local.flags = frappe._dict()
		self.db = MagicMock()
		for key, value in (
			("db", self.db),
			("flags", frappe._dict()),
			("session", frappe._dict(user="reviewer")),
		):
			replacement = patch.object(frappe, key, value, create=True)
			replacement.start()
			self.addCleanup(replacement.stop)


class SchemaTests(Base):
	def test_route_requires_all_three_conditions(self):
		doc = encounter()
		self.assertTrue(matches_route(doc))
		for field, value in (
			("sr_encounter_type", "Followup"),
			("sr_encounter_place", "OPD"),
			("sr_sales_type", "Fresh"),
		):
			changed = copy.copy(doc)
			setattr(changed, field, value)
			self.assertFalse(matches_route(changed))

	def test_snapshot_tracks_notes_and_rows_but_not_our_status(self):
		doc = encounter()
		digest = fingerprint(encounter_snapshot(doc))
		doc.sr_encounter_status = "AI Review"
		self.assertEqual(digest, fingerprint(encounter_snapshot(doc)))
		doc.sr_notes = "Changed history"
		self.assertNotEqual(digest, fingerprint(encounter_snapshot(doc)))

	def test_valid_proposal(self):
		self.assertEqual(validate_proposal(proposal(), {"PREV-1"}), proposal())

	def test_invalid_medication_rows(self):
		for field, value in (
			("medication", ""),
			("dosage", ""),
			("source_ids", ["UNKNOWN"]),
			("source_ids", []),
			("medication_class", "Unknown"),
		):
			value_proposal = proposal()
			value_proposal["items"][0][field] = value
			with self.subTest(field=field, value=value), self.assertRaises(DraftError):
				validate_proposal(value_proposal, {"PREV-1"})

	def test_duplicate_medications_rejected_across_classes(self):
		value = proposal()
		value["items"].append({**value["items"][0], "medication_class": "Homeopathic Medicine"})
		with self.assertRaises(DraftError):
			validate_proposal(value, {"PREV-1"})

	def test_unable_response_cannot_contain_drugs(self):
		value = proposal()
		value["outcome"] = "unable_to_draft"
		with self.assertRaises(DraftError):
			validate_proposal(value, {"PREV-1"})

	def test_unknown_fields_and_excess_rows_rejected(self):
		value = proposal()
		value["override_status"] = "Approved"
		with self.assertRaises(DraftError):
			validate_proposal(value, {"PREV-1"})
		with self.assertRaises(DraftError):
			validate_proposal(proposal(), {"PREV-1"}, max_items=0)


class HistoryTests(Base):
	def test_enforced_medication_rules_block_dose_duration_and_medication(self):
		for rule in ({"blocked": True}, {"dosages": []}, {"periods": ["OTHER"]}, {"drug_codes": ["OTHER"]}):
			value = catalog()
			value["rules"] = {"MED-1": rule}
			with self.subTest(rule=rule), self.assertRaises(DraftError):
				history.validate_catalog(proposal(), value)

	def test_normalization_excludes_identifiers_and_extra_api_fields(self):
		value = context()
		value["patient_name"] = "Must not be sent"
		value["current"]["phone"] = "Must not be sent"
		normalized = history.normalize_history(value, encounter())
		self.assertNotIn("Must not be sent", json.dumps(normalized))
		self.assertNotIn("patient_id", normalized)

	def test_patient_mismatch_fails_closed(self):
		value = context()
		value["patient_id"] = "OTHER"
		with self.assertRaises(DraftError):
			history.normalize_history(value, encounter())

	def test_cancelled_future_and_duplicate_history_rejected(self):
		for change in ({"docstatus": 2}, {"date": "2027-01-01"}, {"id": "CURRENT"}):
			value = context()
			value["previous"][0].update(change)
			with self.subTest(change=change), self.assertRaises(DraftError):
				history.normalize_history(value, encounter())

	def test_no_previous_prescription_or_current_notes(self):
		value = context()
		value["previous"] = []
		with self.assertRaises(DraftError):
			history.normalize_history(value, encounter())
		value = context()
		value["current"]["notes"] = {}
		with self.assertRaises(DraftError):
			history.normalize_history(value, encounter())

	def test_catalog_rejects_wrong_item_class_dose_and_form(self):
		history.validate_catalog(proposal(), catalog())
		for key, value in (
			("drug_code", "OTHER"),
			("medication_class", "Homeopathic Medicine"),
			("dosage", "Invented dose"),
			("period", "Invented period"),
			("dosage_form", "OTHER"),
		):
			response = proposal()
			response["items"][0][key] = value
			with self.subTest(key=key), self.assertRaises(DraftError):
				history.validate_catalog(response, catalog())

	def test_remote_history_is_get_only_and_identity_checked(self):
		config = settings()
		config.history_url, config.history_days, config.history_limit = "https://history.example/api", 365, 20
		with patch.object(history, "request_json", return_value={"message": context()}) as request:
			history.fetch_history(encounter(), config)
		self.assertEqual(request.call_args.args[0], "GET")
		self.assertEqual(request.call_args.kwargs["params"]["encounter"], "CURRENT")


class ProviderTests(Base):
	def test_strict_schema_and_no_storage_or_tools(self):
		result = {
			"status": "completed",
			"id": "response-1",
			"output": [
				{"type": "message", "content": [{"type": "output_text", "text": json.dumps(proposal())}]}
			],
		}
		with patch.object(provider, "request_json", return_value=result) as request:
			value, _, _ = provider.generate({}, {}, settings())
		payload = request.call_args.kwargs["json"]
		self.assertFalse(payload["store"])
		self.assertTrue(payload["text"]["format"]["strict"])
		self.assertNotIn("tools", payload)
		self.assertEqual(value, proposal())

	def test_refusal_incomplete_and_invalid_json(self):
		for response in (
			{"status": "incomplete"},
			{"status": "completed", "output": []},
			{"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal"}]}]},
		):
			with patch.object(provider, "request_json", return_value=response), self.assertRaises(DraftError):
				provider.generate({}, {}, settings())

	def test_history_url_rejects_plain_http_credentials_and_fragments(self):
		for url in ("http://example.org", "https://name:password@example.org", "https://example.org/#secret"):
			with self.assertRaises(DraftError):
				provider.validate_url(url)

	def test_timeouts_retry_bounded_and_do_not_leak_error(self):
		with (
			patch.object(provider.requests, "request", side_effect=requests.Timeout("SECRET")) as request,
			patch.object(provider.time, "sleep"),
			self.assertRaises(DraftError) as error,
		):
			provider.request_json("GET", "https://example.org", headers={}, timeout=10, retries=1)
		self.assertEqual(request.call_count, 2)
		self.assertNotIn("SECRET", str(error.exception))

	def test_redirect_not_followed(self):
		response = MagicMock(status_code=302)
		response.__enter__.return_value = response
		with (
			patch.object(provider.requests, "request", return_value=response) as request,
			self.assertRaises(DraftError),
		):
			provider.request_json("GET", "https://example.org", headers={}, timeout=10, retries=0)
		self.assertFalse(request.call_args.kwargs["allow_redirects"])


class WorkflowTests(Base):
	def test_prompt_policy_change_requires_regeneration(self):
		doc, config, _, draft = self.make_review()
		with patch.object(workflow, "PROMPT_VERSION", "followup-v1"):
			draft.settings_hash = workflow.config_hash(config)
		with self.assertRaises(DraftError):
			workflow._current(draft, config, doc)

	def test_generation_saves_review_draft_without_populating_encounter_medications(self):
		doc, config, clinical, draft = self.make_review()
		draft.status, draft.requested_by = "Queued", "reviewer"
		with (
			patch.object(frappe, "set_user"),
			patch.object(workflow, "check_reviewer"),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(frappe, "cache", MagicMock()),
			patch.object(history, "fetch_history", return_value=clinical),
			patch.object(history, "medication_catalog", return_value=catalog()),
			patch.object(provider, "generate", return_value=(proposal(), "response", {})),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, *args, **kwargs: doc if kind == "Patient Encounter" else draft,
			),
		):
			workflow.generate_draft("DRAFT")
		self.assertEqual(draft.status, "Ready", draft.get("failure_reason"))
		self.assertEqual(doc.sr_encounter_status, "AI Review")
		self.assertEqual(draft.expected_status, "AI Review")
		self.assertEqual(draft.items[0]["medication"], "MED-1")
		self.assertIsNone(doc.get("drug_prescription"))

	def test_generation_marks_changed_remote_notes_stale(self):
		doc, config, clinical, draft = self.make_review()
		draft.status, draft.requested_by = "Queued", "reviewer"
		changed = copy.deepcopy(clinical)
		changed["previous"][0]["notes"]["sr_notes"] = "New history"
		with (
			patch.object(frappe, "set_user"),
			patch.object(workflow, "check_reviewer"),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(frappe, "cache", MagicMock()),
			patch.object(history, "fetch_history", side_effect=[clinical, changed]),
			patch.object(history, "medication_catalog", return_value=catalog()),
			patch.object(provider, "generate", return_value=(proposal(), "response", {})),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, *args, **kwargs: doc if kind == "Patient Encounter" else draft,
			),
		):
			workflow.generate_draft("DRAFT")
		self.assertEqual(draft.status, "Stale", draft.get("failure_reason"))
		doc.save.assert_not_called()

	def test_submit_orders_on_save_is_ineligible(self):
		doc = encounter()
		doc.submit_orders_on_save = 1
		with self.assertRaises(DraftError):
			workflow.assert_eligible(doc, settings())

	def make_review(self):
		doc, config = encounter(), settings()
		clinical = history.normalize_history(context(), doc)
		response = proposal()
		draft = Doc(
			name="DRAFT",
			encounter=doc.name,
			modified="v1",
			status="Ready",
			warnings="",
			expected_status="Draft",
			input_hash=fingerprint(encounter_snapshot(doc)),
			settings_hash=workflow.config_hash(config),
			context_hash=workflow.context_hash(clinical, catalog()),
			save=MagicMock(),
			original_response=json.dumps(response),
			summary=response["summary"],
			instruction=response["instruction"],
			items=[Doc(**{**row, "source_ids": json.dumps(row["source_ids"])}) for row in response["items"]],
		)
		return doc, config, clinical, draft

	def test_approval_applies_rows_and_status_once_with_audit(self):
		doc, config, clinical, draft = self.make_review()
		self.db.get_value.return_value = "PRACTITIONER-1"
		with (
			patch.object(workflow, "_review_document", return_value=draft),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(history, "fetch_history", return_value=clinical),
			patch.object(history, "medication_catalog", return_value=catalog()),
			patch.object(frappe, "get_meta", return_value=MagicMock()),
			patch.object(workflow, "now_datetime", return_value="now"),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, *args, **kwargs: doc if kind == "Patient Encounter" else draft,
			),
		):
			result = workflow.approve_draft("DRAFT", "v1")
		self.assertEqual(result["status"], "Approved")
		self.assertEqual(doc.sr_encounter_status, "Approved")
		self.assertEqual(doc.drug_prescription[0]["medication"], "MED-1")
		self.assertEqual(doc.drug_prescription[0]["strength"], 10)
		self.assertEqual(doc.sr_homeopathy_drug_prescription, [])
		self.assertEqual(draft.reviewed_by, "reviewer")
		self.assertEqual(draft.reviewed_practitioner, "PRACTITIONER-1")
		doc.save.assert_called_once()
		draft.save.assert_called_once()
		self.db.commit.assert_not_called()

	def test_failed_encounter_save_does_not_approve_draft(self):
		doc, config, clinical, draft = self.make_review()
		doc.save.side_effect = frappe.ValidationError("Workflow prevents transition")
		with (
			patch.object(workflow, "_review_document", return_value=draft),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(history, "fetch_history", return_value=clinical),
			patch.object(history, "medication_catalog", return_value=catalog()),
			patch.object(frappe, "get_meta", return_value=MagicMock()),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, *args, **kwargs: doc if kind == "Patient Encounter" else draft,
			),
			self.assertRaises(frappe.ValidationError),
		):
			workflow.approve_draft("DRAFT", "v1")
		self.assertEqual(draft.status, "Ready")
		draft.save.assert_not_called()
		self.db.commit.assert_not_called()

	def test_generation_failure_never_saves_encounter(self):
		doc, config, _, draft = self.make_review()
		draft.status, draft.requested_by = "Queued", "reviewer"
		with (
			patch.object(frappe, "set_user"),
			patch.object(workflow, "check_reviewer"),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(frappe, "cache", MagicMock()),
			patch.object(history, "fetch_history", side_effect=DraftError("History unavailable")),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, *args, **kwargs: doc if kind == "Patient Encounter" else draft,
			),
		):
			workflow.generate_draft("DRAFT")
		self.assertEqual(draft.status, "Failed")
		self.assertEqual(draft.failure_reason, "History unavailable")
		doc.save.assert_not_called()

	def test_approval_requires_registered_clinician_and_resolved_warnings(self):
		_, _, _, draft = self.make_review()
		self.db.get_value.return_value = None
		with (
			patch.object(workflow, "_review_document", return_value=draft),
			patch.object(frappe, "throw", side_effect=frappe.PermissionError),
			self.assertRaises(frappe.PermissionError),
		):
			workflow.approve_draft("DRAFT", "v1")
		self.db.get_value.return_value = "PRACTITIONER"
		draft.warnings, draft.concerns_resolved = "Check clinical concern", 0
		with patch.object(workflow, "_review_document", return_value=draft), self.assertRaises(DraftError):
			workflow.approve_draft("DRAFT", "v1")

	def test_ready_checkbox_and_status_required(self):
		doc = encounter()
		workflow.assert_eligible(doc, settings())
		for field, value in (
			("auto_pex_notes_ready", 0),
			("docstatus", 1),
			("sr_encounter_status", "Approved"),
		):
			changed = copy.copy(doc)
			setattr(changed, field, value)
			with self.assertRaises(DraftError):
				workflow.assert_eligible(changed, settings())

	def test_feature_disabled_fails_closed(self):
		config = settings()
		config.enabled = 0
		with self.assertRaises(DraftError):
			workflow.assert_eligible(encounter(), config)

	def test_existing_ai_record_protected_when_disabled(self):
		self.db.exists.return_value = True
		with patch.object(workflow, "get_settings", return_value=None):
			self.assertTrue(workflow.protects_prescription(encounter()))

	def test_fresh_route_still_uses_original_mapping(self):
		self.db.exists.return_value = False
		doc = encounter()
		doc.sr_sales_type = "Fresh"
		with patch.object(workflow, "get_settings", return_value=settings()):
			self.assertFalse(workflow.protects_prescription(doc))

	def test_history_hash_tracks_notes_not_internal_status_transition(self):
		value = history.normalize_history(context(), encounter())
		original = workflow.context_hash(value, catalog())
		value["current"]["modified"] = "later"
		value["current"]["status"] = "AI Review"
		self.assertEqual(original, workflow.context_hash(value, catalog()))
		value["previous"][0]["notes"]["sr_notes"] = "New finding"
		self.assertNotEqual(original, workflow.context_hash(value, catalog()))

	def test_duplicate_generation_reuses_pending_draft(self):
		doc = encounter()
		last = frappe._dict(
			name="EXISTING", status="Generating", input_hash=fingerprint(encounter_snapshot(doc))
		)
		with (
			patch.object(workflow, "check_reviewer"),
			patch.object(workflow, "get_settings", return_value=settings()),
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe, "get_all", return_value=[last]),
			patch.object(frappe, "enqueue") as enqueue,
		):
			self.assertEqual(workflow.request_draft("CURRENT", regenerate=1), "EXISTING")
		enqueue.assert_not_called()

	def test_approved_encounter_not_regenerated(self):
		with (
			patch.object(workflow, "check_reviewer"),
			patch.object(workflow, "get_settings", return_value=settings()),
			patch.object(frappe, "get_doc", return_value=encounter()),
			patch.object(frappe, "get_all", return_value=[frappe._dict(status="Approved")]),
			self.assertRaises(DraftError),
		):
			workflow.request_draft("CURRENT", regenerate=1)

	def test_service_write_flag_restored_on_failure(self):
		with self.assertRaises(ValueError), workflow.service_write():
			self.assertTrue(frappe.flags.auto_pex_service_write)
			raise ValueError
		self.assertIsNone(frappe.flags.auto_pex_service_write)

	def test_draft_access_requires_encounter_permission(self):
		doc = Doc(encounter="CURRENT", patient="PATIENT-1", context_json="{}")
		with (
			patch.object(frappe, "get_doc", return_value=encounter()),
			patch.object(frappe, "has_permission", return_value=False),
		):
			self.assertFalse(workflow.draft_permission(doc, user="other", ptype="read"))

	def test_write_permission_requires_reviewer_role(self):
		doc = Doc(encounter="CURRENT", patient="PATIENT-1", context_json="{}")
		with (
			patch.object(frappe, "get_doc", return_value=encounter()),
			patch.object(frappe, "has_permission", return_value=True),
			patch.object(frappe, "get_roles", return_value=["System Manager"]),
		):
			self.assertFalse(workflow.draft_permission(doc, ptype="write"))

	def test_approval_stale_history_never_writes_prescription_or_status(self):
		doc, config = encounter(), settings()
		clinical = history.normalize_history(context(), doc)
		draft = Doc(
			name="DRAFT",
			encounter=doc.name,
			modified="v1",
			status="Ready",
			warnings="",
			expected_status="Draft",
			input_hash=fingerprint(encounter_snapshot(doc)),
			settings_hash=workflow.config_hash(config),
			context_hash="outdated",
			save=MagicMock(),
		)
		with (
			patch.object(workflow, "_review_document", return_value=draft),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(history, "fetch_history", return_value=clinical),
			patch.object(history, "medication_catalog", return_value=catalog()),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, *args, **kwargs: doc if kind == "Patient Encounter" else draft,
			),
		):
			result = workflow.approve_draft("DRAFT", "v1")
		self.assertEqual(result["status"], "Stale")
		doc.save.assert_not_called()
		self.assertEqual(doc.sr_encounter_status, "Draft")


if __name__ == "__main__":
	unittest.main()
