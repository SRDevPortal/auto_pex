"""Synthetic report tests; no patient files or network calls."""

import copy
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from PIL import Image
from pypdf import PdfWriter

from auto_pex.ai import history, provider, reports, workflow
from auto_pex.ai import test_followup as followup
from auto_pex.ai.schema import DraftError
from auto_pex.ai.test_followup import Base, Doc, catalog, context, encounter, proposal, settings


def pdf(pages=2, encrypted=False):
	writer = PdfWriter()
	for _ in range(pages):
		writer.add_blank_page(width=100, height=100)
	if encrypted:
		writer.encrypt("test-password")
	stream = io.BytesIO()
	writer.write(stream)
	return stream.getvalue()


def extraction(count=2):
	return {
		"pages": [
			{"page": i, "readable": True, "text": "Synthetic result 7.2 unit", "uncertainties": []}
			for i in range(1, count + 1)
		]
	}


def file_doc(name="F1", parent="CURRENT"):
	return Doc(
		name=name,
		file_name="report.pdf",
		file_url="/private/files/report.pdf",
		modified="v1",
		attached_to_doctype="Patient Encounter",
		attached_to_name=parent,
		check_permission=MagicMock(),
	)


def source(content=None):
	return {
		"id": "File:F1",
		"file_id": "F1",
		"filename": "report.pdf",
		"mime": "application/pdf",
		"pages": 2,
		"sha256": hashlib.sha256(content or pdf()).hexdigest(),
		"references": [{"encounter": "CURRENT", "field": "attachments", "row": ""}],
	}


class ReportTests(Base):
	def test_supported_formats_and_rejection(self):
		self.assertEqual(reports.inspect_file(pdf()), ("application/pdf", 2))
		for fmt, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")):
			stream = io.BytesIO()
			Image.new("RGB", (40, 40), "white").save(stream, format=fmt)
			self.assertEqual(reports.inspect_file(stream.getvalue()), (mime, 1))
		for content in (b"not a report", b"%PDF-broken", pdf(21), pdf(encrypted=True)):
			with self.subTest(content=content[:12]), self.assertRaises(DraftError):
				reports.inspect_file(content)

	def test_read_file_bounds_paths_urls_and_permissions(self):
		with tempfile.TemporaryDirectory() as folder:
			root = Path(folder)
			(root / "private/files").mkdir(parents=True)
			path = root / "private/files/report.pdf"
			path.write_bytes(pdf())
			doc = file_doc()
			doc.get_full_path = lambda: str(path)
			with patch.object(frappe, "get_site_path", side_effect=lambda *parts: str(root.joinpath(*parts))):
				self.assertEqual(reports.read_file(doc), path.read_bytes())
				doc.check_permission.assert_called_with("read")
				with patch.object(reports, "MAX_FILE_BYTES", 1), self.assertRaises(DraftError):
					reports.read_file(doc)
				doc.get_full_path = lambda: str(root / "outside.pdf")
				with self.assertRaises(DraftError):
					reports.read_file(doc)
				doc.file_url = "https://example.org/report.pdf"
				with self.assertRaises(DraftError):
					reports.read_file(doc)
				doc.check_permission.side_effect = frappe.PermissionError
				with self.assertRaises(frappe.PermissionError):
					reports.read_file(doc)

	def test_manifest_current_history_dedup_and_changed_bytes(self):
		doc = encounter()
		doc.meta.fields = [Doc(fieldtype="Attach", fieldname="report")]
		doc.report = "/private/files/report.pdf"
		doc.apply_fieldlevel_read_permissions = MagicMock()
		previous = copy.copy(doc)
		previous.name, previous.report = "PREV-1", None
		files = {"F1": file_doc(), "F2": file_doc("F2", "PREV-1")}

		def get_doc(kind, name):
			return files[name] if kind == "File" else (doc if name == "CURRENT" else previous)

		def get_all(kind, filters, **kwargs):
			if "file_url" in filters:
				return [files["F1"]]
			return [files["F1" if filters["attached_to_name"] == "CURRENT" else "F2"]]

		with (
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(reports, "read_file", return_value=pdf()) as read,
		):
			before = reports.manifest(doc, context())
			self.assertEqual(len(before), 2)
			self.assertEqual(len(before[0]["references"]), 2)
			self.assertEqual(read.call_count, 2)
			read.return_value = pdf(1)
			after = reports.manifest(doc, context())
			self.assertNotEqual(before[0]["sha256"], after[0]["sha256"])
			self.assertEqual(len(reports.manifest(doc, context(), local_history=False)), 1)
			files["F1"].attached_to_name = "OTHER-PATIENT-ENCOUNTER"
			with self.assertRaises(DraftError):
				reports.manifest(doc, context())

	def test_child_table_report_reference(self):
		doc = encounter()
		doc.meta.fields = [Doc(fieldtype="Table", fieldname="reports")]
		doc.reports = [
			Doc(
				name="ROW1",
				doctype="SR Medical Report",
				report="/private/files/report.pdf",
				meta=Doc(fields=[Doc(fieldtype="Attach", fieldname="report")]),
				report_date="2026-01-01",
			)
		]
		refs = reports._attachment_refs(doc)
		self.assertEqual(refs[0]["report_date"], "2026-01-01")
		file = file_doc()
		file.attached_to_doctype, file.attached_to_name = "SR Medical Report", "ROW1"
		reports.check_source(file, [{"encounter": "CURRENT", **refs[0]}])
		file.attached_to_name = "OTHER-ROW"
		with self.assertRaises(DraftError):
			reports.check_source(file, [{"encounter": "CURRENT", **refs[0]}])

	def test_manifest_over_limit_never_silently_skips(self):
		doc = encounter()
		doc.apply_fieldlevel_read_permissions = MagicMock()
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(frappe, "get_all", return_value=[Doc(name=str(i)) for i in range(21)]),
			self.assertRaises(DraftError),
		):
			reports.manifest(doc, context(), local_history=False)

	def test_extraction_requires_complete_readable_pages(self):
		reports.validate_extraction(extraction(), 2)
		for result in ({}, extraction(1), extraction(3), {"pages": list(reversed(extraction()["pages"]))}):
			with self.assertRaises(DraftError):
				reports.validate_extraction(result, 2)
		for key, value in (("readable", False), ("text", " ")):
			result = extraction()
			result["pages"][0][key] = value
			with self.assertRaises(DraftError):
				reports.validate_extraction(result, 2)

	def test_provider_pdf_and_image_payloads_and_incomplete_response(self):
		response = {
			"status": "completed",
			"id": "R1",
			"output": [
				{"type": "message", "content": [{"type": "output_text", "text": json.dumps(extraction())}]}
			],
		}
		for mime, kind in (("application/pdf", "input_file"), ("image/png", "input_image")):
			with patch.object(provider, "request_json", return_value=response) as request:
				provider.extract_report({**source(), "mime": mime}, b"synthetic", settings())
			payload = request.call_args.kwargs["json"]
			self.assertIs(payload["store"], False)
			self.assertNotIn("tools", payload)
			self.assertEqual(payload["input"][0]["content"][1]["type"], kind)
			self.assertIn("data:" + mime + ";base64,", json.dumps(payload))
		with (
			patch.object(provider, "request_json", return_value={"status": "incomplete"}),
			self.assertRaises(DraftError),
		):
			provider.extract_report(source(), pdf(), settings())

	def test_missing_extraction_blocks_prescription_request(self):
		clinical = {**context(), "attached_reports": [source()]}
		with patch.object(provider, "request_json") as request, self.assertRaises(DraftError):
			provider.generate(clinical, catalog(), settings())
		request.assert_not_called()

	def test_partial_extraction_preserved_but_not_usable(self):
		content = pdf()
		clinical = {**context(), "attached_reports": [source(content)]}
		result = extraction()
		result["pages"][1]["readable"] = False
		persist = MagicMock()
		with (
			patch.object(frappe, "get_doc", return_value=file_doc()),
			patch.object(reports, "read_file", return_value=content),
			patch.object(provider, "extract_report", return_value=(result, "R1", {})),
			self.assertRaises(DraftError),
		):
			reports.extract(clinical, settings(), persist)
		persist.assert_called_once()
		self.assertEqual(clinical["report_extractions"][0]["response_id"], "R1")
		with self.assertRaises(DraftError):
			reports.require_complete(clinical)

	def test_changed_file_stops_extraction_and_freshness_tracks_manifest(self):
		clinical = {**context(), "attached_reports": [source()]}
		before = workflow.context_hash(clinical, {})
		clinical["report_extractions"] = [{"result": extraction()}]
		self.assertEqual(before, workflow.context_hash(clinical, {}))
		with (
			patch.object(frappe, "get_doc", return_value=file_doc()),
			patch.object(reports, "read_file", return_value=pdf(1)),
			patch.object(provider, "extract_report") as extract,
			self.assertRaises(DraftError),
		):
			reports.extract(clinical, settings(), MagicMock())
		extract.assert_not_called()
		self.assertIn("File:F1", history.source_ids(clinical))
		clinical["attached_reports"][0]["sha256"] = "changed"
		self.assertNotEqual(before, workflow.context_hash(clinical, {}))
		clinical["attached_reports"] = []
		self.assertNotEqual(before, workflow.context_hash(clinical, {}))

	def test_report_access_revocation_hides_snapshot(self):
		draft = Doc(
			encounter="CURRENT",
			patient="PATIENT-1",
			context_json=json.dumps({"attached_reports": [source()]}),
		)
		with (
			patch.object(frappe, "get_doc", return_value=encounter()),
			patch.object(frappe, "has_permission", side_effect=lambda kind, *args, **kwargs: kind != "File"),
		):
			self.assertFalse(workflow.draft_permission(draft, ptype="read"))

	def test_generation_reads_before_prescribing_and_unreadable_report_blocks(self):
		for readable in (True, False):
			doc, config, clinical, draft = followup.WorkflowTests.make_review(self)
			content = pdf()
			clinical["attached_reports"] = [source(content)]
			draft.status, draft.requested_by = "Queued", "reviewer"
			result = extraction()
			result["pages"][0]["readable"] = readable
			order = []

			def extract(*args):
				order.append("read")
				return result, "REPORT-RESPONSE", {}

			def generate(clinical, *args):
				reports.require_complete(clinical)
				order.append("draft")
				return proposal(), "RX-RESPONSE", {}

			with (
				patch.object(frappe, "set_user"),
				patch.object(workflow, "check_reviewer"),
				patch.object(workflow, "get_settings", return_value=config),
				patch.object(frappe, "cache", MagicMock()),
				patch.object(history, "fetch_history", return_value=clinical),
				patch.object(history, "medication_catalog", return_value=catalog()),
				patch.object(reports, "read_file", return_value=content),
				patch.object(provider, "extract_report", side_effect=extract),
				patch.object(provider, "generate", side_effect=generate),
				patch.object(
					frappe,
					"get_doc",
					side_effect=lambda kind, *args, **kwargs: doc
					if kind == "Patient Encounter"
					else file_doc()
					if kind == "File"
					else draft,
				),
			):
				workflow.generate_draft("DRAFT")
			self.assertEqual(order, ["read", "draft"] if readable else ["read"])
			self.assertEqual(
				draft.status, "Needs Review" if readable else "Failed", draft.get("failure_reason")
			)
			self.assertEqual(
				json.loads(draft.context_json)["report_extractions"][0]["response_id"], "REPORT-RESPONSE"
			)
			self.assertIsNone(doc.get("drug_prescription"))

	def test_changed_report_prevents_approval(self):
		doc, config, clinical, draft = followup.WorkflowTests.make_review(self)
		config.history_source = "Local read-only API"
		draft.settings_hash = workflow.config_hash(config)
		clinical["attached_reports"] = [source()]
		draft.context_hash = workflow.context_hash(clinical, catalog())
		fresh = copy.deepcopy(clinical)
		fresh["attached_reports"][0]["sha256"] = "new-report-bytes"
		self.db.get_value.return_value = "PRACTITIONER-1"
		with (
			patch.object(workflow, "_review_document", return_value=draft),
			patch.object(workflow, "get_settings", return_value=config),
			patch.object(history, "fetch_history", return_value=fresh),
			patch.object(history, "medication_catalog", return_value=catalog()),
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, *args, **kwargs: doc if kind == "Patient Encounter" else draft,
			),
		):
			result = workflow.approve_draft("DRAFT", "v1")
		self.assertEqual(result["status"], "Stale")
		doc.save.assert_not_called()
		self.assertIsNone(doc.get("drug_prescription"))

	def test_prescription_request_contains_extracted_evidence(self):
		clinical = {**context(), "attached_reports": [source()]}
		clinical["report_extractions"] = [
			{"id": "File:F1", "sha256": clinical["attached_reports"][0]["sha256"], "result": extraction()}
		]
		response = {
			"status": "completed",
			"output": [
				{"type": "message", "content": [{"type": "output_text", "text": json.dumps(proposal())}]}
			],
		}
		with patch.object(provider, "request_json", return_value=response) as request:
			provider.generate(clinical, catalog(), settings())
		payload = json.loads(request.call_args.kwargs["json"]["input"])
		self.assertEqual(payload["clinical_context"]["report_extractions"][0]["result"], extraction())
		self.assertNotIn("base64", json.dumps(payload))

	def test_reused_upload_resolves_to_its_own_encounter_file(self):
		doc = encounter()
		doc.meta.fields = [Doc(fieldtype="Attach", fieldname="report")]
		doc.report = "/private/files/report.pdf"
		doc.apply_fieldlevel_read_permissions = MagicMock()
		previous = copy.copy(doc)
		previous.name = "PREV-1"
		files = {"F1": file_doc(), "F2": file_doc("F2", "PREV-1")}

		def get_all(kind, filters, **kwargs):
			return (
				list(files.values())
				if "file_url" in filters
				else [files["F1" if filters["attached_to_name"] == "CURRENT" else "F2"]]
			)

		with (
			patch.object(
				frappe,
				"get_doc",
				side_effect=lambda kind, name: files[name]
				if kind == "File"
				else doc
				if name == "CURRENT"
				else previous,
			),
			patch.object(frappe, "get_all", side_effect=get_all),
			patch.object(reports, "read_file", return_value=pdf()),
		):
			manifest = reports.manifest(doc, context())
		self.assertEqual(len(manifest), 2)
		for source in manifest:
			self.assertEqual(
				{ref["encounter"] for ref in source["references"]},
				{files[source["file_id"]].attached_to_name},
			)

	def test_job_budget_covers_maximum_report_batch_and_retries(self):
		# Valid settings permit 120-second reads and two retries. Reserve connect time,
		# retry backoff, three history reads and the prescription request as well.
		per_request = (120 + 10) * 3 + 1 + 2
		self.assertGreater(workflow.GENERATION_TIMEOUT, (reports.MAX_FILES + 4) * per_request + 300)
