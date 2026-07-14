from types import SimpleNamespace
from uuid import uuid4

import frappe
from frappe.tests.utils import FrappeTestCase

from auto_pex.api.encounter_hooks import _conditions_match


def _make_inactive_status_mapping():
	suffix = uuid4().hex[:8]
	status_name = f"AUTO PEX TEST INACTIVE {suffix}"
	item_code = frappe.db.sql(
		"""
		select item.name
		from `tabItem` item
		left join `tabAuto Pex Item Mapping` mapping on mapping.item = item.name
		where mapping.name is null
		limit 1
		"""
	)[0][0]

	frappe.get_doc(
		{
			"doctype": "SR Encounter Status",
			"sr_status_name": status_name,
			"is_active": 0,
		}
	).insert(ignore_permissions=True)

	mapping = frappe.get_doc(
		{
			"doctype": "Auto Pex Item Mapping",
			"item": item_code,
			"is_active": 1,
			"encounter_type": "Order",
			"sales_type": "Fresh",
			"encounter_status": status_name,
		}
	).insert(ignore_permissions=True)

	return mapping, status_name


def run_mock_test():
	"""Run a focused integration check through `bench execute` without global test fixtures."""
	encounter = SimpleNamespace(
		sr_encounter_type="Order",
		sr_sales_type="Fresh",
		sr_encounter_status="Draft",
	)
	matching = {
		"encounter_type": "Order",
		"sales_type": "Fresh",
		"encounter_status": "Draft",
	}
	blank_status = {**matching, "encounter_status": ""}
	mismatched_status = {**matching, "encounter_status": "PRX Ready"}
	case_insensitive = {
		"encounter_type": "order",
		"sales_type": "fresh",
		"encounter_status": "draft",
	}

	results = {
		"all_conditions_match": _conditions_match(encounter, matching),
		"different_status_rejected": not _conditions_match(encounter, mismatched_status),
		"blank_status_skipped": _conditions_match(encounter, blank_status),
		"case_insensitive_match": _conditions_match(encounter, case_insensitive),
	}

	savepoint = "auto_pex_encounter_status_mock_test"
	frappe.db.savepoint(savepoint)
	try:
		mapping, status_name = _make_inactive_status_mapping()
		results["inactive_status_linked"] = (
			mapping.encounter_status == status_name
			and frappe.db.get_value("SR Encounter Status", status_name, "is_active") == 0
		)
	finally:
		frappe.db.rollback(save_point=savepoint)
	results["mock_data_rolled_back"] = not frappe.db.exists(
		"SR Encounter Status", status_name
	) and not frappe.db.exists("Auto Pex Item Mapping", mapping.name)

	if not all(results.values()):
		raise AssertionError(results)

	return results


class TestAutoPexEncounterStatusCondition(FrappeTestCase):
	def setUp(self):
		self.encounter = SimpleNamespace(
			sr_encounter_type="Order",
			sr_sales_type="Fresh",
			sr_encounter_status="Draft",
		)

	def test_all_populated_conditions_must_match(self):
		mapping = {
			"encounter_type": "Order",
			"sales_type": "Fresh",
			"encounter_status": "Draft",
		}

		self.assertTrue(_conditions_match(self.encounter, mapping))

		mapping["encounter_status"] = "PRX Ready"
		self.assertFalse(_conditions_match(self.encounter, mapping))

	def test_blank_status_is_skipped_and_matching_is_case_insensitive(self):
		self.assertTrue(
			_conditions_match(
				self.encounter,
				{
					"encounter_type": "order",
					"sales_type": "fresh",
					"encounter_status": "",
				},
			)
		)

	def test_mapping_accepts_inactive_encounter_status(self):
		mapping, status_name = _make_inactive_status_mapping()

		self.assertEqual(mapping.encounter_status, status_name)
		self.assertEqual(
			frappe.db.get_value("SR Encounter Status", status_name, "is_active"),
			0,
		)
