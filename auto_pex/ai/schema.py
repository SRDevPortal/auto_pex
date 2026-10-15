"""Transport schema and deterministic checks; no patient data or API calls at import."""

import hashlib
import json

import frappe
from jsonschema import Draft202012Validator

TABLES = {
	"Ayurvedic Medicine": "drug_prescription",
	"Homeopathic Medicine": "sr_homeopathy_drug_prescription",
	"Allopathic Medicine": "sr_allopathy_drug_prescription",
}
NOTE_FIELDS = ("sr_complaints", "sr_observations", "sr_investigations", "sr_diagnosis", "sr_notes")
RX_FIELDS = (
	"medication",
	"drug_code",
	"dosage",
	"period",
	"dosage_form",
	"strength",
	"strength_uom",
	"dosage_by_interval",
	"interval",
	"interval_uom",
	"sr_drug_instruction",
	"comment",
)
PROMPT_VERSION = "followup-v2-current-order-instructions"


def object_schema(properties):
	return {
		"type": "object",
		"properties": properties,
		"required": list(properties),
		"additionalProperties": False,
	}


TEXT = {"type": "string"}
TEXT_LIST = {"type": "array", "items": TEXT}
ITEM_SCHEMA = object_schema(
	{
		"medication_class": {"type": "string", "enum": list(TABLES)},
		"medication": TEXT,
		"drug_code": TEXT,
		"dosage": TEXT,
		"period": TEXT,
		"dosage_form": TEXT,
		"instruction": TEXT,
		"rationale": TEXT,
		"source_ids": TEXT_LIST,
	}
)
PROPOSAL_SCHEMA = object_schema(
	{
		"outcome": {"type": "string", "enum": ["draft", "unable_to_draft"]},
		"summary": TEXT,
		"instruction": TEXT,
		"warnings": TEXT_LIST,
		"missing_information": TEXT_LIST,
		"items": {"type": "array", "items": ITEM_SCHEMA},
	}
)


class DraftError(frappe.ValidationError):
	"""Only fixed, non-patient error messages should be passed to this exception."""


def fingerprint(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
	).hexdigest()


def matches_route(encounter):
	return all(
		str(encounter.get(key) or "").strip().lower() == expected
		for key, expected in (
			("sr_encounter_type", "order"),
			("sr_encounter_place", "online"),
			("sr_sales_type", "repeat"),
		)
	)


def encounter_snapshot(doc):
	fields = (
		"patient",
		"company",
		"encounter_date",
		"sr_encounter_type",
		"sr_encounter_place",
		"sr_sales_type",
		"auto_pex_notes_ready",
		"submit_orders_on_save",
		"sr_pe_instruction",
		*NOTE_FIELDS,
	)
	result = {field: doc.get(field) for field in fields}
	result["order_items"] = [
		{
			key: row.get(key)
			for key in ("sr_item_code", "item_code", "sr_item_qty", "sr_item_rate", "qty", "rate")
		}
		for row in doc.get("sr_pe_order_items") or []
	]
	for table in TABLES.values():
		result[table] = [{key: row.get(key) for key in RX_FIELDS} for row in doc.get(table) or []]
	return result


def validate_proposal(proposal, source_ids, max_items=20):
	if list(Draft202012Validator(PROPOSAL_SCHEMA).iter_errors(proposal)):
		raise DraftError("The AI response does not match the prescription schema.")
	items = proposal["items"]
	if len(items) > max_items or (proposal["outcome"] == "draft" and not items):
		raise DraftError("The prescription has an invalid number of medication rows.")
	if proposal["outcome"] == "unable_to_draft" and items:
		raise DraftError("An unable-to-draft response must not contain medication rows.")
	seen = set()
	for row in items:
		if any(
			not row[key].strip()
			for key in ("medication", "drug_code", "dosage", "period", "dosage_form", "rationale")
		):
			raise DraftError("A medication row is missing a required value.")
		key = row["medication"].strip().casefold()
		if key in seen:
			raise DraftError("Duplicate medication rows require manual reconciliation.")
		seen.add(key)
		if not row["source_ids"] or not set(row["source_ids"]).issubset(source_ids):
			raise DraftError("A medication row contains missing or unknown source references.")
	return proposal
