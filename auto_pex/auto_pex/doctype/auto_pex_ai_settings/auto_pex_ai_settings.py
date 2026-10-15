import json

import frappe
from frappe.model.document import Document

from auto_pex.ai.provider import validate_url
from auto_pex.ai.schema import DraftError


class AutoPexAISettings(Document):
	def validate(self):
		try:
			rules = json.loads(self.medication_rules or "{}")
			if not isinstance(rules, dict):
				raise ValueError
			for name, rule in rules.items():
				if (
					not name
					or not isinstance(rule, dict)
					or set(rule) - {"dosages", "periods", "drug_codes", "blocked"}
				):
					raise ValueError
				for key, value in rule.items():
					if key == "blocked":
						if not isinstance(value, bool):
							raise ValueError
					elif not isinstance(value, list) or not all(isinstance(item, str) for item in value):
						raise ValueError
		except (ValueError, TypeError):
			frappe.throw("Medication rules must match the documented JSON format.")
		for field, minimum, maximum in (
			("history_days", 1, 3650),
			("history_limit", 1, 100),
			("request_timeout", 10, 120),
			("retry_limit", 0, 2),
			("max_medications", 1, 50),
		):
			value = self.get(field)
			if value is None or not minimum <= int(value) <= maximum:
				frappe.throw(f"{field} must be between {minimum} and {maximum}.")
		if not self.enabled:
			return
		for field in (
			"eligible_status",
			"review_status",
			"approved_status",
			"model",
			"prescribing_constraints",
		):
			if not (self.get(field) or "").strip():
				frappe.throw(f"Configure {field} before enabling AI drafts.")
		if len({self.eligible_status, self.review_status, self.approved_status}) != 3:
			frappe.throw("Eligible, review, and approved statuses must be different.")
		if not self.get_password("openai_api_key", raise_exception=False):
			frappe.throw("Configure the OpenAI API key before enabling AI drafts.")
		if self.history_source == "Remote read-only API":
			try:
				validate_url(self.history_url)
			except DraftError as exc:
				frappe.throw(str(exc))
			if not self.get_password("history_authorization", raise_exception=False):
				frappe.throw("Configure the read-only history Authorization header.")
		for status in (self.eligible_status, self.review_status, self.approved_status):
			if frappe.db.get_value("SR Encounter Status", status, "is_active") == 0:
				frappe.throw("AI workflow statuses must be active.")
		if self.auto_generate:
			if not self.automation_user:
				frappe.throw("Select an automation user before enabling generation on save.")
			from auto_pex.ai.workflow import check_reviewer

			check_reviewer(self.automation_user)
