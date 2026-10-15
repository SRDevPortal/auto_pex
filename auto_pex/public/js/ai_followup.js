frappe.ui.form.on("Patient Encounter", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("Auto Pex AI Reviewer")) return;
		if (
			frm.doc.sr_encounter_type !== "Order" ||
			frm.doc.sr_encounter_place !== "Online" ||
			frm.doc.sr_sales_type !== "Repeat"
		)
			return;
		frm.add_custom_button(
			__("View AI Drafts"),
			() => {
				frappe.set_route("List", "Auto Pex AI Draft", { encounter: frm.doc.name });
			},
			__("AI Follow-up")
		);
		const generate = async (regenerate) => {
			if (frm.is_dirty()) {
				frappe.msgprint(
					__("Save the encounter and updated notes before generating a draft.")
				);
				return;
			}
			const result = await frappe.call({
				method: "auto_pex.ai.workflow.request_draft",
				args: { encounter: frm.doc.name, regenerate },
				freeze: true,
				freeze_message: __("Queuing AI draft"),
			});
			if (result.message) frappe.set_route("Form", "Auto Pex AI Draft", result.message);
		};
		frm.add_custom_button(__("Generate AI Draft"), () => generate(0), __("AI Follow-up"));
		frm.add_custom_button(__("Regenerate AI Draft"), () => generate(1), __("AI Follow-up"));
	},
});
