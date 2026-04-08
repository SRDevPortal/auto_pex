# auto_pex/api/encounter_hooks.py
"""
Auto Pex — Patient Encounter before_save hook.

Logic:
  - Look up the Auto Pex Item Mapping for the single draft-invoice item.
  - Check Validation Conditions configured on the mapping record:
      • encounter_type — if set, Patient Encounter's sr_encounter_type must match
      • sales_type     — if set, Patient Encounter's sr_sales_type must match
    Blank conditions are skipped (not enforced).
  - If conditions pass, automatically:
      1. Set `sr_medication_template` on the encounter.
      2. Copy SR Medication Template rows → `sr_homeopathy_drug_prescription` child table.
      3. Set `sr_homeopathy_practitioner` / `practitioner` on the encounter.
      4. Set `diet_chart` on the encounter (if mapped).
  - If 0 or 2+ draft-invoice items exist, exit silently — no changes.
"""

import frappe


# ── field names ──────────────────────────────────────────────────────────────
# Draft-invoice child table on Patient Encounter
DRAFT_INVOICE_TABLE = "sr_pe_order_items"

# Possible fieldnames for the item code column on each child row
ITEM_CODE_FIELDS = ["sr_item_code", "item_code"]

# Target fields on Patient Encounter to set
ENCOUNTER_MEDICATION_TEMPLATE_FIELD = "sr_medication_template"

# Standard drug-prescription child table on Patient Encounter
DRUG_PRESCRIPTION_TABLE = "sr_homeopathy_drug_prescription"

# Diet chart field on Patient Encounter
ENCOUNTER_DIET_CHART_FIELD = "diet_chart"

# Encounter type & sales type fields on Patient Encounter
ENCOUNTER_TYPE_FIELD = "sr_encounter_type"
SALES_TYPE_FIELD = "sr_sales_type"

# Practitioner fields — try all; set whichever exist on the doc
ENCOUNTER_PRACTITIONER_FIELDS = [
    "sr_homeopathy_practitioner",   # custom pathy-specific field (preferred)
    "practitioner",                  # standard Healthcare field (fallback)
]
# ─────────────────────────────────────────────────────────────────────────────


def _get_item_code(row) -> str:
    """Return the item code from a child row regardless of fieldname variant."""
    for key in ITEM_CODE_FIELDS:
        val = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
        if val:
            return val
    return ""


def _conditions_match(doc, mapping: dict) -> bool:
    """
    Validate Patient Encounter against the conditions set on the mapping record.

    Rules:
      - If a condition field on the mapping is blank, that check is skipped.
      - All non-blank conditions must match for the function to return True.
    """
    # Check encounter_type condition
    required_encounter_type = (mapping.get("encounter_type") or "").strip()
    if required_encounter_type:
        actual = (getattr(doc, ENCOUNTER_TYPE_FIELD, None) or "").strip()
        if actual.lower() != required_encounter_type.lower():
            return False

    # Check sales_type condition
    required_sales_type = (mapping.get("sales_type") or "").strip()
    if required_sales_type:
        actual = (getattr(doc, SALES_TYPE_FIELD, None) or "").strip()
        if actual.lower() != required_sales_type.lower():
            return False

    return True


def _populate_drug_prescription(doc, template_name: str):
    """
    Copy rows from SR Medication Template → drug_prescription child table.

    Maps SR Medication Template Item fields to Drug Prescription fields:
      sr_medication  → medication
      sr_drug_code   → drug_code
      sr_dosage      → dosage
      sr_period      → period
      sr_dosage_form → dosage_form
    """
    try:
        template = frappe.get_doc("SR Medication Template", template_name)
    except frappe.DoesNotExistError:
        frappe.log_error(
            f"auto_pex: SR Medication Template '{template_name}' not found",
            "auto_pex: _populate_drug_prescription"
        )
        return

    enc_meta = frappe.get_meta("Patient Encounter")
    if not enc_meta.has_field(DRUG_PRESCRIPTION_TABLE):
        return  # field doesn't exist on this install — skip silently

    # Clear existing rows so we don't duplicate on repeated saves
    doc.set(DRUG_PRESCRIPTION_TABLE, [])

    for tmpl_row in (template.sr_medications or []):
        row = doc.append(DRUG_PRESCRIPTION_TABLE, {})
        if tmpl_row.get("sr_medication"):
            row.medication = tmpl_row.sr_medication
        if tmpl_row.get("sr_drug_code"):
            row.drug_code = tmpl_row.sr_drug_code
        if tmpl_row.get("sr_dosage"):
            row.dosage = tmpl_row.sr_dosage
        if tmpl_row.get("sr_period"):
            row.period = tmpl_row.sr_period
        if tmpl_row.get("sr_dosage_form"):
            row.dosage_form = tmpl_row.sr_dosage_form


def apply_auto_pex_mapping(doc, method=None):
    """
    Called on Patient Encounter `before_save`.

    Checks draft invoice items. If exactly one item is present and a mapping
    exists for it, validates configured conditions then populates the medication
    template, drug prescription rows, practitioner, and diet chart fields.
    """
    try:
        rows = doc.get(DRAFT_INVOICE_TABLE) or []

        # Only proceed when exactly one item is in the draft invoice
        if len(rows) != 1:
            return

        item_code = _get_item_code(rows[0])
        if not item_code:
            return

        # Look up the mapping record for this item
        mapping = frappe.db.get_value(
            "Auto Pex Item Mapping",
            {"item": item_code},
            [
                "medication_template",
                "healthcare_practitioner",
                "diet_chart",
                "encounter_type",
                "sales_type",
            ],
            as_dict=True,
        )
        if not mapping:
            return  # no mapping defined — do nothing

        # ── Validation: check conditions configured on the mapping ────────────
        if not _conditions_match(doc, mapping):
            return  # encounter doesn't satisfy this mapping's conditions

        enc_meta = frappe.get_meta("Patient Encounter")

        # 1. Set medication template
        tmpl = mapping.get("medication_template")
        if tmpl and enc_meta.has_field(ENCOUNTER_MEDICATION_TEMPLATE_FIELD):
            doc.set(ENCOUNTER_MEDICATION_TEMPLATE_FIELD, tmpl)

            # 2. Populate drug_prescription table from the template
            _populate_drug_prescription(doc, tmpl)

        # 3. Set practitioner — try each candidate field, set all that exist
        practitioner = mapping.get("healthcare_practitioner")
        if practitioner:
            for field in ENCOUNTER_PRACTITIONER_FIELDS:
                if enc_meta.has_field(field):
                    doc.set(field, practitioner)

        # 4. Set diet chart if mapped and field exists on PE
        diet_chart = mapping.get("diet_chart")
        if diet_chart and enc_meta.has_field(ENCOUNTER_DIET_CHART_FIELD):
            doc.set(ENCOUNTER_DIET_CHART_FIELD, diet_chart)

    except Exception:
        # Non-fatal — log but never block the save
        frappe.log_error(frappe.get_traceback(), "auto_pex: apply_auto_pex_mapping error")
