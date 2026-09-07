### Auto Pex

Automated medication template and practitioner mapping for Patient Encounters

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch frappe-16
bench --site YOUR_SITE install-app auto_pex
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

### Frappe 16 compatibility

The compatibility branch is `migration/frappe-16-compat`, based on v15 commit
`1a1d05b07f9b16b23a45cf30a124518a97c95a6e`. The installation commands above
apply after the reviewed compatibility changes have been merged into `frappe-16`.

Requires Python 3.14, Frappe 16, ERPNext, Healthcare, and Sriaas Clinic.
Clinic setup must provide the SR medication, practitioner, encounter, and status fields.

Mapping failures restore all encounter fields changed by Auto Pex, including
original prescription child rows. Missing templates abort the mapping; errors
are logged without blocking the encounter save. Successful mappings retain the
v15 behavior of replacing prescription rows from the configured template.

The activation patch initializes only NULL flags. Explicitly inactive records
stay inactive. The status patch does not assign or reactivate an existing inactive
PRX Ready status; configured nonblank output statuses remain unchanged.

### Compatibility checks

Run the standalone regression suite from this repository using a Python environment
that already contains Frappe 16 (no app installation or database writes required):

```bash
/path/to/frappe-v16-bench/env/bin/python -m unittest auto_pex.tests.test_encounter_hooks -v
uvx --from ruff==0.12.12 ruff check auto_pex
uvx --from ruff==0.12.12 ruff format --check auto_pex
git diff --check
```

The DocType tests use Frappe 16 IntegrationTestCase/UnitTestCase and create
test-owned Items, Item Groups, and UOMs instead of selecting arbitrary existing Items.
Run these only after installing the candidate and its dependencies on an isolated
test site, with the standard ERPNext All Item Groups root available.

Before promotion, verify fresh installation and a restored-data upgrade, run the
full app test suite, run migrations twice, and check real Patient Encounter saves
and practitioner filters in the UI. The standalone suite does not replace these gates.
