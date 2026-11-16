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
3. Enter your OpenAI model ID (supporting Responses API Structured Outputs and PDF/image inputs) and API
   key in the Password field. Paste the team-maintained medicine catalog and
   clinician-approved prescribing guidance using the format documented below. These
   credentials stay server-side; never put them in browser code or Git.
4. Choose **Local read-only API** for this site's records. For a remote source,
   configure an HTTPS GET endpoint implementing the contract below and the complete
   Authorization header for a read-only service account. Redirects are rejected.
5. Configure lookback, history count, timeout and retries. The medication catalog
   combines pasted mappings with active medications from historical prescriptions. To permit additional
   medications, list exact Medication IDs in **Additional Permitted Medications**.
   The deduplicated union of pasted, additional and historical medicines supports up to
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
There is no readiness-checkbox requirement; save complete clinical notes before requesting a draft.

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
  "investigations": [],
  "investigations_truncated": false,
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

Structured history forwards only allowlisted clinical fields; separate patient IDs,
names and contact fields are excluded. Encounter report reading additionally sends the complete
original PDF/image to OpenAI. Those files, their names and free-text notes may contain patient
identifiers. Review data-sharing and retention arrangements before enabling. The Responses request uses `store: false`; this does not itself remove
all provider retention. See [OpenAI data controls](https://developers.openai.com/api/docs/guides/your-data)
and [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

Drafts store clinical snapshots, original responses, edited prescriptions, prompt/model
versions, token usage and reviewer decisions as restricted clinical audit records.
Application deletion is blocked; define retention through your organization's controlled
clinical-record process. Error messages exclude remote response bodies and credentials.
No real patient data needs to leave the site to run the automated tests:

```bash
./env/bin/python -m unittest auto_pex.ai.test_followup -v
./env/bin/python -m unittest auto_pex.ai.test_catalog_requirements auto_pex.ai.test_investigations auto_pex.ai.test_reports -v
bench --site YOUR_SITE execute auto_pex.ai.test_site_integration.run
```

These tests use mocked network responses. Validate configured status transitions and
clinician-reviewed sample cases in your staging environment before enabling automatic
generation. An OpenAI model's structured output is not a clinical accuracy guarantee.

#### Pasted catalog and prescribing instructions

The team maintains all formulations, medicine choices, clinical rules and instructions.
Open **Auto Pex AI Settings** and paste the catalog into **Medicine Catalog and
Formulations** and the complete prescribing prompt into **Prescription Instructions / Prompt**
(the existing `prescribing_constraints` field). No uploads or composition records are needed.
The upgrade disables **Generate when eligible encounter is saved** once. Populate both fields,
verify a manually generated draft with a clinician, and only then enable automatic generation.
Manual generation also requires **Enable AI Follow-up Drafts**, model/credentials and workflow configuration.

Accepted format is UTF-8 CSV text with exact, case-sensitive column headers:

```csv
code,medication_id,name,formulation,drug_code
TEAM-CODE,EXACT-ERP-MEDICATION-ID,Team display name,"Team-supplied formulation, including commas
and additional lines",EXACT-LINKED-ITEM-ID
```

This is a format example, not clinical content. `medication_id` and `formulation` are required
columns and must be nonempty on every row. `code`, `name` and `drug_code` are optional columns.
Double embedded quotes (`""`) inside quoted values. A UTF-8 BOM and Windows newlines are accepted.
One row per exact Medication ID; codes must be unique ignoring case and cannot collide with another
permitted Medication ID. Unknown/duplicate headers, duplicate mappings, missing values and malformed
CSV fail visibly. Names are descriptive only; no fuzzy mapping, potency conversion or substitution occurs.
Optional `drug_code` must be an active Item linked to the exact Medication and restricts that mapping
to that Item. Disabled/unsupported pasted medicines and invalid ERP mappings fail validation.

The permitted ERP catalog is the deduplicated union of pasted IDs, **Additional Permitted Medications**
and historical prescription IDs. The 150-medicine limit applies to the combined union before disabled
historical medicines are excluded. **Enforced Medication Rules** always apply, including blocked medicines
and dosage/duration/item restrictions; a pasted formulation or prompt cannot override them. Conflicting
proposals fail validation. Additional/history-only medicines retain their ERP identifiers but have no
pasted formulation unless the team supplies one. The separate **Maximum Draft Medications** setting
limits prescription rows, not the catalog size.

Limits are 500,000 UTF-8 bytes for the pasted catalog, 200,000 UTF-8 bytes for prescribing instructions,
and 2,000,000 UTF-8 bytes for the serialized AI request. The existing 150,000-character normalized-history
limit also remains. Oversized inputs fail explicitly; no catalog or instructions are sliced. Provider
context-window limits can be smaller than these application limits; rejected requests fail visibly and
require adjusting the configured model or supplied content. The app sends the complete saved catalog
text and instructions, plus parsed mappings, ERP references and the configured patient history.

History includes all readable prior Patient Encounters in the configured window (including Order and
Followup), their clinical-note fields including `sr_investigations`, and all three prescription tables.
Separate **Lab Test** documents and **Observation** records with category `Laboratory` are included
as `investigations`, in addition to encounter notes. Normal, descriptive, organism and sensitivity result
rows include recorded values, units, reference ranges and comments. Laboratory Observations include
component records, their canonical `result_data` (including a recorded zero), typed text/select/boolean/date/time
results, units, reference and interpretation. The unused hidden numeric cache is excluded to avoid presenting
a default zero as a finding. Diagnostic Report is a grouping record; its underlying laboratory Observations
provide the results. Files attached to the selected encounters are read as described below.
Files attached only to separate laboratory/Observation documents remain indicators; attach those reports
to the encounter to include their file contents.

The existing history count applies independently to prior encounters and to the combined Lab Test/Observation
list (at most 100 each). Lab dates use `result_date`, then `date`, then creation; Observations use `time_of_result`,
then `posting_date`, then creation. All are restricted to the configured lookback through the encounter date.
Other patients, future, cancelled, rejected and entered-in-error results are excluded. Pending/unapproved
results remain explicitly labelled and generate a clinician-review warning. Count truncation is flagged;
remote responses are additionally restricted to the configured date window/count. Each request derives the patient from the authorized encounter. Remote endpoints must
still enforce the documented identity/source contract. A prior prescription is required.

Draft rows retain the exact Medication and Item IDs, dosage/frequency schedule (`Prescription Dosage`),
dosage form, duration, administration instructions, and **Dispensing Quantity / Unit**. Quantity must be
positive and finite, and unit must be an exact ERP UOM. On approval these are stored as
`Drug Prescription.auto_pex_dispensing_quantity` and `auto_pex_dispensing_uom` on all three medication-class
tables. They are separate from dosage and medication strength. Free-text administration instructions go
into `Drug Prescription.comment`; `sr_drug_instruction` is a Link and is not populated with arbitrary text.
This change does not submit the encounter or create dispensing/billing orders.

Every generated draft saves its complete instructions (including the application prompt), raw pasted
catalog, history and ERP catalog snapshots. Audit snapshots exclude credentials and cannot be edited
through the draft form. Configuration hashing includes both pasted fields. Approval rechecks configuration,
encounter and source history. Unchanged-save reuse also checks configuration and, for drafts with a history
snapshot, the history/catalog fingerprint. Pending jobs recheck freshness during generation. Old drafts
without the current prompt version must be regenerated; approved prescriptions remain protected.

#### Reading attached encounter reports

Before preparing a prescription draft, Auto Pex reads all local uploaded reports attached to the current
encounter and the prior encounters selected by the history window/count. Both standard paperclip attachments
and Attach / Attach Image fields (including report child tables) are supported. Each source encounter and File
must be readable by the generation user. Files belonging to a different patient or an unrelated document
are rejected. Arbitrary external URLs are never downloaded; upload the original file to this ERP site.
With remote history, only the current local encounter's attachments are available; a mandatory review warning
states that remote prior attachments were not read. Remote history cannot supply arbitrary file URLs.

PDFs (including scanned PDFs), JPEG, PNG and WebP images use the configured model through the
[Responses PDF input](https://developers.openai.com/api/docs/guides/file-inputs) and
[image input](https://developers.openai.com/api/docs/guides/images-vision) APIs. The model must support
both vision/file inputs and Structured Outputs. Each file requires an additional extraction request before
the prescription request, adding model usage and processing time. The background job allows up to
three hours for a maximum-size batch with retries; individual request timeouts still follow settings. Requests use inline file data and
`store: false`; there is no separate Files API upload or public download link. As above, `store: false`
does not itself eliminate provider retention. Extracted text is instructed to omit patient identifiers,
but the original report is sent intact and AI redaction is not guaranteed.

The reader preserves dates, values, units, reference ranges, impressions and clinical comments page by page.
Report text is untrusted clinical data, never prescribing instructions. Every page must be returned in order.
Unreadable clinical content, missing pages, incomplete responses, encrypted PDFs, unsupported/damaged files,
permission failures and excessive input sizes stop generation before a prescription request. No report is
silently skipped or truncated. Supported bounds: 20 files, 10 MiB per file, 40 MiB total; 20 pages per PDF,
100 pages total; single-frame images up to 25 million pixels; extracted text up to 120,000 UTF-8 bytes per
report, also subject to the existing 150,000-character combined context limit. Reduce the history range or
supply clearer/smaller files if limits are exceeded. Office documents need to be exported to PDF.

The draft review shows each filename/source and extracted page text, including unreadable/partial output
when extraction failed. Extraction prompt/version, model, response IDs and usage are retained in restricted
audit snapshots. File bytes/base64 are not saved in draft snapshots. The prescription model receives the
extracted text with `File:NAME` source IDs, alongside encounter history and laboratory results. A report
review warning requires the clinician to compare values, units, dates and patient identity with the originals
and document resolution before approval. AI extraction is not a guarantee of clinical or transcription accuracy.

Freshness checks hash actual file bytes and references, detecting added, removed, replaced or relinked files.
Checks run again after extraction, after drafting, on draft reuse and before approval. Approval locks File
records as well as local clinical sources; out-of-band filesystem writes cannot be transactionally locked.
Changing a report requires regeneration; existing pending drafts from the older prompt version also require
regeneration. Automatic generation remains controlled by the existing settings.

#### Laboratory history API extension

Remote history adapters must now return `investigations` (use `[]` when none) and should return
`investigations_truncated`. An adapter omitting the section fails visibly rather than silently leaving
lab results out. Each record requires its own `patient_id`, checked against the current encounter and
removed before model input. Example source (all content below is synthetic):

```json
{
  "id": "Lab Test:LAB-EXAMPLE",
  "doctype": "Lab Test",
  "name": "LAB-EXAMPLE",
  "patient_id": "EXACT-LOCAL-PATIENT-ID",
  "date": "2026-09-30",
  "modified": "2026-09-30 10:00:00",
  "docstatus": 1,
  "status": "Approved",
  "details": {
    "lab_test_name": "Synthetic panel",
    "normal_test_items": [{
      "lab_test_event": "Synthetic analyte",
      "result_value": "Recorded value",
      "lab_test_uom": "Recorded unit",
      "normal_range": "Recorded reference range"
    }]
  }
}
```

Observation source IDs use `Observation:NAME`; details must include
`observation_category: "Laboratory"` and Boolean `attachment_present`. Allowed clinical fields and result
tables are defined in `auto_pex/ai/investigations.py`; arbitrary extra fields and attachment URLs are never
forwarded. Each result table supports up to 1,000 rows, subject to the existing total-history size limit;
oversize inputs fail without slicing result rows. The model can cite these source IDs. Saved snapshots,
unchanged-save reuse and approval freshness checks include the result values and statuses, so even a child-row
correction without a changed parent timestamp invalidates the draft. Local source parents are locked during
approval and snapshot access requires read access to those local records. Local generation users need read
permissions for Lab Test and Observation in addition to the existing healthcare records. Remote access remains
subject to the configured service account and the existing remote transaction boundary. The prompt-version
change invalidates drafts generated before laboratory support; regenerate pending drafts.

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
