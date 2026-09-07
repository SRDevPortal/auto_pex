from types import SimpleNamespace
from uuid import uuid4

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from auto_pex.api.encounter_hooks import _conditions_match, _set_mapped_encounter_status

# Each database test creates its own records. Following these links otherwise
# loads unrelated ERPNext company/tax fixtures before Auto Pex tests can run.
IGNORE_TEST_RECORD_DEPENDENCIES = [
	"Item",
	"SR Medication Template",
	"Healthcare Practitioner",
	"Diet Chart",
	"SR Encounter Status",
	"SR Sales Type",
]


class _EncounterStub:
	def __init__(self, status, is_new):
		self.sr_encounter_status = status
		self._is_new = is_new

	def is_new(self):
		return self._is_new

	def set(self, fieldname, value):
		setattr(self, fieldname, value)


class _EncounterMetaStub:
	def has_field(self, fieldname):
		return fieldname == "sr_encounter_status"


def _get_unmapped_item_code():
	"""Create a test-owned item instead of depending on live business records."""
	# India Compliance validates HSN/SAC even for non-stock test Items.
	if not frappe.db.exists("GST HSN Code", "999900"):
		frappe.get_doc({"doctype": "GST HSN Code", "hsn_code": "999900"}).insert(ignore_permissions=True)
	suffix = uuid4().hex[:8]
	group = frappe.get_doc(
		{
			"doctype": "Item Group",
			"item_group_name": f"Auto Pex Test {suffix}",
			"parent_item_group": "All Item Groups",
			"is_group": 0,
		}
	).insert(ignore_permissions=True)
	uom = frappe.get_doc({"doctype": "UOM", "uom_name": f"Auto Pex Test {suffix}"}).insert(
		ignore_permissions=True
	)
	return (
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"AUTO-PEX-TEST-{suffix}",
				"item_name": f"Auto Pex Test {suffix}",
				"item_group": group.name,
				"stock_uom": uom.name,
				"is_stock_item": 0,
				"gst_hsn_code": "999900",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def _make_inactive_status_mapping():
	suffix = uuid4().hex[:8]
	status_name = f"AUTO PEX TEST INACTIVE {suffix}"
	item_code = _get_unmapped_item_code()

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

	new_encounter = _EncounterStub(status="Draft", is_new=True)
	_set_mapped_encounter_status(
		new_encounter,
		{"set_encounter_status": "PRX Ready"},
		_EncounterMetaStub(),
	)
	results["configured_status_set_on_new_encounter"] = new_encounter.sr_encounter_status == "PRX Ready"

	existing_encounter = _EncounterStub(status="Ready to Dispatch", is_new=False)
	_set_mapped_encounter_status(
		existing_encounter,
		{"set_encounter_status": "PRX Ready"},
		_EncounterMetaStub(),
	)
	results["existing_encounter_status_preserved"] = (
		existing_encounter.sr_encounter_status == "Ready to Dispatch"
	)

	draft_encounter = _EncounterStub(status="Draft", is_new=False)
	_set_mapped_encounter_status(
		draft_encounter,
		{"encounter_status": "", "set_encounter_status": "PRX Ready"},
		_EncounterMetaStub(),
	)
	results["existing_draft_status_set"] = draft_encounter.sr_encounter_status == "PRX Ready"

	savepoint = "auto_pex_encounter_status_mock_test"
	frappe.db.savepoint(savepoint)
	try:
		mapping, status_name = _make_inactive_status_mapping()
		results["inactive_status_linked"] = (
			mapping.encounter_status == status_name
			and frappe.db.get_value("SR Encounter Status", status_name, "is_active") == 0
		)

		invalid_output_mapping = frappe.get_doc(
			{
				"doctype": "Auto Pex Item Mapping",
				"item": _get_unmapped_item_code(),
				"is_active": 1,
				"set_encounter_status": status_name,
			}
		)
		try:
			invalid_output_mapping.insert(ignore_permissions=True)
		except frappe.ValidationError:
			results["inactive_output_status_rejected"] = True
		else:
			results["inactive_output_status_rejected"] = False
	finally:
		frappe.db.rollback(save_point=savepoint)
	results["mock_data_rolled_back"] = not frappe.db.exists(
		"SR Encounter Status", status_name
	) and not frappe.db.exists("Auto Pex Item Mapping", mapping.name)

	if not all(results.values()):
		raise AssertionError(results)

	return results


class TestAutoPexEncounterStatusCondition(IntegrationTestCase):
	def setUp(self):
		super().setUp()
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

	def test_mapping_rejects_inactive_output_status(self):
		status_name = f"AUTO PEX TEST OUTPUT INACTIVE {uuid4().hex[:8]}"
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
				"item": _get_unmapped_item_code(),
				"is_active": 1,
				"set_encounter_status": status_name,
			}
		)

		with self.assertRaises(frappe.ValidationError):
			mapping.insert(ignore_permissions=True)


class TestAutoPexEncounterStatusOutput(UnitTestCase):
	def setUp(self):
		super().setUp()
		self.meta = _EncounterMetaStub()

	def test_configured_status_is_set_on_new_encounter(self):
		encounter = _EncounterStub(status="Draft", is_new=True)

		_set_mapped_encounter_status(
			encounter,
			{"set_encounter_status": "PRX Ready"},
			self.meta,
		)

		self.assertEqual(encounter.sr_encounter_status, "PRX Ready")

	def test_blank_output_status_leaves_status_unchanged(self):
		encounter = _EncounterStub(status="Draft", is_new=True)

		_set_mapped_encounter_status(encounter, {"set_encounter_status": ""}, self.meta)

		self.assertEqual(encounter.sr_encounter_status, "Draft")

	def test_existing_encounter_status_is_preserved(self):
		encounter = _EncounterStub(status="Ready to Dispatch", is_new=False)

		_set_mapped_encounter_status(
			encounter,
			{"set_encounter_status": "PRX Ready"},
			self.meta,
		)

		self.assertEqual(encounter.sr_encounter_status, "Ready to Dispatch")

	def test_existing_draft_encounter_receives_configured_status(self):
		encounter = _EncounterStub(status="Draft", is_new=False)

		_set_mapped_encounter_status(
			encounter,
			{"encounter_status": "", "set_encounter_status": "PRX Ready"},
			self.meta,
		)

		self.assertEqual(encounter.sr_encounter_status, "PRX Ready")

	def test_explicit_current_status_allows_existing_status_transition(self):
		encounter = _EncounterStub(status="Payment Approved", is_new=False)

		_set_mapped_encounter_status(
			encounter,
			{
				"encounter_status": "Payment Approved",
				"set_encounter_status": "PRX Requested",
			},
			self.meta,
		)

		self.assertEqual(encounter.sr_encounter_status, "PRX Requested")
