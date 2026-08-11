const PRACTITIONER_PATHY_FILTERS = {
	ayurvedic_practitioner: ["Ayurveda"],
	homeopathic_practitioner: ["Homeopathy"],
	allopathic_practitioner: ["Allopathy"],
	healthcare_practitioner: ["Homeopathy"],
};

frappe.ui.form.on("Auto Pex Item Mapping", {
	setup(frm) {
		if (frm.fields_dict.set_encounter_status) {
			frm.set_query("set_encounter_status", () => ({
				filters: { is_active: 1 },
			}));
		}

		Object.entries(PRACTITIONER_PATHY_FILTERS).forEach(([fieldname, allowed]) => {
			if (!frm.fields_dict[fieldname]) return;

			frm.set_query(fieldname, () => ({
				filters: {
					sr_pathy: ["in", allowed],
					status: "Active",
				},
			}));
		});
	},
});
