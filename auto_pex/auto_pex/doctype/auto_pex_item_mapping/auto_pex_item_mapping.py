# Copyright (c) 2026, SRIAAS and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AutoPexItemMapping(Document):
	def validate(self):
		self.validate_practitioner_pathy()
		self.validate_set_encounter_status()

	def validate_set_encounter_status(self):
		target_status = self.get("set_encounter_status")
		if not target_status:
			return

		is_active = frappe.db.get_value("SR Encounter Status", target_status, "is_active")
		if not is_active:
			frappe.throw(
				_("Set Encounter Status must be active. Selected status {0} is inactive.").format(
					frappe.bold(target_status)
				),
				title=_("Invalid Encounter Status"),
			)

	def validate_practitioner_pathy(self):
		if not frappe.db.has_column("Healthcare Practitioner", "sr_pathy"):
			return

		pathy_by_field = {
			"ayurvedic_practitioner": "Ayurveda",
			"homeopathic_practitioner": "Homeopathy",
			"allopathic_practitioner": "Allopathy",
			"healthcare_practitioner": "Homeopathy",
		}

		for fieldname, expected_pathy in pathy_by_field.items():
			practitioner = self.get(fieldname)
			if not practitioner:
				continue

			actual_pathy = frappe.db.get_value(
				"Healthcare Practitioner",
				practitioner,
				"sr_pathy",
			)
			if actual_pathy != expected_pathy:
				label = self.meta.get_label(fieldname) or fieldname
				frappe.throw(
					_("{0} must have Pathy {1}. Selected practitioner {2} has Pathy {3}.").format(
						_(label),
						frappe.bold(expected_pathy),
						frappe.bold(practitioner),
						frappe.bold(actual_pathy or _("blank")),
					),
					title=_("Invalid Practitioner"),
				)
