# auto_pex/api/encounter_hooks.py
"""
Auto Pex — Patient Encounter before_save hook.

Logic:
  - Inspect the draft-invoice items table (sr_pe_order_items) on the encounter.
  - If EXACTLY ONE item is present and an `Auto Pex Item Mapping` record exists
    for that item, automatically:
      1. Set `sr_medication_template` on the encounter (the JS will load the
         homeopathic drug prescription rows on the next form-reload).
      2. Set `sr_homeopathy_practitioner` (and/or `practitioner`) on the encounter.
  - If 0 or 2+ items exist, exit silently — no changes.
"""

import frappe


# ── field names ──────────────────────────────────────────────────────────────
# Draft-invoice child table on Patient Encounter
DRAFT_INVOICE_TABLE = "sr_pe_order_items"

# Possible fieldnames for the item code column on each child row
ITEM_CODE_FIELDS = ["sr_item_code", "item_code"]

# Target fields on Patient Encounter to set
ENCOUNTER_MEDICATION_TEMPLATE_FIELD = "sr_medication_template"

# Practitioner fields — we try all of them; set whichever exists on the doc
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


def apply_auto_pex_mapping(doc, method=None):
    """
    Called on Patient Encounter `before_save`.

    Checks draft invoice items. If exactly one item is present and a mapping
    exists for it, populates the medication template and practitioner fields.
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
            ["medication_template", "healthcare_practitioner"],
            as_dict=True,
        )
        if not mapping:
            return  # no mapping defined — do nothing

        enc_meta = frappe.get_meta("Patient Encounter")

        # 1. Set medication template
        tmpl = mapping.get("medication_template")
        if tmpl and enc_meta.has_field(ENCOUNTER_MEDICATION_TEMPLATE_FIELD):
            doc.set(ENCOUNTER_MEDICATION_TEMPLATE_FIELD, tmpl)

        # 2. Set practitioner — try each candidate field, set all that exist
        practitioner = mapping.get("healthcare_practitioner")
        if practitioner:
            for field in ENCOUNTER_PRACTITIONER_FIELDS:
                if enc_meta.has_field(field):
                    doc.set(field, practitioner)

    except Exception:
        # Non-fatal — log but never block the save
        frappe.log_error(frappe.get_traceback(), "auto_pex: apply_auto_pex_mapping error")
