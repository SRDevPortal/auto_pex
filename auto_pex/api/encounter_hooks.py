# auto_pex/api/encounter_hooks.py
"""
Auto Pex — Patient Encounter before_save hook.

Logic:
  - Look up the Auto Pex Item Mapping for the single draft-invoice item.
  - Only active Auto Pex Item Mapping records are applied.
  - Check Validation Conditions configured on the mapping record:
      • encounter_type — if set, Patient Encounter's sr_encounter_type must match
      • sales_type     — if set, Patient Encounter's sr_sales_type must match
    Blank conditions are skipped (not enforced).
  - If conditions pass, automatically:
      1. Set `sr_medication_template` on the encounter.
      2. Copy SR Medication Template rows into the matching drug table by medication class:
         • Ayurvedic Medicine   → `drug_prescription`
         • Homeopathic Medicine → `sr_homeopathy_drug_prescription`
         • Allopathic Medicine  → `sr_allopathy_drug_prescription`
      3. Copy template header instruction → `sr_pe_instruction`.
      4. Set Ayurvedic/Homeopathic/Allopathic practitioners on the encounter.
      5. Set `diet_chart` on the encounter (if mapped).
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
ENCOUNTER_INSTRUCTION_FIELD = "sr_pe_instruction"
ENCOUNTER_STATUS_FIELD = "sr_encounter_status"

# Encounter status to mark when Auto Pex loads medication
PRX_READY_STATUS = "PRX Ready"

# Drug-prescription child tables on Patient Encounter, routed by medication class
DRUG_PRESCRIPTION_TABLES = {
    "ayurvedic medicine": "drug_prescription",
    "homeopathic medicine": "sr_homeopathy_drug_prescription",
    "allopathic medicine": "sr_allopathy_drug_prescription",
}

# Diet chart field on Patient Encounter
ENCOUNTER_DIET_CHART_FIELD = "diet_chart"

# Encounter type & sales type fields on Patient Encounter
ENCOUNTER_TYPE_FIELD = "sr_encounter_type"
SALES_TYPE_FIELD = "sr_sales_type"

PRACTITIONER_FIELD_MAP = {
    "ayurvedic_practitioner": {
        "practitioner": "sr_ayurvedic_practitioner",
        "name": "sr_ayurvedic_practitioner_name",
        "reg": "sr_ayurvedic_practitioner_reg",
    },
    "homeopathic_practitioner": {
        "practitioner": "sr_homeopathy_practitioner",
        "name": "sr_homeopathy_practitioner_name",
        "reg": "sr_homeopathy_practitioner_reg",
    },
    "allopathic_practitioner": {
        "practitioner": "sr_allopathy_practitioner",
        "name": "sr_allopathy_practitioner_name",
        "reg": "sr_allopathy_practitioner_reg",
    },
}

# Backward-compatible mapping for older Auto Pex records.
LEGACY_PRACTITIONER_FIELD = "healthcare_practitioner"
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


def _resolve_drug_prescription_table(medication_class: str) -> str:
    """Return the Patient Encounter child table for a medication class."""
    return DRUG_PRESCRIPTION_TABLES.get((medication_class or "").strip().lower(), "")


def _get_medication_class(tmpl_row) -> str:
    """Return medication class from the template row, falling back to Medication."""
    medication_class = tmpl_row.get("sr_medication_class")
    if medication_class:
        return medication_class

    medication = tmpl_row.get("sr_medication")
    if medication:
        return frappe.db.get_value("Medication", medication, "medication_class") or ""

    return ""


def _first_existing_field(meta, fieldnames):
    for fieldname in fieldnames:
        if fieldname == "name" or meta.has_field(fieldname):
            return fieldname
    return ""


def _get_practitioner_details(practitioner: str) -> dict:
    if not practitioner:
        return {}

    hp_meta = frappe.get_meta("Healthcare Practitioner")
    name_field = _first_existing_field(hp_meta, ("practitioner_name", "full_name", "name"))
    reg_field = _first_existing_field(hp_meta, ("sr_reg_no", "registration_no", "ayush_reg_no"))

    fields = [field for field in (name_field, reg_field) if field]
    if not fields:
        return {}

    values = frappe.db.get_value("Healthcare Practitioner", practitioner, fields, as_dict=True) or {}
    return {
        "name": values.get(name_field) or practitioner,
        "reg": values.get(reg_field) if reg_field else "",
    }


def _set_practitioner_fields(doc, mapping: dict, enc_meta):
    """Set segment-specific practitioners from Auto Pex mapping."""
    practitioner_values = {
        mapping_field: mapping.get(mapping_field)
        for mapping_field in PRACTITIONER_FIELD_MAP
    }

    legacy_practitioner = mapping.get(LEGACY_PRACTITIONER_FIELD)
    if legacy_practitioner and not practitioner_values.get("homeopathic_practitioner"):
        practitioner_values["homeopathic_practitioner"] = legacy_practitioner

    for mapping_field, encounter_fields in PRACTITIONER_FIELD_MAP.items():
        practitioner = practitioner_values.get(mapping_field)
        if not practitioner:
            continue

        if enc_meta.has_field(encounter_fields["practitioner"]):
            doc.set(encounter_fields["practitioner"], practitioner)

        details = _get_practitioner_details(practitioner)
        if details.get("name") and enc_meta.has_field(encounter_fields["name"]):
            doc.set(encounter_fields["name"], details["name"])
        if enc_meta.has_field(encounter_fields["reg"]):
            doc.set(encounter_fields["reg"], details.get("reg") or "")

    primary_practitioner = (
        practitioner_values.get("homeopathic_practitioner")
        or practitioner_values.get("ayurvedic_practitioner")
        or practitioner_values.get("allopathic_practitioner")
    )
    if primary_practitioner and enc_meta.has_field("practitioner"):
        doc.set("practitioner", primary_practitioner)
        details = _get_practitioner_details(primary_practitioner)
        if details.get("name") and enc_meta.has_field("practitioner_name"):
            doc.set("practitioner_name", details["name"])


def _ensure_prx_ready_status():
    """Create the PRX Ready encounter status if it is not present."""
    if frappe.db.exists("SR Encounter Status", PRX_READY_STATUS):
        return

    status = frappe.new_doc("SR Encounter Status")
    status.sr_status_name = PRX_READY_STATUS
    status.is_active = 1
    status.insert(ignore_permissions=True)


def _set_prx_ready_status(doc, enc_meta):
    """Mark the encounter as PRX Ready if the status field exists."""
    if not enc_meta.has_field(ENCOUNTER_STATUS_FIELD):
        return

    _ensure_prx_ready_status()
    doc.set(ENCOUNTER_STATUS_FIELD, PRX_READY_STATUS)


def _populate_drug_prescription(doc, template_name: str):
    """
    Copy rows from SR Medication Template → medication-class child tables.

    Maps SR Medication Template Item fields to Drug Prescription fields:
      sr_medication  → medication
      sr_drug_code   → drug_code
      sr_dosage      → dosage
      sr_period      → period
      sr_dosage_form → dosage_form
      sr_instruction → sr_drug_instruction
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
    existing_tables = [
        table for table in set(DRUG_PRESCRIPTION_TABLES.values())
        if enc_meta.has_field(table)
    ]
    if not existing_tables:
        return  # fields don't exist on this install — skip silently

    # Clear existing rows so we don't duplicate on repeated saves or leave stale class rows
    for table in existing_tables:
        doc.set(table, [])

    for tmpl_row in (template.sr_medications or []):
        target_table = _resolve_drug_prescription_table(_get_medication_class(tmpl_row))
        if not target_table or target_table not in existing_tables:
            continue

        row = doc.append(target_table, {})
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
        if tmpl_row.get("sr_instruction"):
            row.sr_drug_instruction = tmpl_row.sr_instruction

    if template.get("sr_tmpl_instruction") and enc_meta.has_field(ENCOUNTER_INSTRUCTION_FIELD):
        doc.set(ENCOUNTER_INSTRUCTION_FIELD, template.sr_tmpl_instruction)


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

        # Look up the active mapping record for this item
        mapping = frappe.db.get_value(
            "Auto Pex Item Mapping",
            {"item": item_code, "is_active": 1},
            [
                "medication_template",
                "ayurvedic_practitioner",
                "homeopathic_practitioner",
                "allopathic_practitioner",
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
            _set_prx_ready_status(doc, enc_meta)

        # 3. Set practitioner for each medication segment
        _set_practitioner_fields(doc, mapping, enc_meta)

        # 4. Set diet chart if mapped and field exists on PE
        diet_chart = mapping.get("diet_chart")
        if diet_chart and enc_meta.has_field(ENCOUNTER_DIET_CHART_FIELD):
            doc.set(ENCOUNTER_DIET_CHART_FIELD, diet_chart)

    except Exception:
        # Non-fatal — log but never block the save
        frappe.log_error(frappe.get_traceback(), "auto_pex: apply_auto_pex_mapping error")
