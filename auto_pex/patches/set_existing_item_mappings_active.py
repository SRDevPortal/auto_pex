import frappe


def execute():
    if not frappe.db.has_column("Auto Pex Item Mapping", "is_active"):
        return

    frappe.db.sql(
        """
        update `tabAuto Pex Item Mapping`
        set is_active = 1
        """
    )
