"""Permission-checked lab history and a strict clinical-only transport contract."""

import math
from datetime import date, datetime, time

import frappe
from frappe.utils import add_days, getdate

from auto_pex.ai.schema import DraftError

SOURCE_FIELDS = {
	"Lab Test": ("lab_test_name", "lab_test_comment", "custom_result", "descriptive_result", "result_legend"),
	"Observation": (
		"observation_template",
		"preferred_display_name",
		"observation_category",
		"parent_observation",
		"permitted_data_type",
		"result_data",
		"result_text",
		"result_select",
		"result_boolean",
		"result_time",
		"result_datetime",
		"result_period_from",
		"result_period_to",
		"permitted_unit",
		"reference",
		"result_interpretation",
		"note",
		"attachment_present",
	),
}
RESULT_TABLES = {
	"normal_test_items": (
		"lab_test_name",
		"lab_test_event",
		"result_value",
		"lab_test_uom",
		"normal_range",
		"lab_test_comment",
		"secondary_uom",
		"secondary_uom_result",
		"conversion_factor",
	),
	"descriptive_test_items": ("lab_test_particulars", "result_value"),
	"organism_test_items": ("organism", "colony_population", "colony_uom"),
	"sensitivity_test_items": ("antibiotic", "antibiotic_sensitivity"),
}
DATE_FIELDS = {
	"Lab Test": ("result_date", "date", "creation"),
	"Observation": ("time_of_result", "posting_date", "creation"),
}
EXCLUDED_STATUSES = ("Cancelled", "Rejected", "Entered in Error")
FINAL_STATUSES = {"Lab Test": {"Approved"}, "Observation": {"Final", "Approved", "Amended", "Corrected"}}


def fetch(encounter, days, limit):
	"""Bound each date-fallback query, then apply one combined limit across both source types."""
	candidates = []
	start, end = str(add_days(encounter.encounter_date, -days)), str(add_days(encounter.encounter_date, 1))
	for doctype, date_fields in DATE_FIELDS.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		for index, field in enumerate(date_fields):
			filters = [
				["patient", "=", encounter.patient],
				["docstatus", "!=", 2],
				["status", "not in", list(EXCLUDED_STATUSES)],
				[field, ">=", start],
				[field, "<", end],
			]
			filters.extend([earlier, "is", "not set"] for earlier in date_fields[:index])
			if doctype == "Observation":
				filters.append(["observation_category", "=", "Laboratory"])
			rows = frappe.get_list(
				doctype,
				filters=filters,
				fields=["name", field],
				order_by=f"{field} desc, name desc",
				limit_page_length=limit + 1,
			)
			candidates.extend((str(row[field]), doctype, row.name) for row in rows)
	candidates.sort(reverse=True)
	sources = []
	for _, doctype, name in candidates[:limit]:
		doc = frappe.get_doc(doctype, name)
		doc.check_permission("read")
		if doc.patient != encounter.patient:
			raise DraftError("An investigation belongs to a different patient.")
		doc.apply_fieldlevel_read_permissions()
		date = next((doc.get(field) for field in DATE_FIELDS[doctype] if doc.get(field)), None)
		if not date:
			raise DraftError("The investigation date is not readable.")
		details = {key: doc.get(key) for key in SOURCE_FIELDS[doctype]}
		if doctype == "Observation":
			details["attachment_present"] = bool(doc.get("result_attach"))
		else:
			for table, fields in RESULT_TABLES.items():
				details[table] = [{key: row.get(key) for key in fields} for row in doc.get(table) or []]
		sources.append(
			{
				"id": f"{doctype}:{name}",
				"doctype": doctype,
				"name": name,
				"patient_id": encounter.patient,
				"date": str(getdate(date)),
				"modified": str(doc.modified),
				"docstatus": doc.docstatus,
				"status": doc.get("status") or "",
				"details": details,
			}
		)
	return sources, len(candidates) > limit


def _values(data, fields):
	if not isinstance(data, dict):
		raise ValueError
	result = {}
	for key in fields:
		value = data.get(key)
		if value is None:
			value = ""
		if isinstance(value, date | datetime | time):
			value = value.isoformat()
		if not isinstance(value, str | int | float | bool) or (
			isinstance(value, float) and not math.isfinite(value)
		):
			raise ValueError
		result[key] = value
	return result


def normalize(sources, encounter, days, limit, used_ids):
	if not isinstance(sources, list) or len(sources) > 100:
		raise ValueError
	result = []
	for source in sources:
		doctype, name = source["doctype"], source["name"]
		if doctype not in SOURCE_FIELDS or not isinstance(name, str) or not name:
			raise ValueError
		if source["patient_id"] != encounter.patient:
			raise DraftError("The history API returned an investigation for a different patient.")
		if source["id"] != f"{doctype}:{name}" or source["id"] in used_ids:
			raise ValueError
		used_ids.add(source["id"])
		if source["docstatus"] not in (0, 1) or source["status"] in EXCLUDED_STATUSES:
			raise DraftError("Cancelled, rejected or erroneous investigation results cannot be used.")
		if (
			not isinstance(source["status"], str)
			or not isinstance(source["modified"], str)
			or not source["modified"]
			or not source["date"]
		):
			raise ValueError
		date = getdate(source["date"])
		if date > getdate(encounter.encounter_date):
			raise DraftError("The history API returned a future investigation result.")
		details = _values(source["details"], SOURCE_FIELDS[doctype])
		if doctype == "Observation":
			if details["observation_category"] != "Laboratory" or not isinstance(
				details["attachment_present"], bool
			):
				raise ValueError
		else:
			for table, fields in RESULT_TABLES.items():
				rows = source["details"].get(table, [])
				if not isinstance(rows, list) or len(rows) > 1000:
					raise DraftError(
						"An investigation result table exceeds the supported size. Nothing was truncated."
					)
				details[table] = [_values(row, fields) for row in rows]
		result.append(
			{
				"id": source["id"],
				"doctype": doctype,
				"name": name,
				"date": str(date),
				"modified": str(source["modified"]),
				"docstatus": source["docstatus"],
				"status": str(source["status"]),
				"details": details,
			}
		)
	within_window = [
		row
		for row in result
		if days is None or getdate(row["date"]) >= getdate(add_days(encounter.encounter_date, -int(days)))
	]
	within_window.sort(key=lambda row: (row["date"], row["id"]), reverse=True)
	bounded = within_window[: int(limit)] if limit is not None else within_window
	return list(reversed(bounded)), len(bounded) != len(result)


def warnings(context):
	sources = context.get("investigations", [])
	result = []
	if any(row["status"] not in FINAL_STATUSES[row["doctype"]] for row in sources):
		result.append(
			"Investigation history includes pending or unapproved results. Verify their status before approval."
		)
	if any(row["details"].get("attachment_present") for row in sources):
		result.append(
			"An investigation has an attachment. File contents were not read; review the original report."
		)
	return result
