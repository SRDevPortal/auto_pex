### Auto Pex

Automated medication template and practitioner mapping for Patient Encounters

### AI follow-up prescription drafts

Auto Pex can generate a clinician-reviewed draft for a **draft Patient Encounter** with
`sr_encounter_type = Order`, `sr_encounter_place = Online`, and `sr_sales_type = Repeat`.
Fresh-patient item mappings continue to use the existing rules. While AI is enabled,
matching repeat encounters bypass template mapping. Any encounter with an AI draft
remains protected from template replacement even if AI is subsequently disabled.

The model has no write tools. Clinical history is read through a permission-checked
GET API. Only the application applies a reviewed prescription, in the same database
transaction as its configured encounter-status transition. Approval does not submit
the encounter or impersonate a practitioner.

Historical clinical notes are data, not instructions to the AI. Medication-change
directions in the **current Order encounter's clinical notes** instruct the draft:
for example, “add Medicine A” or “remove Medicine B.” Additions cite that encounter;
removals are omitted from the proposed prescription and explained in its summary.
The proposal contains the complete prescription, not just the changed rows.
These directions still require valid catalog identifiers, configured prescribing
constraints and clinician approval. If a requested new medicine is not already in
the permitted catalog, add its exact Medication ID under **Additional Permitted
Medications**. Ambiguous or unsupported changes are reported for review.
After a prompt-policy update, regenerate pending drafts before approval.

#### Install and configure

After updating the app, install its Python dependencies and migrate the target site:

```bash
cd /path/to/frappe-bench
./env/bin/pip install -e apps/auto_pex
bench --site YOUR_SITE migrate
```

Restart existing web and worker processes using your normal bench deployment process.
The migration creates the `Auto Pex AI Reviewer` role and hides the legacy
`Clinical notes ready for AI` checkbox. AI is disabled by default.

1. Open **Auto Pex AI Settings** as a System Manager.
2. Select three distinct, active `SR Encounter Status` records: eligible, draft review,
   and clinician approved. These must also be permitted by your clinic's existing
   encounter workflow. No status names are invented or hardcoded by Auto Pex.
3. Enter your OpenAI model ID (supporting Responses API Structured Outputs) and API
   key in the Password field. Add clinician-approved prescribing guidance. These
   credentials stay server-side; never put them in browser code or Git.
4. Choose **Local read-only API** for this site's records. For a remote source,
   configure an HTTPS GET endpoint implementing the contract below and the complete
   Authorization header for a read-only service account. Redirects are rejected.
5. Configure lookback, history count, timeout and retries. The medication catalog
   contains active medications from historical prescriptions. To permit additional
   medications, list exact Medication IDs in **Additional Permitted Medications**.
   The deduplicated union of additional and historical medicines supports up to
   150 entries. Larger catalogs fail explicitly rather than being truncated.
   Read permissions and active, supported medication checks still apply.
6. Optionally configure deterministic **Enforced Medication Rules**, for example:

   ```json
   {
     "EXACT-MEDICATION-ID": {
       "dosages": ["EXACT-DOSAGE-ID"],
       "periods": ["EXACT-DURATION-ID"],
       "drug_codes": ["EXACT-ITEM-ID"],
       "blocked": false
     }
   }
   ```

   Omit a restriction to allow the corresponding catalog values. An empty array
   allows no values. Rules are checked on generation, edits and approval; prose
   guidance is sent to the model and is not a deterministic clinical rule engine.
7. Assign **Auto Pex AI Reviewer** to authorized clinicians alongside their existing
   healthcare permissions. They need read access to patients, source encounters and
   medication catalogs, and write access to the target encounter. Approval also
   requires a Healthcare Practitioner linked to the signed-in user.
8. Enable AI. Optionally enable automatic generation and select an enabled automation
   user with the reviewer role and the required record permissions. Automatic jobs
   use that user's access, not unrestricted Administrator access.

The history limit is intentional: truncated history and unsubmitted historical
encounters produce review warnings. Missing prior prescriptions or current notes
prevent generation. This implementation supports dosage-reference prescriptions;
interval regimens and strength changes require manual clinical review. Medication
identifiers, forms, strengths, linked items and allowed dosage/duration values are
checked against the local catalog. This is not an interaction-checking or diagnostic
engine; a licensed clinician remains responsible for clinical suitability.

#### Clinician workflow

- Save an eligible encounter with clinical notes; no readiness checkbox is required.
- Keep **Submit Orders on Save** off while using this draft workflow; encounters with
  that option enabled are ineligible to avoid creating medication requests during review.
- Use **AI Follow-up > Generate AI Draft**, or let the configured automatic job run.
  Generation happens on the long queue; keep short and long workers running.
- Once a usable draft is ready, the encounter moves to **Status before clinician approval**.
  Approval applies **Status after Clinician Approval**; both statuses remain configurable.
- Unchanged saves reuse the latest draft, including failed drafts (use **Regenerate** to retry).
  Changed clinical inputs automatically replace unapproved drafts, including while awaiting
  review. Old drafts remain **Stale** for audit and cannot be approved. Approved drafts
  are never automatically replaced.
- Open **View AI Drafts**. Use **Refresh Status** to see job completion. The draft
  shows previous prescriptions, current notes, proposed rows, source IDs and rationale.
- Edit proposed rows and instructions if needed. Document resolution of any warnings
  and check the concerns-reviewed checkbox. Save edits before approving or rejecting.
- **Approve and Apply** re-reads the history and catalog, checks permissions and source
  versions, applies the three medication-class tables and sets the approved status.
  Existing rows in those tables are replaced by the reviewed proposal.
- **Reject** records the reviewer and reason and leaves the encounter unchanged.
  **Regenerate** creates a new audit record and supersedes the older draft.

New notes, changed prescriptions, changed AI configuration, or changed source history
invalidate approval. Pending drafts are marked stale on encounter edits; external
history is checked when generation completes and again when approval is requested.
Local source parents are locked during final approval. Remote systems cannot share
the local transaction: their response must accurately reflect current versioned
records, and a change after the last GET is outside this application's lock boundary.
Clinical-note readiness should be unchecked while notes are incomplete.

If AI cannot support a draft, no prescription or encounter-status change is made.
An unable-to-draft result cannot be approved by merely editing the rows. Complete the
notes and regenerate or prescribe manually. Timeouts and invalid outputs leave the
encounter unchanged. Interrupted jobs expire after an hour through the hourly
scheduler, and require explicit regeneration rather than repeated automatic billing.

#### Read-only history API

```text
GET /api/method/auto_pex.ai.history.get_history
    ?encounter=ENCOUNTER-ID&history_days=365&history_limit=20
```

Authentication and Frappe record permissions are required. The patient's identity is
derived from the encounter. Calling this endpoint never writes clinical records.
Local generation calls this same handler directly without loopback HTTP credentials.

A remote endpoint receives the same query parameters and must return this object,
either directly or wrapped in Frappe's `message` key:

```json
{
  "patient_id": "EXACT-LOCAL-PATIENT-ID",
  "encounter_id": "CURRENT-ENCOUNTER-ID",
  "truncated": false,
  "patient_history": {
    "modified": "2026-10-13 09:00:00",
    "age_years": 40,
    "sex": "Female",
    "allergies": "Documented findings or explicitly unknown",
    "medication": "Documented current use or explicitly unknown",
    "medical_history": "Updated medical history",
    "surgical_history": "Documented history"
  },
  "current": {
    "id": "CURRENT-ENCOUNTER-ID",
    "date": "2026-10-13",
    "modified": "2026-10-13 09:00:00",
    "docstatus": 0,
    "status": "YOUR-ELIGIBLE-STATUS",
    "notes": {
      "sr_complaints": "Updated complaint",
      "sr_observations": "Updated observations",
      "sr_investigations": "Updated results",
      "sr_diagnosis": "Documented diagnosis",
      "sr_notes": "Updated follow-up notes"
    },
    "instruction": "",
    "prescriptions": []
  },
  "previous": [
    {
      "id": "PREVIOUS-ENCOUNTER-ID",
      "date": "2026-09-13",
      "modified": "2026-09-13 09:00:00",
      "docstatus": 1,
      "status": "YOUR-APPROVED-STATUS",
      "notes": {"sr_notes": "Previous clinical notes"},
      "instruction": "Previous instructions",
      "prescriptions": [
        {
          "medication_class": "Ayurvedic Medicine",
          "medication": "EXACT-MEDICATION-ID",
          "drug_code": "EXACT-ITEM-ID",
          "dosage": "EXACT-DOSAGE-ID",
          "period": "EXACT-DURATION-ID",
          "dosage_form": "EXACT-FORM-ID",
          "strength": 10,
          "strength_uom": "mg",
          "dosage_by_interval": 0,
          "interval": null,
          "interval_uom": null,
          "sr_drug_instruction": "Previous instructions",
          "comment": ""
        }
      ]
    }
  ]
}
```

Supported classes are `Ayurvedic Medicine`, `Homeopathic Medicine` and `Allopathic
Medicine`. Preserve document IDs, dates and modification timestamps. Exclude cancelled
records, future records and other patients. API field names that differ from this
contract need an adapter at the source. Previous records represent historical
prescriptions, not proof of current medication use.

#### Data handling and validation

Only allowlisted clinical fields are forwarded to OpenAI; separate patient IDs,
names and contact fields are excluded. Free-text clinical notes may still contain
identifying information, so review data-sharing and retention arrangements before
enabling. The Responses request uses `store: false`; this does not itself remove
all provider retention. See [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
and [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

Drafts store clinical snapshots, original responses, edited prescriptions, prompt/model
versions, token usage and reviewer decisions as restricted clinical audit records.
Application deletion is blocked; define retention through your organization's controlled
clinical-record process. Error messages exclude remote response bodies and credentials.
No real patient data needs to leave the site to run the automated tests:

```bash
./env/bin/python -m unittest auto_pex.ai.test_followup -v
bench --site YOUR_SITE execute auto_pex.ai.test_site_integration.run
```

These tests use mocked network responses. Validate configured status transitions and
clinician-reviewed sample cases in your staging environment before enabling automatic
generation. An OpenAI model's structured output is not a clinical accuracy guarantee.

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app auto_pex
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/auto_pex
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
