import frappe


def execute():
	"""Initialize unset flags without reactivating explicitly disabled mappings.

	The DocField default handles a newly added column. Existing 0/1 values
	belong to the user and must survive upgrades and explicit patch reruns.
	"""
	if not frappe.db.has_column("Auto Pex Item Mapping", "is_active"):
		return

	frappe.db.sql(
		"""
        update `tabAuto Pex Item Mapping`
        set is_active = 1
        where is_active is null
        """
	)
