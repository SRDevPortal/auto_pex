"""Strict, bounded pasted CSV configuration. No clinical content is supplied by code."""

import csv
import io
import json

import frappe

from auto_pex.ai.schema import TABLES, DraftError

MAX_CATALOG_BYTES = 500_000
MAX_INSTRUCTION_BYTES = 200_000
MAX_REQUEST_BYTES = 2_000_000
MAX_MEDICINES = 150
COLUMNS = {"code", "medication_id", "name", "formulation", "drug_code"}
csv.field_size_limit(max(csv.field_size_limit(), MAX_CATALOG_BYTES))


def check_size(value, maximum, label):
	if not isinstance(value, str):
		raise DraftError(f"{label} must be text.")
	if len(value.encode("utf-8")) > maximum:
		raise DraftError(f"{label} exceeds the {maximum:,}-byte UTF-8 limit. Nothing was truncated.")
	if "\x00" in value:
		raise DraftError(f"{label} contains a null character.")


def parse_catalog(text):
	check_size(text, MAX_CATALOG_BYTES, "Medicine catalog")
	if not text.strip():
		return []
	reader = csv.reader(io.StringIO(text.lstrip("\ufeff"), newline=""), strict=True)
	try:
		headers = next(reader)
		if (
			len(headers) != len(set(headers))
			or set(headers) - COLUMNS
			or not {"medication_id", "formulation"}.issubset(headers)
		):
			raise DraftError(
				"Catalog CSV requires medication_id and formulation columns; optional columns are code, name, drug_code. Headers must be unique and exact."
			)
		rows, codes, medications = [], set(), set()
		for values in reader:
			if not values:
				continue
			if len(values) != len(headers):
				raise DraftError(f"Catalog CSV line {reader.line_num} has the wrong number of columns.")
			row = dict(zip(headers, values, strict=True))
			for key in ("medication_id", "code", "drug_code"):
				if key in row:
					row[key] = row[key].strip()
			if not row["medication_id"] or not row["formulation"].strip():
				raise DraftError(f"Catalog CSV line {reader.line_num} needs a Medication ID and formulation.")
			code = row.get("code", "").casefold()
			if row["medication_id"] in medications or (code and code in codes):
				raise DraftError(f"Catalog CSV line {reader.line_num} duplicates a Medication ID or code.")
			medications.add(row["medication_id"])
			if code:
				codes.add(code)
			rows.append(row)
			if len(rows) > MAX_MEDICINES:
				raise DraftError("The medication catalog exceeds 150 medications.")
		if not rows:
			raise DraftError("Catalog CSV contains a header but no medicines.")
		ids = {row["medication_id"].casefold(): row["medication_id"] for row in rows}
		for row in rows:
			if row.get("code", "").casefold() in ids and ids[row["code"].casefold()] != row["medication_id"]:
				raise DraftError("A catalog code conflicts with another exact Medication ID.")
		return rows
	except (csv.Error, StopIteration):
		raise DraftError(
			f"Malformed catalog CSV near line {reader.line_num}. Quote commas and multiline values."
		) from None


def additional_ids(settings):
	return {
		line.strip() for line in (settings.get("additional_medications") or "").splitlines() if line.strip()
	}


def validate_mapping(row):
	try:
		doc = frappe.get_doc("Medication", row["medication_id"])
	except frappe.DoesNotExistError:
		raise DraftError(
			"A catalog mapping references a Medication ID that does not exist. Use exact ERP IDs."
		) from None
	doc.check_permission("read")
	if doc.name != row["medication_id"]:
		raise DraftError("Catalog mappings must use exact ERP Medication IDs, including letter case.")
	if doc.get("disabled") or doc.get("medication_class") not in TABLES:
		raise DraftError("A pasted catalog mapping references a disabled or unsupported medication.")
	if row.get("drug_code"):
		linked = {item.item for item in doc.get("linked_items") or [] if item.get("item")}
		if row["drug_code"] not in linked or frappe.db.get_value("Item", row["drug_code"], "disabled"):
			raise DraftError("A catalog drug_code is disabled or does not belong to its exact Medication ID.")
	return doc


def validate_settings_content(settings, *, require_content=False):
	instructions = settings.get("prescribing_constraints") or ""
	check_size(instructions, MAX_INSTRUCTION_BYTES, "Prescription instructions")
	rows = parse_catalog(settings.get("medicine_catalog") or "")
	if require_content and (not rows or not instructions.strip()):
		raise DraftError(
			"Populate the medicine catalog and prescription instructions before generating AI drafts."
		)
	if len({row["medication_id"] for row in rows} | additional_ids(settings)) > MAX_MEDICINES:
		raise DraftError("The combined medication catalog exceeds 150 medications.")
	return rows


def audit_configuration(settings, instructions):
	from auto_pex.ai.reports import EXTRACTION_PROMPT, READER_VERSION

	# Never snapshot API keys or authorization headers.
	return json.dumps(
		{
			"model": settings.model,
			"prescribing_constraints": settings.get("prescribing_constraints") or "",
			"medicine_catalog": settings.get("medicine_catalog") or "",
			"instructions_sent": instructions,
			"report_reader_version": READER_VERSION,
			"report_reader_instructions": EXTRACTION_PROMPT,
			"additional_medications": settings.get("additional_medications") or "",
			"medication_rules": settings.get("medication_rules") or "{}",
			"history_days": settings.get("history_days"),
			"history_limit": settings.get("history_limit"),
		},
		ensure_ascii=False,
	)
