"""Read-only clinical context API and strict remote adapter."""

import json

import frappe
from frappe.utils import add_days, getdate

from auto_pex.ai.provider import request_json, validate_url
from auto_pex.ai.schema import NOTE_FIELDS, RX_FIELDS, TABLES, DraftError

PATIENT_FIELDS = ("sex", "allergies", "medication", "medical_history", "surgical_history")


def _clinical_source(doc):
	doc.apply_fieldlevel_read_permissions()
	return {
		"id": doc.name,
		"kind": "encounter",
		"date": str(doc.encounter_date),
		"modified": str(doc.modified),
		"docstatus": doc.docstatus,
		"status": doc.get("sr_encounter_status") or "",
		"notes": {key: doc.get(key) or "" for key in NOTE_FIELDS},
		"instruction": doc.get("sr_pe_instruction") or "",
		"prescriptions": [
			{"medication_class": category, **{key: row.get(key) for key in RX_FIELDS}}
			for category, table in TABLES.items()
			for row in doc.get(table) or []
		],
	}


@frappe.whitelist(methods=["GET"])
def get_history(encounter, history_days=365, history_limit=20):
	"""Patient identity is derived from the authorized encounter, never a caller-supplied patient."""
	doc = frappe.get_doc("Patient Encounter", encounter)
	doc.check_permission("read")
	patient = frappe.get_doc("Patient", doc.patient)
	patient.check_permission("read")
	patient.apply_fieldlevel_read_permissions()
	days, limit = int(history_days), int(history_limit)
	if not 1 <= days <= 3650 or not 1 <= limit <= 100:
		frappe.throw("History limits are outside the supported range.")
	names = frappe.get_list(
		"Patient Encounter",
		filters={
			"patient": doc.patient,
			"name": ["!=", doc.name],
			"docstatus": ["!=", 2],
			"encounter_date": ["between", [add_days(doc.encounter_date, -days), doc.encounter_date]],
			"creation": ["<", doc.creation],
		},
		fields=["name"],
		order_by="encounter_date desc, creation desc",
		limit_page_length=limit + 1,
	)
	records = []
	for entry in names[:limit]:
		previous = frappe.get_doc("Patient Encounter", entry.name)
		previous.check_permission("read")
		records.append(_clinical_source(previous))
	age = None
	if patient.get("dob"):
		age = round((getdate(doc.encounter_date) - getdate(patient.dob)).days / 365.25, 2)
	return {
		"patient_id": doc.patient,
		"encounter_id": doc.name,
		"patient_history": {
			"id": "patient-history",
			"modified": str(patient.modified),
			"age_years": age,
			**{key: patient.get(key) or "" for key in PATIENT_FIELDS},
		},
		"current": _clinical_source(doc),
		"previous": records,
		"truncated": len(names) > limit,
	}


def normalize_history(data, encounter):
	"""Whitelist clinical keys so arbitrary API fields are never forwarded to OpenAI."""
	try:
		if data["patient_id"] != encounter.patient or data["encounter_id"] != encounter.name:
			raise DraftError("The history API returned a different patient or encounter.")
		previous, current, patient = data["previous"], data["current"], data["patient_history"]
		if not isinstance(previous, list) or len(previous) > 100 or not isinstance(patient, dict):
			raise ValueError
		normalized = []
		ids = set()
		for source in [current, *previous]:
			if not isinstance(source, dict) or not source["id"] or source["id"] in ids:
				raise ValueError
			ids.add(source["id"])
			if source["docstatus"] == 2:
				raise DraftError("Cancelled clinical records cannot be used as prescription sources.")
			if source["docstatus"] not in (0, 1) or not source["modified"]:
				raise ValueError
			if getdate(source["date"]) > getdate(encounter.encounter_date):
				raise DraftError("The history API returned a future clinical record.")
			notes = {key: source["notes"].get(key) or "" for key in NOTE_FIELDS}
			if any(not isinstance(value, str) for value in notes.values()):
				raise ValueError
			rows = source["prescriptions"]
			if not isinstance(rows, list) or len(rows) > 100:
				raise ValueError
			rx = []
			for row in rows:
				if row["medication_class"] not in TABLES:
					raise ValueError
				rx.append(
					{"medication_class": row["medication_class"], **{key: row.get(key) for key in RX_FIELDS}}
				)
			normalized.append(
				{
					"id": str(source["id"]),
					"date": str(source["date"]),
					"modified": str(source["modified"]),
					"docstatus": source["docstatus"],
					"status": str(source.get("status") or ""),
					"notes": notes,
					"prescriptions": rx,
					"instruction": str(source.get("instruction") or ""),
				}
			)
		if current["id"] != encounter.name or "patient-history" in ids:
			raise ValueError
		if not patient.get("modified"):
			raise ValueError
		result = {
			"patient_history": {
				"id": "patient-history",
				"modified": str(patient["modified"]),
				"age_years": patient.get("age_years"),
				**{key: str(patient.get(key) or "") for key in PATIENT_FIELDS},
			},
			"current": normalized[0],
			"previous": sorted(normalized[1:], key=lambda s: (s["date"], s["id"])),
			"truncated": bool(data.get("truncated")),
		}
		if not any(result["current"]["notes"].values()):
			raise DraftError("Current clinical notes are required before AI generation.")
		if not any(record["prescriptions"] for record in result["previous"]):
			raise DraftError("No previous prescription was found in the available history.")
		if len(json.dumps(result, default=str)) > 150_000:
			raise DraftError("Clinical history is too large. Reduce the configured history range.")
		return result
	except (KeyError, TypeError, ValueError, AttributeError):
		raise DraftError(
			"The history API response does not match the documented clinical context contract."
		) from None


def fetch_history(encounter, settings):
	if settings.history_source == "Remote read-only API":
		validate_url(settings.history_url)
		authorization = settings.get_password("history_authorization", raise_exception=False)
		if not authorization:
			raise DraftError("Configure a read-only history API credential.")
		data = request_json(
			"GET",
			settings.history_url,
			headers={"Authorization": authorization},
			timeout=settings.request_timeout,
			retries=settings.retry_limit,
			params={
				"encounter": encounter.name,
				"history_days": settings.history_days,
				"history_limit": settings.history_limit,
			},
		)
		if isinstance(data, dict) and "message" in data:
			data = data["message"]
	else:
		# The same permission-checked GET handler, without a loopback HTTP request or extra credentials.
		data = get_history(encounter.name, settings.history_days, settings.history_limit)
	return normalize_history(data, encounter)


def source_ids(context):
	return {"patient-history", context["current"]["id"], *(row["id"] for row in context["previous"])}


def medication_catalog(context, settings):
	names = {
		row["medication"]
		for source in context["previous"]
		for row in source["prescriptions"]
		if row.get("medication")
	}
	names.update(
		line.strip() for line in (settings.get("additional_medications") or "").splitlines() if line.strip()
	)
	if len(names) > 100:
		raise DraftError("The medication catalog exceeds 100 medications.")
	medications = []
	for name in sorted(names):
		doc = frappe.get_doc("Medication", name)
		doc.check_permission("read")
		if doc.get("disabled") or doc.get("medication_class") not in TABLES:
			continue
		medications.append(
			{
				"medication": name,
				"medication_class": doc.medication_class,
				"strength": doc.get("strength"),
				"strength_uom": doc.get("strength_uom"),
				"dosage_form": doc.get("dosage_form"),
				"drug_codes": sorted(
					{
						row.item
						for row in doc.get("linked_items") or []
						if row.get("item") and not frappe.db.get_value("Item", row.item, "disabled")
					}
				),
			}
		)
	if not medications:
		raise DraftError("No active, supported medications were found for the clinical history.")
	catalog = {"medications": medications, "rules": json.loads(settings.get("medication_rules") or "{}")}
	for key, doctype in (
		("dosages", "Prescription Dosage"),
		("periods", "Prescription Duration"),
		("dosage_forms", "Dosage Form"),
	):
		values = frappe.get_list(doctype, pluck="name", order_by="name", limit_page_length=501)
		if len(values) > 500:
			raise DraftError("A prescription reference catalog exceeds the supported size.")
		catalog[key] = values
	catalog["dosage_definitions"] = []
	for name in catalog["dosages"]:
		dosage = frappe.get_doc("Prescription Dosage", name)
		dosage.check_permission("read")
		catalog["dosage_definitions"].append(
			{
				"name": name,
				"schedule": [
					{key: row.get(key) for key in ("strength", "strength_time")}
					for row in dosage.get("dosage_strength") or []
				],
			}
		)
	catalog["duration_definitions"] = []
	for name in catalog["periods"]:
		duration = frappe.get_doc("Prescription Duration", name)
		duration.check_permission("read")
		catalog["duration_definitions"].append(
			{"name": name, "number": duration.number, "unit": duration.period}
		)
	return catalog


def validate_catalog(proposal, catalog):
	allowed = {row["medication"]: row for row in catalog["medications"]}
	for row in proposal["items"]:
		medicine = allowed.get(row["medication"])
		if not medicine or medicine["medication_class"] != row["medication_class"]:
			raise DraftError("A proposed medication is not in the permitted catalog or has the wrong class.")
		if row["drug_code"] and row["drug_code"] not in medicine["drug_codes"]:
			raise DraftError("A drug code does not belong to its medication or is disabled.")
		if (
			row["dosage"] not in catalog["dosages"]
			or row["period"] not in catalog["periods"]
			or row["dosage_form"] not in catalog["dosage_forms"]
			or (medicine["dosage_form"] and row["dosage_form"] != medicine["dosage_form"])
		):
			raise DraftError("A dosage, duration or dosage form is not valid for this medication.")
		rule = catalog.get("rules", {}).get(row["medication"], {})
		if rule.get("blocked") or any(
			row[field] not in rule[allowed]
			for field, allowed in (("dosage", "dosages"), ("period", "periods"), ("drug_code", "drug_codes"))
			if allowed in rule
		):
			raise DraftError("A medication row violates the configured prescribing constraints.")
