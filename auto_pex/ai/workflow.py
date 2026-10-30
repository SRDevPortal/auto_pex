"""Draft lifecycle. Only these service methods may change lifecycle and audit fields."""

import json
from contextlib import contextmanager
from html import unescape

import frappe
from frappe.utils import add_to_date, now_datetime, strip_html

from auto_pex.ai import history, provider
from auto_pex.ai.schema import (
	NOTE_FIELDS,
	PROMPT_VERSION,
	TABLES,
	DraftError,
	encounter_snapshot,
	fingerprint,
	matches_route,
	validate_proposal,
)

DRAFT = "Auto Pex AI Draft"
SETTINGS = "Auto Pex AI Settings"
ROLE = "Auto Pex AI Reviewer"
EDITABLE = {"instruction", "items", "review_notes", "concerns_resolved"}


@contextmanager
def service_write():
	previous = frappe.flags.auto_pex_service_write
	frappe.flags.auto_pex_service_write = True
	try:
		yield
	finally:
		frappe.flags.auto_pex_service_write = previous


def get_settings():
	if not frappe.db.exists("DocType", SETTINGS):
		return None
	return frappe.get_single(SETTINGS)


def config_hash(settings):
	# A prompt-policy change invalidates drafts and queued jobs created under the old policy.
	return fingerprint(
		{
			"prompt_version": PROMPT_VERSION,
			"settings": {
				key: settings.get(key)
				for key in (
					"enabled",
					"eligible_status",
					"review_status",
					"approved_status",
					"model",
					"history_source",
					"history_url",
					"history_days",
					"history_limit",
					"prescribing_constraints",
					"max_medications",
					"additional_medications",
					"medication_rules",
				)
			},
		}
	)


def context_hash(context, catalog):
	# Our own review-status save changes modified/status, but not clinical content.
	clinical = json.loads(json.dumps(context, default=str))
	clinical["current"].pop("modified", None)
	clinical["current"].pop("status", None)
	return fingerprint({"history": clinical, "catalog": catalog})


def check_reviewer(user=None):
	user = user or frappe.session.user
	if ROLE not in frappe.get_roles(user) or not frappe.db.get_value("User", user, "enabled"):
		frappe.throw("An enabled Auto Pex AI Reviewer is required.", frappe.PermissionError)


def assert_eligible(doc, settings, expected_status=None):
	if not settings or not settings.enabled:
		raise DraftError("AI follow-up generation is disabled.")
	if doc.get("submit_orders_on_save"):
		raise DraftError("Turn off Submit Orders on Save before using the AI draft workflow.")
	if doc.docstatus != 0 or not matches_route(doc):
		raise DraftError("A draft Online / Order / Repeat encounter is required.")
	if not any(unescape(strip_html(str(doc.get(field) or ""))).strip() for field in NOTE_FIELDS):
		raise DraftError("Current clinical notes are required before AI generation.")
	if doc.get("sr_encounter_status") != (expected_status or settings.eligible_status):
		raise DraftError("The encounter is no longer in the expected workflow status.")


def protects_prescription(doc):
	# Existing AI records remain protected even when the feature is disabled or route fields are edited.
	if (
		doc.get("name")
		and frappe.db.exists("DocType", DRAFT)
		and frappe.db.exists(DRAFT, {"encounter": doc.name})
	):
		return True
	settings = get_settings()
	return bool(settings and settings.enabled and matches_route(doc))


def on_encounter_update(doc, method=None):
	if frappe.flags.auto_pex_service_write:
		return
	settings = get_settings()
	if not settings or not settings.enabled or not matches_route(doc):
		return
	# Mark pending results stale immediately; remote changes are additionally checked before approval.
	pending = frappe.get_all(
		DRAFT,
		filters={"encounter": doc.name, "status": ["in", ["Queued", "Generating", "Ready", "Needs Review"]]},
		fields=["name", "input_hash", "expected_status"],
	)
	current_hash = fingerprint(encounter_snapshot(doc))
	for entry in pending:
		if entry.input_hash != current_hash or entry.expected_status != doc.get("sr_encounter_status"):
			with service_write():
				draft = frappe.get_doc(DRAFT, entry.name)
				draft.status = "Stale"
				draft.save(ignore_permissions=True)
	if not settings.auto_generate or not settings.automation_user:
		return
	try:
		expected = (
			settings.review_status if doc.get("sr_encounter_status") == settings.review_status else None
		)
		assert_eligible(doc, settings, expected)
	except DraftError:
		return
	# Run after commit; never do remote reads or change the initiating user's session inside save hooks.
	frappe.enqueue(
		"auto_pex.ai.workflow.auto_request",
		encounter=doc.name,
		user=settings.automation_user,
		enqueue_after_commit=True,
		queue="short",
	)


def auto_request(encounter, user):
	settings = get_settings()
	if not settings or not settings.auto_generate or settings.automation_user != user:
		return
	previous_user = frappe.session.user
	try:
		frappe.set_user(user)
		request_draft(encounter)
	except DraftError:
		# Encounter may have moved on between save and queue processing.
		pass
	finally:
		frappe.set_user(previous_user)


@frappe.whitelist(methods=["POST"])
def request_draft(encounter, regenerate=0):
	check_reviewer()
	settings = get_settings()
	if not settings or not settings.enabled:
		raise DraftError("AI follow-up generation is disabled.")
	doc = frappe.get_doc("Patient Encounter", encounter, for_update=True)
	doc.check_permission("write")
	previous = frappe.get_all(
		DRAFT,
		filters={"encounter": doc.name},
		fields=["name", "status", "input_hash"],
		order_by="creation desc",
		limit_page_length=1,
	)
	# Existing drafts may be replaced while the encounter is awaiting review.
	allowed_review = bool(previous) and doc.get("sr_encounter_status") == settings.review_status
	assert_eligible(doc, settings, settings.review_status if allowed_review else None)
	input_hash = fingerprint(encounter_snapshot(doc))
	if previous:
		last = previous[0]
		if last.status == "Approved":
			raise DraftError(
				"This encounter already has an approved AI prescription. Use a new follow-up encounter."
			)
		if last.status in ("Queued", "Generating") and last.input_hash == input_hash:
			return last.name
		if not int(regenerate) and last.input_hash == input_hash:
			return last.name
		if last.status in ("Ready", "Needs Review", "Queued", "Generating"):
			with service_write():
				old = frappe.get_doc(DRAFT, last.name)
				old.status = "Stale"
				old.save(ignore_permissions=True)
	with service_write():
		draft = frappe.get_doc(
			{
				"doctype": DRAFT,
				"encounter": doc.name,
				"patient": doc.patient,
				"status": "Queued",
				"requested_by": frappe.session.user,
				"input_hash": input_hash,
				"input_snapshot": json.dumps(encounter_snapshot(doc), default=str),
				"settings_hash": config_hash(settings),
				"expected_status": doc.get("sr_encounter_status"),
				"model": settings.model,
				"prompt_version": PROMPT_VERSION,
			}
		).insert(ignore_permissions=True)
	frappe.enqueue(
		"auto_pex.ai.workflow.generate_draft",
		draft_name=draft.name,
		queue="long",
		timeout=1800,
		enqueue_after_commit=True,
		job_id="auto-pex-" + draft.name,
		deduplicate=True,
	)
	return draft.name


def _persist(draft):
	with service_write():
		draft.save(ignore_permissions=True)


def _current(draft, settings, doc):
	assert_eligible(doc, settings, draft.expected_status)
	if draft.input_hash != fingerprint(encounter_snapshot(doc)) or draft.settings_hash != config_hash(
		settings
	):
		raise DraftError("Encounter data or AI settings changed. Regenerate the draft.")


def generate_draft(draft_name):
	"""Background-only entry point. Network calls run outside row locks and transactions."""
	initial = frappe.get_doc(DRAFT, draft_name)
	previous_user = frappe.session.user
	try:
		frappe.set_user(initial.requested_by)
		check_reviewer()
		with frappe.cache.lock("auto-pex-generation:" + draft_name, timeout=1900, blocking_timeout=1):
			draft = frappe.get_doc(DRAFT, draft_name)
			if draft.status != "Queued":
				return
			settings = get_settings()
			doc = frappe.get_doc("Patient Encounter", draft.encounter)
			doc.check_permission("write")
			_current(draft, settings, doc)
			draft.status = "Generating"
			_persist(draft)
			frappe.db.commit()
			context = history.fetch_history(doc, settings)
			catalog = history.medication_catalog(context, settings)
			digest = context_hash(context, catalog)
			# Persist source evidence before calling the model, so failures are reviewable.
			draft.context_json = json.dumps(context, default=str)
			draft.catalog_json = json.dumps(catalog, default=str)
			draft.context_hash = digest
			_persist(draft)
			frappe.db.commit()
			proposal, response_id, usage = provider.generate(context, catalog, settings)
			validate_proposal(proposal, history.source_ids(context), settings.max_medications)
			history.validate_catalog(proposal, catalog)
			# Use fresh DB reads after the external request (MariaDB repeatable-read snapshot).
			frappe.db.rollback()
			fresh_doc = frappe.get_doc("Patient Encounter", draft.encounter)
			latest = history.fetch_history(fresh_doc, settings)
			latest_catalog = history.medication_catalog(latest, settings)
			changed = digest != context_hash(latest, latest_catalog)
			frappe.db.rollback()
			doc = frappe.get_doc("Patient Encounter", draft.encounter, for_update=True)
			draft = frappe.get_doc(DRAFT, draft_name, for_update=True)
			if draft.status != "Generating":
				return
			if changed or fingerprint(encounter_snapshot(doc)) != draft.input_hash:
				draft.status = "Stale"
				_persist(draft)
				frappe.db.commit()
				return
			settings = get_settings()
			_current(draft, settings, doc)
			doc.check_permission("write")
			draft.original_response = json.dumps(proposal)
			draft.response_id, draft.usage_json = response_id, json.dumps(usage)
			draft.summary, draft.instruction = proposal["summary"], proposal["instruction"]
			warnings = proposal["warnings"] + proposal["missing_information"]
			if context["truncated"]:
				warnings.append("History was limited by the configured encounter count; review completeness.")
			if any(record["docstatus"] == 0 for record in context["previous"]):
				warnings.append(
					"History includes unsubmitted encounters. Confirm which prescriptions were authorized."
				)
			draft.warnings = "\n".join(warnings)
			draft.set("items", [])
			for row in proposal["items"]:
				draft.append("items", {**row, "source_ids": json.dumps(row["source_ids"])})
			draft.status = "Needs Review" if warnings or proposal["outcome"] != "draft" else "Ready"
			# No prescription is written here. The status transition uses normal encounter validation.
			if proposal["outcome"] == "draft":
				with service_write():
					doc.sr_encounter_status = settings.review_status
					doc.save()
				draft.expected_status = settings.review_status
			_persist(draft)
			frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		draft = frappe.get_doc(DRAFT, draft_name, for_update=True)
		if draft.status in ("Queued", "Generating"):
			draft.status = "Failed"
			draft.failure_reason = (
				str(exc)
				if isinstance(exc, DraftError)
				else (
					"Generation failed due to an application or permission error. Check configuration and access, then retry."
				)
			)
			_persist(draft)
			frappe.db.commit()
	finally:
		frappe.set_user(previous_user)


def proposal_from_draft(draft):
	original = json.loads(draft.original_response or "{}")
	try:
		items = [
			{
				key: row.get(key) or ""
				for key in (
					"medication_class",
					"medication",
					"drug_code",
					"dosage",
					"period",
					"dosage_form",
					"instruction",
					"rationale",
				)
			}
			| {"source_ids": json.loads(row.source_ids or "[]")}
			for row in draft.items
		]
	except (TypeError, ValueError):
		raise DraftError("Source IDs must be a JSON array of source identifiers.") from None
	return {
		"outcome": original.get("outcome"),
		"summary": draft.summary or "",
		"instruction": draft.instruction or "",
		"warnings": original.get("warnings", []),
		"missing_information": original.get("missing_information", []),
		"items": items,
	}


def _review_document(name, modified=None):
	check_reviewer()
	draft = frappe.get_doc(DRAFT, name)
	draft.check_permission("write")
	if modified and str(draft.modified) != str(modified):
		frappe.throw("The draft changed. Reload it before continuing.", frappe.TimestampMismatchError)
	return draft


@frappe.whitelist(methods=["POST"])
def approve_draft(name, modified):
	draft = _review_document(name, modified)
	if draft.status not in ("Ready", "Needs Review"):
		raise DraftError("Only a current reviewable draft can be approved.")
	practitioner = frappe.db.get_value("Healthcare Practitioner", {"user_id": frappe.session.user}, "name")
	if not practitioner:
		frappe.throw(
			"The approving user must be linked to a Healthcare Practitioner.", frappe.PermissionError
		)
	if draft.warnings and (not draft.concerns_resolved or not (draft.review_notes or "").strip()):
		raise DraftError(
			"Document how the listed concerns were resolved and mark them reviewed before approval."
		)
	settings = get_settings()
	doc = frappe.get_doc("Patient Encounter", draft.encounter)
	doc.check_permission("write")
	_current(draft, settings, doc)
	context = history.fetch_history(doc, settings)
	catalog = history.medication_catalog(context, settings)
	changed = draft.context_hash != context_hash(context, catalog)
	# No writes occurred yet. End the read snapshot before obtaining locks in encounter -> draft order.
	frappe.db.rollback()
	doc = frappe.get_doc("Patient Encounter", draft.encounter, for_update=True)
	draft = frappe.get_doc(DRAFT, name, for_update=True)
	if str(draft.modified) != str(modified) or draft.status not in ("Ready", "Needs Review"):
		frappe.throw("The draft changed. Reload it before continuing.", frappe.TimestampMismatchError)
	if settings.history_source == "Local read-only API":
		# Lock all local source parents and patient history before the final comparison and write.
		frappe.get_doc("Patient", doc.patient, for_update=True)
		for source in sorted(context["previous"], key=lambda source: source["id"]):
			frappe.get_doc("Patient Encounter", source["id"], for_update=True)
		context = history.fetch_history(doc, settings)
		catalog = history.medication_catalog(context, settings)
		changed = draft.context_hash != context_hash(context, catalog)
	if changed or fingerprint(encounter_snapshot(doc)) != draft.input_hash:
		draft.status = "Stale"
		_persist(draft)
		return {"status": "Stale", "message": "Clinical records changed. Regenerate the draft."}
	settings = get_settings()
	_current(draft, settings, doc)
	doc.check_permission("write")
	proposal = proposal_from_draft(draft)
	if proposal["outcome"] != "draft":
		raise DraftError(
			"The AI could not support a draft. Regenerate with complete notes or prescribe manually."
		)
	validate_proposal(proposal, history.source_ids(context), settings.max_medications)
	history.validate_catalog(proposal, catalog)
	for category, table in TABLES.items():
		rows = [row for row in proposal["items"] if row["medication_class"] == category]
		if rows and not doc.meta.has_field(table):
			raise DraftError("A required medication table is not installed on this encounter.")
		if doc.meta.has_field(table):
			doc.set(table, [])
		for row in rows:
			medicine = next(m for m in catalog["medications"] if m["medication"] == row["medication"])
			values = {key: row[key] for key in ("medication", "drug_code", "dosage", "period", "dosage_form")}
			values.update(strength=medicine["strength"], strength_uom=medicine["strength_uom"])
			child_meta = frappe.get_meta(doc.meta.get_field(table).options)
			instruction_field = (
				"sr_drug_instruction" if child_meta.has_field("sr_drug_instruction") else "comment"
			)
			values[instruction_field] = row["instruction"]
			doc.append(table, values)
	with service_write():
		if doc.meta.has_field("sr_medication_template"):
			doc.sr_medication_template = None
		doc.sr_pe_instruction = draft.instruction
		doc.sr_encounter_status = settings.approved_status
		doc.save()
		draft.status = "Approved"
		draft.reviewed_by = frappe.session.user
		draft.reviewed_practitioner = practitioner
		draft.reviewed_at = now_datetime()
		draft.save(ignore_permissions=True)
	return {"status": "Approved", "encounter": doc.name}


@frappe.whitelist(methods=["POST"])
def reject_draft(name, modified):
	draft = _review_document(name, modified)
	# Keep the same lock order as generation and approval.
	doc = frappe.get_doc("Patient Encounter", draft.encounter, for_update=True)
	doc.check_permission("write")
	draft = frappe.get_doc(DRAFT, name, for_update=True)
	if str(draft.modified) != str(modified) or draft.status not in ("Ready", "Needs Review"):
		raise DraftError("This draft has changed or cannot be rejected in its current state.")
	if not (draft.review_notes or "").strip():
		raise DraftError("Enter the reason for rejecting the draft in Clinician Review Notes.")
	draft.status, draft.reviewed_by, draft.reviewed_at = "Rejected", frappe.session.user, now_datetime()
	_persist(draft)
	return {"status": "Rejected"}


def expire_jobs():
	"""Recover interrupted workers and abandoned queued jobs without automatic repeat billing."""
	cutoff = add_to_date(now_datetime(), hours=-1)
	for name in frappe.get_all(
		DRAFT, filters={"status": ["in", ["Queued", "Generating"]], "modified": ["<", cutoff]}, pluck="name"
	):
		draft = frappe.get_doc(DRAFT, name, for_update=True)
		if draft.status in ("Queued", "Generating") and draft.modified < cutoff:
			draft.status = "Failed"
			draft.failure_reason = "The background job was interrupted or did not start. Regenerate to retry."
			_persist(draft)


def draft_permission(doc, user=None, ptype=None):
	user = user or frappe.session.user
	if not doc.encounter:
		return False
	encounter = frappe.get_doc("Patient Encounter", doc.encounter)
	required = "write" if ptype in ("write", "create", "delete") else "read"
	if not frappe.has_permission("Patient Encounter", required, doc=encounter, user=user):
		return False
	if not frappe.has_permission("Patient", "read", doc=doc.patient, user=user):
		return False
	if required == "write" and ROLE not in frappe.get_roles(user):
		return False
	# Access to stored local source snapshots cannot outlive access to their source encounters.
	context = json.loads(doc.context_json or "{}")
	for source in context.get("previous", []):
		if frappe.db.exists("Patient Encounter", source["id"]) and not frappe.has_permission(
			"Patient Encounter", "read", doc=source["id"], user=user
		):
			return False
	return None


def draft_query(user=None):
	# get_list applies Patient Encounter permissions, including user-permission and custom app conditions.
	user = user or frappe.session.user
	names = frappe.get_list("Patient Encounter", pluck="name", limit_page_length=0, user=user)
	if not names:
		return "1=0"
	candidates = frappe.get_all(
		DRAFT, filters={"encounter": ["in", names]}, fields=["name", "encounter", "patient", "context_json"]
	)
	visible = [row.name for row in candidates if draft_permission(row, user=user, ptype="read") is not False]
	if not visible:
		return "1=0"
	return "`tabAuto Pex AI Draft`.`name` in (" + ",".join(frappe.db.escape(n) for n in visible) + ")"
