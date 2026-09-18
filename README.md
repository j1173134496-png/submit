# Submit Flow Agent

Submit Flow Agent is a Python workflow for turning monthly photovoltaic source documents into validated Excel delivery files. It provides file classification, OCR-result mapping, normalization, business validation, review gates, task state management, and a structured worker contract for gateway integrations.

This repository is a public source snapshot. Customer-specific site registries, meter identifiers, prices, workbook templates, source PDFs/Excel files, runtime task data, and local credentials are intentionally excluded. Start with the example configuration and provide private site data and templates outside Git before running a real workflow.

## Layout

- `src/submit_flow_agent/` — application and CLI code
- `tests/` — unit and contract tests
- `spec/` — product, architecture, and integration contracts
- `config/sites.json` — private deployment configuration; use the public example as a starting point
- `config/templates/` — private workbook/template inputs
- `runtime/` and `outputs/` — local task state and generated delivery files

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Provide a private `config/sites.json` and matching workbook templates, then run the CLI through the documented contracts in `spec/contracts/`.

## Safety boundary

The worker accepts only structured, schema-validated actions. Failed validation, missing fields, low-confidence OCR, and ambiguous site/month matches must remain reviewable states; the workflow must not silently write a delivery workbook.
