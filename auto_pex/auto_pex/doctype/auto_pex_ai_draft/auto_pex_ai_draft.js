frappe.ui.form.on("Auto Pex AI Draft", {
	refresh(frm) {
		const editable =
			["Ready", "Needs Review"].includes(frm.doc.status) &&
			frappe.user.has_role("Auto Pex AI Reviewer");
		["items", "instruction", "review_notes", "concerns_resolved"].forEach((field) =>
			frm.set_df_property(field, "read_only", !editable)
		);
		frm.set_intro(
			__(
				"AI proposal only. Review the clinical history and every medication before approval."
			)
		);
		render_comparison(frm);
		const act = async (method) => {
			if (frm.is_dirty()) await frm.save();
			const result = await frappe.call({
				method: `auto_pex.ai.workflow.${method}`,
				args: { name: frm.doc.name, modified: frm.doc.modified },
				freeze: true,
				freeze_message: __("Checking the current clinical records"),
			});
			if (result.message?.message) frappe.msgprint(result.message.message);
			await frm.reload_doc();
		};
		if (editable) {
			frm.add_custom_button(__("Approve and Apply"), () => {
				frappe.confirm(
					__(
						"Apply this reviewed prescription and change the encounter to its configured approved status?"
					),
					() => act("approve_draft")
				);
			});
			frm.add_custom_button(__("Reject"), () => act("reject_draft"));
		}
		if (
			["Ready", "Needs Review", "Failed", "Stale", "Rejected"].includes(frm.doc.status) &&
			frappe.user.has_role("Auto Pex AI Reviewer")
		) {
			frm.add_custom_button(__("Regenerate"), async () => {
				if (frm.is_dirty()) await frm.save();
				const result = await frappe.call({
					method: "auto_pex.ai.workflow.request_draft",
					args: { encounter: frm.doc.encounter, regenerate: 1 },
					freeze: true,
				});
				if (result.message) frappe.set_route("Form", "Auto Pex AI Draft", result.message);
			});
		}
		frm.add_custom_button(__("Open Encounter"), () =>
			frappe.set_route("Form", "Patient Encounter", frm.doc.encounter)
		);
		frm.add_custom_button(__("Refresh Status"), () => frm.reload_doc());
	},
});

function render_comparison(frm) {
	const escape = (value) => frappe.utils.escape_html(String(value ?? ""));
	let context, catalog;
	try {
		context = JSON.parse(frm.doc.context_json || "{}");
		catalog = JSON.parse(frm.doc.catalog_json || "{}");
	} catch {
		return;
	}
	const rows = (items) =>
		(items || [])
			.map(
				(row) => `<tr>
        <td>${escape(row.medication_class)}</td><td>${escape(row.medication)}</td>
        <td>${escape(row.strength)} ${escape(row.strength_uom)}</td><td>${escape(
					row.dosage_form
				)}</td>
        <td>${escape(row.dosage)}</td><td>${escape(row.period)}</td>
		<td>${escape(row.dispensing_quantity ?? row.auto_pex_dispensing_quantity)} ${escape(
					row.dispensing_uom ?? row.auto_pex_dispensing_uom
				)}</td>
        <td>${escape(row.instruction || row.sr_drug_instruction || row.comment)}</td>
        </tr>`
			)
			.join("");
	const table = (items) => `<div class="table-responsive"><table class="table table-bordered">
        <thead><tr><th>Class</th><th>Medication</th><th>Strength</th><th>Form</th><th>Dosage / frequency</th><th>Duration</th><th>Dispensing quantity / unit</th><th>Instructions</th></tr></thead>
        <tbody>${rows(items)}</tbody></table></div>`;
	const previous = (context.previous || [])
		.slice()
		.reverse()
		.map(
			(record) =>
				`<p><strong>${escape(record.date)} — ${escape(record.id)}</strong>
        (${escape(record.status)}, docstatus ${escape(record.docstatus)})</p>${table(
					record.prescriptions
				)}`
		)
		.join("");
	const notes = Object.entries(context.current?.notes || {})
		.filter(([, value]) => value)
		.map(
			([key, value]) =>
				`<p><strong>${escape(key.replace(/^sr_/, ""))}</strong><br>${escape(value)}</p>`
		)
		.join("");
	const patientHistory = Object.entries(context.patient_history || {})
		.filter(([key]) => !["id", "modified"].includes(key))
		.map(
			([key, value]) =>
				`<p><strong>${escape(key.replaceAll("_", " "))}</strong>: ${escape(
					value || "Not documented"
				)}</p>`
		)
		.join("");
	const proposed = (frm.doc.items || []).map((row) => ({
		...(catalog.medications || []).find(
			(medication) => medication.medication === row.medication
		),
		...row,
	}));
	const resultValue = (value) => {
		if (Array.isArray(value)) {
			return value
				.map(
					(row) =>
						`<p>${Object.entries(row)
							.filter(([, item]) => item !== "" && item != null)
							.map(
								([key, item]) =>
									`<strong>${escape(
										key.replaceAll("_", " ")
									)}:</strong> ${escape(item)}`
							)
							.join("; ")}</p>`
				)
				.join("");
		}
		return escape(value);
	};
	const investigations = (context.investigations || [])
		.map((source) => {
			const details = Object.entries(source.details || {})
				.filter(
					([, value]) =>
						value !== "" && value != null && (!Array.isArray(value) || value.length)
				)
				.map(
					([key, value]) =>
						`<div><strong>${escape(key.replaceAll("_", " "))}</strong>: ${resultValue(
							value
						)}</div>`
				)
				.join("");
			return `<details><summary>${escape(source.date)} — ${escape(source.id)} (${escape(
				source.status
			)})</summary>${details}</details>`;
		})
		.join("");
	const reports = (context.attached_reports || [])
		.map((source) => {
			const extraction = (context.report_extractions || []).find(
				(item) => item.id === source.id
			);
			const pages = extraction?.result?.pages;
			const text = Array.isArray(pages)
				? pages
						.map(
							(page) => `<p><strong>Page ${escape(page?.page)} ? ${
								page?.readable === true ? "Read" : "Unreadable / incomplete"
							}</strong></p>
			<pre style="white-space:pre-wrap">${escape(page?.text)}</pre>
			<p>${escape(Array.isArray(page?.uncertainties) ? page.uncertainties.join("; ") : "")}</p>`
						)
						.join("")
				: "Not read yet.";
			return `<details><summary>${escape(source.filename)} ? ${escape(source.id)} (${escape(
				source.pages
			)} pages)</summary>
			<p>${escape(
				(source.references || []).map((ref) => ref.encounter).join(", ")
			)}</p>${text}</details>`;
		})
		.join("");
	frm.fields_dict.review_html.$wrapper.html(`<h4>Medical history</h4>${patientHistory}
        <h4>Current clinical notes</h4>${notes}
        <h4>Previous prescriptions</h4>${previous || "No history available yet."}
		<h4>Laboratory tests and results</h4>${
			investigations || "No laboratory records in the supplied history."
		}
		${
			context.investigations_truncated
				? "<p>Laboratory history was limited; review completeness.</p>"
				: ""
		}
        <h4>Attached reports</h4>${reports || "No attached reports in the selected encounters."}
        ${
			context.attached_reports?.length
				? "<p>Check extracted values, units, dates and patient identity against the originals before approval.</p>"
				: ""
		}
        <h4>Proposed prescription</h4>${table(proposed)}`);
}
