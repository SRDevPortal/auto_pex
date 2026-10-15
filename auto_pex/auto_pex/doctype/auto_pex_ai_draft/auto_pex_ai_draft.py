import json

import frappe
from frappe.model.document import Document

from auto_pex.ai import history
from auto_pex.ai.schema import validate_proposal
from auto_pex.ai.workflow import EDITABLE, check_reviewer, proposal_from_draft


class AutoPexAIDraft(Document):
	def validate(self):
		if frappe.flags.auto_pex_service_write:
			return
		check_reviewer()
		if self.is_new():
			frappe.throw("Create AI drafts using Generate AI Draft on an encounter.", frappe.PermissionError)
		before = self.get_doc_before_save()
		if not before or before.status not in ("Ready", "Needs Review"):
			frappe.throw("Only a draft awaiting review can be edited.")
		for field in self.meta.fields:
			if field.fieldname not in EDITABLE and field.fieldtype not in (
				"HTML",
				"Section Break",
				"Column Break",
			):
				if self.get(field.fieldname) != before.get(field.fieldname):
					frappe.throw(
						"Draft source, lifecycle, and audit fields cannot be edited.", frappe.PermissionError
					)
		self.check_permission("write")
		proposal = proposal_from_draft(self)
		context = json.loads(self.context_json)
		settings = frappe.get_single("Auto Pex AI Settings")
		validate_proposal(proposal, history.source_ids(context), settings.max_medications)
		history.validate_catalog(proposal, json.loads(self.catalog_json))

	def on_trash(self):
		frappe.throw(
			"AI draft audit records cannot be deleted through the application.", frappe.PermissionError
		)
