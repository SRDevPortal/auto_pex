"""Database-free regression tests for optional encounter mapping and upgrade patches."""

import copy
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import frappe

from auto_pex.api import encounter_hooks as hooks
from auto_pex.patches import set_existing_item_mappings_active as active_patch
from auto_pex.patches import set_existing_mapping_encounter_status as status_patch


class Encounter(SimpleNamespace):
	def get(self, field):
		return getattr(self, field, None)

	def set(self, field, value):
		setattr(self, field, value)

	def append(self, field, value):
		row = frappe._dict(value)
		self.get(field).append(row)
		return row

	def is_new(self):
		return True


class TestEncounterMapping(unittest.TestCase):
	def setUp(self):
		self.doc = Encounter(
			sr_pe_order_items=[frappe._dict(sr_item_code="TEST-ITEM")],
			sr_medication_template="original-template",
			sr_pe_instruction="original-instruction",
			sr_encounter_status="Draft",
			sr_encounter_type="Order",
			sr_sales_type="Fresh",
			drug_prescription=[frappe._dict(drug_code="MANUAL-AYURVEDIC", name="original-row")],
			sr_homeopathy_drug_prescription=[frappe._dict(drug_code="MANUAL-HOMEOPATHIC")],
			sr_allopathy_drug_prescription=[frappe._dict(drug_code="MANUAL-ALLOPATHIC")],
			practitioner="original-practitioner",
			practitioner_name="Original Practitioner",
			diet_chart="original-diet",
		)
		self.mapping = frappe._dict(
			medication_template="mapped-template",
			set_encounter_status="PRX Ready",
			diet_chart="mapped-diet",
		)
		self.template = frappe._dict(
			sr_tmpl_instruction="new instructions",
			sr_medications=[
				frappe._dict(
					sr_medication_class=label,
					sr_drug_code=label,
					sr_dosage="Once",
					sr_instruction="After food",
				)
				for label in ["Ayurvedic Medicine", "Homeopathic Medicine", "Allopathic Medicine"]
			],
		)
		self.db = Mock()
		self.db.get_value.return_value = self.mapping
		self.meta = Mock()
		self.meta.has_field.return_value = True
		for name, value in [
			("db", self.db),
			("get_meta", Mock(return_value=self.meta)),
			("get_doc", Mock(return_value=self.template)),
			("log_error", Mock()),
			("get_traceback", Mock(return_value="mapping traceback")),
		]:
			p = patch.object(hooks.frappe, name, value, create=True)
			p.start()
			self.addCleanup(p.stop)

	def test_zero_and_multiple_items_do_not_change_encounter(self):
		for rows in [[], [frappe._dict(item_code="A"), frappe._dict(item_code="B")]]:
			with self.subTest(rows=rows):
				self.doc.sr_pe_order_items = rows
				before = copy.deepcopy(vars(self.doc))
				hooks.apply_auto_pex_mapping(self.doc)
				self.assertEqual(vars(self.doc), before)
		self.db.get_value.assert_not_called()

	def test_missing_or_inactive_mapping_leaves_encounter_unchanged(self):
		self.db.get_value.return_value = None
		before = copy.deepcopy(vars(self.doc))
		hooks.apply_auto_pex_mapping(self.doc)
		self.assertEqual(vars(self.doc), before)
		self.assertEqual(self.db.get_value.call_args.args[1], {"item": "TEST-ITEM", "is_active": 1})

	def test_item_code_fallback_and_missing_code(self):
		self.assertEqual(hooks._get_item_code({"item_code": "FALLBACK"}), "FALLBACK")
		self.assertEqual(hooks._get_item_code(SimpleNamespace(sr_item_code="FIRST")), "FIRST")
		self.doc.sr_pe_order_items = [frappe._dict()]
		hooks.apply_auto_pex_mapping(self.doc)
		self.db.get_value.assert_not_called()

	def test_conditions_reject_each_mismatch(self):
		for field in ["encounter_type", "sales_type", "encounter_status"]:
			with self.subTest(field=field):
				self.mapping[field] = "does not match"
				before = copy.deepcopy(vars(self.doc))
				hooks.apply_auto_pex_mapping(self.doc)
				self.assertEqual(vars(self.doc), before)
				del self.mapping[field]

	def test_conditions_are_case_insensitive_and_blanks_are_optional(self):
		self.assertTrue(
			hooks._conditions_match(
				self.doc, {"encounter_type": " order ", "sales_type": "fresh", "encounter_status": ""}
			)
		)

	def test_template_routes_all_classes_and_repeat_save_does_not_duplicate(self):
		for _ in range(2):
			hooks.apply_auto_pex_mapping(self.doc)
			for label, field in hooks.DRUG_PRESCRIPTION_TABLES.items():
				rows = self.doc.get(field)
				self.assertEqual(len(rows), 1)
				self.assertEqual(rows[0].drug_code.lower(), label)
				self.assertEqual(rows[0].dosage, "Once")
				self.assertEqual(rows[0].sr_drug_instruction, "After food")
		self.assertEqual(self.doc.sr_medication_template, "mapped-template")
		self.assertEqual(self.doc.sr_pe_instruction, "new instructions")
		self.assertEqual(self.doc.sr_encounter_status, "PRX Ready")
		self.assertEqual(self.doc.diet_chart, "mapped-diet")
		hooks.frappe.log_error.assert_not_called()

	def test_missing_template_restores_all_original_fields(self):
		hooks.frappe.get_doc.side_effect = frappe.DoesNotExistError("missing template")
		before = copy.deepcopy(vars(self.doc))
		hooks.apply_auto_pex_mapping(self.doc)
		self.assertEqual(vars(self.doc), before)
		hooks.frappe.log_error.assert_called_once_with(
			title="auto_pex: apply_auto_pex_mapping error", message="mapping traceback"
		)

	def test_failure_after_first_prescription_restores_original_rows_and_identity(self):
		before = copy.deepcopy(vars(self.doc))
		original_rows = self.doc.drug_prescription
		original_row = original_rows[0]
		with patch.object(
			hooks, "_get_medication_class", side_effect=["Ayurvedic Medicine", RuntimeError("lookup failed")]
		):
			hooks.apply_auto_pex_mapping(self.doc)
		self.assertEqual(vars(self.doc), before)
		self.assertIs(self.doc.drug_prescription, original_rows)
		self.assertIs(self.doc.drug_prescription[0], original_row)

	def test_late_failure_restores_template_practitioner_and_prescriptions(self):
		before = copy.deepcopy(vars(self.doc))

		def fail(doc, mapping, meta):
			doc.set("practitioner", "partially-changed")
			raise RuntimeError("practitioner lookup failed")

		with patch.object(hooks, "_set_practitioner_fields", side_effect=fail):
			hooks.apply_auto_pex_mapping(self.doc)
		self.assertEqual(vars(self.doc), before)

	def test_error_log_failure_does_not_block_save_or_leave_changes(self):
		hooks.frappe.get_doc.side_effect = RuntimeError("template failed")
		hooks.frappe.log_error.side_effect = RuntimeError("error log unavailable")
		before = copy.deepcopy(vars(self.doc))
		with self.assertLogs(hooks.__name__, level="ERROR"):
			hooks.apply_auto_pex_mapping(self.doc)
		self.assertEqual(vars(self.doc), before)

	def test_medication_class_fallback(self):
		self.db.get_value.return_value = "Homeopathic Medicine"
		self.assertEqual(
			hooks._get_medication_class(frappe._dict(sr_medication="MED")), "Homeopathic Medicine"
		)
		self.db.get_value.assert_called_once_with("Medication", "MED", "medication_class")

	def test_legacy_practitioner_maps_to_homeopathy_and_primary_practitioner(self):
		with patch.object(
			hooks, "_get_practitioner_details", return_value={"name": "Dr Test", "reg": "REG-1"}
		):
			hooks._set_practitioner_fields(self.doc, {"healthcare_practitioner": "LEGACY"}, self.meta)
		self.assertEqual(self.doc.sr_homeopathy_practitioner, "LEGACY")
		self.assertEqual(self.doc.sr_homeopathy_practitioner_reg, "REG-1")
		self.assertEqual(self.doc.practitioner, "LEGACY")

	def test_configured_homeopathic_practitioner_overrides_legacy(self):
		with patch.object(hooks, "_get_practitioner_details", return_value={}):
			hooks._set_practitioner_fields(
				self.doc,
				{"healthcare_practitioner": "LEGACY", "homeopathic_practitioner": "NEW"},
				self.meta,
			)
		self.assertEqual(self.doc.practitioner, "NEW")

	def test_later_status_preserved_unless_explicit_transition_matches(self):
		self.doc.is_new = lambda: False
		self.doc.sr_encounter_status = "Payment Approved"
		hooks._set_mapped_encounter_status(self.doc, {"set_encounter_status": "PRX Ready"}, self.meta)
		self.assertEqual(self.doc.sr_encounter_status, "Payment Approved")
		mapping = {"encounter_status": "Payment Approved", "set_encounter_status": "PRX Requested"}
		self.assertTrue(hooks._conditions_match(self.doc, mapping))
		hooks._set_mapped_encounter_status(self.doc, mapping, self.meta)
		self.assertEqual(self.doc.sr_encounter_status, "PRX Requested")


class TestActivationPatch(unittest.TestCase):
	def test_only_unset_values_are_initialized_and_disabled_values_survive_rerun(self):
		connection = sqlite3.connect(":memory:")
		self.addCleanup(connection.close)
		connection.execute("create table `tabAuto Pex Item Mapping` (name text, is_active integer)")
		connection.executemany(
			"insert into `tabAuto Pex Item Mapping` values (?, ?)",
			[("unset", None), ("disabled", 0), ("enabled", 1)],
		)
		db = Mock()
		db.has_column.return_value = True
		db.sql.side_effect = connection.execute
		with patch.object(active_patch.frappe, "db", db, create=True):
			active_patch.execute()
			self.assertEqual(
				connection.execute(
					"select name, is_active from `tabAuto Pex Item Mapping` order by name"
				).fetchall(),
				[("disabled", 0), ("enabled", 1), ("unset", 1)],
			)
			connection.execute("update `tabAuto Pex Item Mapping` set is_active=0 where name='unset'")
			active_patch.execute()
		self.assertEqual(
			connection.execute(
				"select is_active from `tabAuto Pex Item Mapping` where name='unset'"
			).fetchone()[0],
			0,
		)

	def test_missing_column_is_a_noop(self):
		db = Mock()
		db.has_column.return_value = False
		with patch.object(active_patch.frappe, "db", db, create=True):
			active_patch.execute()
		db.sql.assert_not_called()


class TestStatusPatch(unittest.TestCase):
	def test_inactive_existing_status_is_not_assigned_or_reactivated(self):
		db = Mock()
		db.has_column.return_value = True
		db.exists.return_value = True
		db.get_value.return_value = 0
		with (
			patch.object(status_patch.frappe, "db", db, create=True),
			patch.object(status_patch.frappe, "get_all", return_value=["MAPPING"]),
			patch.object(status_patch.frappe, "new_doc") as new_doc,
		):
			status_patch.execute()
		new_doc.assert_not_called()
		db.set_value.assert_not_called()

	def test_no_pending_mappings_do_not_create_a_status(self):
		db = Mock()
		db.has_column.return_value = True
		with (
			patch.object(status_patch.frappe, "db", db, create=True),
			patch.object(status_patch.frappe, "get_all", return_value=[]),
			patch.object(status_patch.frappe, "new_doc") as new_doc,
		):
			status_patch.execute()
		new_doc.assert_not_called()
		db.set_value.assert_not_called()

	def test_rerun_does_not_rewrite_a_completed_mapping(self):
		db = Mock()
		db.has_column.return_value = True
		db.exists.return_value = True
		db.get_value.return_value = 1
		with (
			patch.object(status_patch.frappe, "db", db, create=True),
			patch.object(status_patch.frappe, "get_all", side_effect=[["MAPPING"], []]),
		):
			status_patch.execute()
			status_patch.execute()
		db.set_value.assert_called_once_with(
			"Auto Pex Item Mapping",
			"MAPPING",
			"set_encounter_status",
			"PRX Ready",
			update_modified=False,
		)
