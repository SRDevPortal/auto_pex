"""Regression coverage for pasted configuration, audit evidence and complete prescriptions."""

import copy
import json
from unittest.mock import MagicMock, patch

import frappe

from auto_pex.ai import history, pasted_catalog, provider, test_followup, workflow
from auto_pex.ai.schema import DraftError, encounter_snapshot, fingerprint, validate_proposal
from auto_pex.ai.test_followup import Base, Doc, catalog, context, encounter, proposal, settings


class CatalogRequirementsTests(Base):
	def test_quoted_multiline_unicode_and_large_field_roundtrip(self):
		formulation = 'Team text, "quoted"\nSecond line — ' + "x" * 140_000
		import csv
		import io

		stream = io.StringIO(newline="")
		writer = csv.writer(stream)
		writer.writerow(["code", "medication_id", "name", "formulation"])
		writer.writerow(["M1", "MED-1", "Synthetic", formulation])
		self.assertEqual(pasted_catalog.parse_catalog(stream.getvalue())[0]["formulation"], formulation)

	def test_malformed_ambiguous_and_oversized_csv_fails_visibly(self):
		for text in (
			'medication_id,formulation\nMED-1,"unclosed',
			"medication_id,formulation\nMED-1,unquoted,comma",
			"medication_id,medication_id,formulation\nMED-1,MED-1,text",
			"code,formulation\nM1,text",
			"medication_id,formulation\nMED-1,",
			"medication_id,formulation\nMED-1,a\nMED-1,b",
			"code,medication_id,formulation\nx,MED-1,a\nX,MED-2,b",
			"code,medication_id,formulation\nMED-2,MED-1,a\ny,MED-2,b",
			"medication_id,formulation\n",
			"x" * (pasted_catalog.MAX_CATALOG_BYTES + 1),
		):
			with self.subTest(text=text[:60]), self.assertRaises(DraftError):
				pasted_catalog.parse_catalog(text)

	def test_size_checks_count_utf8_bytes_and_do_not_slice(self):
		config = settings()
		config.prescribing_constraints = "é" * (pasted_catalog.MAX_INSTRUCTION_BYTES // 2 + 1)
		with self.assertRaisesRegex(DraftError, "Nothing was truncated"):
			pasted_catalog.validate_settings_content(config)

	def test_missing_disabled_and_incorrect_item_mapping_rejected(self):
		with patch.object(frappe, "get_doc", side_effect=frappe.DoesNotExistError):
			with self.assertRaisesRegex(DraftError, "exact ERP IDs"):
				pasted_catalog.validate_mapping({"medication_id": "MISSING"})
		medicine = Doc(
			name="MED-1", disabled=1, medication_class="Ayurvedic Medicine", check_permission=MagicMock()
		)
		with patch.object(frappe, "get_doc", return_value=medicine):
			with self.assertRaisesRegex(DraftError, "letter case"):
				pasted_catalog.validate_mapping({"medication_id": "med-1"})
			with self.assertRaisesRegex(DraftError, "disabled"):
				pasted_catalog.validate_mapping({"medication_id": "MED-1"})
			medicine.disabled = 0
			medicine.linked_items = [Doc(item="ITEM-1")]
			with self.assertRaisesRegex(DraftError, "drug_code"):
				pasted_catalog.validate_mapping({"medication_id": "MED-1", "drug_code": "ITEM-2"})

	def test_combined_limit_counts_all_three_sources_before_filtering(self):
		config = settings()
		config.medicine_catalog += "PASTED-2,Team text\n"
		config.additional_medications = "\n".join(f"EXTRA-{i}" for i in range(149))
		with self.assertRaisesRegex(DraftError, "150"):
			pasted_catalog.validate_settings_content(config)
		config.additional_medications = "\n".join(f"EXTRA-{i}" for i in range(148))
		clinical = context()
		clinical["previous"][0]["prescriptions"].append({"medication": "HISTORY-ONLY"})
		with self.assertRaisesRegex(DraftError, "150"), patch.object(frappe, "get_doc") as get_doc:
			history.medication_catalog(clinical, config)
		get_doc.assert_not_called()

	def test_explicit_catalog_item_and_enforced_rules_take_precedence(self):
		value = catalog()
		value["medications"][0]["drug_codes"].append("ITEM-2")
		value["medications"][0]["pasted_knowledge"] = {"drug_code": "ITEM-2"}
		with self.assertRaisesRegex(DraftError, "explicit pasted"):
			history.validate_catalog(proposal(), value)
		value["medications"][0]["pasted_knowledge"] = {"drug_code": "ITEM-1"}
		value["rules"] = {"MED-1": {"blocked": True}}
		with self.assertRaisesRegex(DraftError, "constraints"):
			history.validate_catalog(proposal(), value)

	def test_actual_request_contains_complete_saved_text_and_history(self):
		config = settings()
		config.medicine_catalog = 'code,medication_id,formulation\nM1,MED-1,"First, line\nSecond line"\n'
		config.prescribing_constraints = "Team instructions\n" + "z" * 140_000
		response = {
			"status": "completed",
			"output": [
				{"type": "message", "content": [{"type": "output_text", "text": json.dumps(proposal())}]}
			],
		}
		with patch.object(provider, "request_json", return_value=response) as request:
			provider.generate(context(), catalog(), config)
		payload = request.call_args.kwargs["json"]
		self.assertTrue(payload["instructions"].endswith(config.prescribing_constraints))
		self.assertEqual(json.loads(payload["input"])["medicine_catalog_text"], config.medicine_catalog)
		self.assertEqual(json.loads(payload["input"])["clinical_context"], context())
		with patch.object(provider, "request_json") as request:
			with self.assertRaisesRegex(DraftError, "Complete AI request"):
				provider.generate({"oversized": "x" * pasted_catalog.MAX_REQUEST_BYTES}, catalog(), config)
		request.assert_not_called()

	def test_remote_context_respects_configured_limits_and_flags_truncation(self):
		clinical = context()
		clinical["previous"].append(
			{**copy.deepcopy(clinical["previous"][0]), "id": "OLDER", "date": "2026-09-01"}
		)
		limited = history.normalize_history(clinical, encounter(), history_days=365, history_limit=1)
		self.assertTrue(limited["truncated"])
		self.assertEqual(len(limited["previous"]), 1)
		limited = history.normalize_history(clinical, encounter(), history_days=10, history_limit=20)
		self.assertTrue(limited["truncated"])
		self.assertEqual(limited["previous"][0]["id"], "PREV-1")

	def test_quantity_and_unit_validation_and_snapshot_freshness(self):
		for quantity in (0, -1, float("nan"), float("inf"), True, "30"):
			value = proposal()
			value["items"][0]["dispensing_quantity"] = quantity
			with self.subTest(quantity=quantity), self.assertRaises(DraftError):
				validate_proposal(value, {"PREV-1"})
		value = proposal()
		value["items"][0]["dispensing_uom"] = "INVENTED"
		with self.assertRaises(DraftError):
			history.validate_catalog(value, catalog())
		doc = encounter()
		doc.drug_prescription = [Doc(auto_pex_dispensing_quantity=1, auto_pex_dispensing_uom="Nos")]
		before = fingerprint(encounter_snapshot(doc))
		doc.drug_prescription[0].auto_pex_dispensing_quantity = 2
		self.assertNotEqual(before, fingerprint(encounter_snapshot(doc)))

	def test_catalog_or_prompt_changes_block_approval(self):
		for field in ("medicine_catalog", "prescribing_constraints"):
			doc, config, _, draft = test_followup.WorkflowTests.make_review(self)
			setattr(config, field, getattr(config, field) + "\nChanged")
			with (
				patch.object(workflow, "_review_document", return_value=draft),
				patch.object(workflow, "get_settings", return_value=config),
				patch.object(frappe, "get_doc", return_value=doc),
				self.assertRaisesRegex(DraftError, "settings changed"),
			):
				workflow.approve_draft("DRAFT", "v1")
			doc.save.assert_not_called()

	def test_unchanged_encounter_replaces_draft_after_config_or_history_change(self):
		for change in ("configuration", "history"):
			doc, config, clinical, draft = test_followup.WorkflowTests.make_review(self)
			last = frappe._dict(
				name="DRAFT",
				status="Ready",
				input_hash=draft.input_hash,
				settings_hash=draft.settings_hash,
				context_hash=draft.context_hash,
			)
			if change == "configuration":
				config.prescribing_constraints += "\nNew instructions"
			else:
				clinical["previous"][0]["notes"]["sr_investigations"] = "Changed result"
			new = MagicMock()
			new.insert.return_value.name = "NEW"
			with (
				patch.object(workflow, "check_reviewer"),
				patch.object(workflow, "get_settings", return_value=config),
				patch.object(frappe, "get_all", return_value=[last]),
				patch.object(frappe, "get_doc", side_effect=[doc, draft, new]),
				patch.object(history, "fetch_history", return_value=clinical),
				patch.object(history, "medication_catalog", return_value=catalog()),
				patch.object(frappe, "enqueue") as enqueue,
			):
				self.assertEqual(workflow.request_draft(doc.name), "NEW")
			self.assertEqual(draft.status, "Stale")
			enqueue.assert_called_once()

	def test_upgrade_disables_automatic_generation_without_touching_team_content(self):
		from auto_pex.patches.disable_auto_generation_for_catalog_setup import execute

		with patch.object(frappe, "clear_cache"):
			execute()
		self.db.set_single_value.assert_called_once_with("Auto Pex AI Settings", "auto_generate", 0)
