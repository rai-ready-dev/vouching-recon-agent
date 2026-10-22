# Vouching & Reconciliation Agent

An audit agent that pairs **LLM reasoning** with a **deterministic calculation engine**, all on top of an
**immutable evidence layer**, with a human reviewer as the final authority.

```
 sources ─► Evidence layer (hashed, immutable) ◄─ every claim must cite it
                 │
        Orchestrator (workflow + control flow)
          │                         │
 Deterministic engine         Reasoning layer (LLM / rule-based)
 recompute · 3-way match      classify · explain · draft
 duplicates · bank rec        (proposes only, never calculates)
 tolerance · 1:many · aging
          └───────────┬─────────────┘
                 Verifier  ── rejects fabricated figures / evidence, blocks "clearing" flagged cases
                      │
              Policy router ── auto-clear · review · escalate
                      │
         Workpaper + hash-chained audit log ─► Human reviewer
```

## Quick start (web app)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
vra-web serve --solo --open            # opens http://localhost:8000
```

First visit: create the administrator account. Then **New engagement -> upload the ledger (Excel/CSV) and the
supporting documents -> Run**. Review exceptions with the document beside each finding, correct anything the
agent misread, decide (resolve / confirm exception / follow-up), then sign off and download the Word / Excel
workpaper. Full team guide: [docs/WEB_APP.md](docs/WEB_APP.md).

* `--solo` lets one person sign off their own run (testing). Leave it off for teams: a different reviewer must sign off.
* Connect Foundry either on the **Settings** page (admin; no restart) or in a `.env` file in the folder you start from
  (see `.env.example`). Real environment variables win over `.env`; settings saved on the Settings page win over both.
  Check it from the command line with `python -m vra check-foundry`.
* Data (database, uploaded documents, run outputs) lives in `./vra_data` (change with `--data-dir`). Back this folder up.

## Quick start (command line)

```bash
pytest                                                  # 105 tests
vra demo-docs                                           # mixed-format documents + ledger -> demo_docs_output/
vra demo-bank                                           # cash book vouched to a PDF + Excel bank statement (see samples/)
vra demo                                                # structured three-way-match + bank-rec demos
```

## What it can vouch

**Any source of truth against any supporting documents.** The *source of truth* is the set of records you are
vouching: a ledger, journal, bank statement, schedule or listing, as Excel/CSV with **any column layout**, or as a
PDF, Word file, text, email or scan. Several source files can be combined. The *supporting documents* are whatever
backs those records up. A vouching programme (chosen by each entry's `Type`) tests each entry against its document.

| Step | What happens |
|---|---|
| Read the source | Spreadsheets: automatic header detection plus a **preview-and-map-columns** screen for unusual layouts. PDF/Word/text: entries read line by line, or by the AI reader (rows must be quoted from the document). |
| Read the documents | PDF (text layer), scans/images (OCR), Excel, Word, CSV, text, email. Unreadable, encrypted, empty or unsupported files are **reported, never skipped silently**. Files are read in parallel and results are cached. |
| Extract | Date, subtotal, tax, total, currency, parties, document numbers, approvals, document type. Offline reader, or an AI reader that must return a **verbatim quote** for every field: anything not found in the document is dropped. |
| Link | By reference, amount, counterparty and date, with typo-tolerant references, reviewer-taught company aliases, **split payments** (several documents that add up to one entry) and an optional **AI adjudicator** for what the rules cannot match (it can only choose from real candidates and every AI link is flagged for confirmation). |
| Test | Right document type, amount, counterparty, reference, currency, date and cut-off, document reused, approval above a limit, **document arithmetic** (subtotal + tax = total), **document integrity** (PDF/Word/Excel edited after creation or by an editing tool), extraction quality. Optional risk signals: weekend postings, round amounts, just below the approval limit. |
| Sample | Clean entries are sampled for a look by rule, or by reproducible **monetary-unit sampling**. |
| Report | Exceptions with evidence, unreadable documents, documents matching **no** entry (possible unrecorded items), population analytics (Benford, reference gaps, concentration, repeated amounts). |

```bash
vra extract    --docs my_docs                     # just show what was read from each document
vra vouch-docs --ledger gl.xlsx --docs my_docs --period-start 2026-01-01 --period-end 2026-03-31 --out out
vra vouch-docs --ledger gl.xlsx --docs my_docs --programmes my_programmes.json    # add/change tests per type
```
Programme override file (JSON): `{"lease": [{"test": "amount_agrees"}, {"test": "date_ok", "max_days_after": 10}]}`.
Tests: `doc_type, amount_agrees, counterparty_agrees, reference_agrees, currency_agrees, date_ok, voucher_not_reused,
approval_present, extraction_quality, doc_arithmetic, document_integrity, risk_signals`.

## Measuring the agent (Performance tab)

Every run records what it did, so you can judge it with evidence rather than impressions:

* **Speed and cost**: time per stage, documents per second, AI calls, tokens, estimated cost (enter your prices in Settings), cache hits.
* **AI usage** by purpose (reading, explaining, linking, OCR): calls, tokens, average and slowest times, retries, failures.
* **Guard rails**: values the model produced that were *not in the document* (dropped), explanations the verifier rejected, model failures that fell back to a person.
* **Reader comparison**: tick *Also read every document with the other reader*; the built-in and AI readers are compared field by field and every disagreement is listed.
* **Reviewer feedback**: of the items the agent flagged, how many reviewers confirmed vs dismissed (precision per finding type), how many documents had to be corrected, how many company aliases were learned.
* **Answer key**: give the right answers for entries you know (`R12,AMOUNT_MISMATCH` or `R13,CLEAN`) and get recall, precision, specificity, missed items and false alarms.

### Structured workflows (earlier modules)
`vra vouch --data folder` (invoice/PO/GRN three-way match with cumulative billing and duplicate/split
detection) and `vra reconcile --bank bank.csv --gl gl.csv --as-of DATE` (bank rec with one-to-many matching,
ageing and proof). `vra demo` runs both on seeded data.

## Using GPT from Microsoft Foundry

1. In Foundry, deploy a chat model (use a vision-capable one if you want scans read). Note the **deployment name**.
2. `pip install -e ".[foundry]"` (add `".[scan]"` to OCR scanned PDFs).
3. Set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT` (see `.env.example`). Either set
   `AZURE_OPENAI_API_KEY`, or leave it empty and run `az login`: the app then signs in with Microsoft Entra ID.
4. Run with the pieces you want to switch on:
```bash
vra --extractor foundry --analyst foundry --ocr foundry demo-docs
```
`--extractor` reads documents with the model (with quote checking), `--analyst` has it classify and explain
exceptions (checked by the verifier), `--ocr` has it transcribe scans. Each is independent; the default is
fully offline. **These Foundry code paths are written but untested against a live deployment**; expect small
adjustments (API version, parameter support of your model) on first use. Confirm with your firm that sending
client documents to your Foundry tenant is approved.

## Design rules (what makes it trustworthy)

1. **The LLM never calculates.** Totals, variances, matches, ageing and duplicates come from
   `src/vra/engine/` using `Decimal`/integer cents.
2. **Propose, then verify.** `verifier.py` rejects an analysis that cites unknown or out-of-case evidence,
   quotes a figure that differs from the record, introduces a money figure found nowhere in the evidence
   or engine output, or tries to clear a medium/high case.
3. **Evidence is first-class.** Records are frozen and SHA-256 stamped; conflicting re-adds are refused and
   `verify_integrity()` runs at the end of every run.
4. **Document text is data, not instructions.** It is passed to the model inside
   `<untrusted_document_data>`, and the model has no write tools. The verifier is the backstop.
5. **Failure is loud.** Model outage/parse failure -> baseline analysis + `ESCALATE` + audit entry.
6. **Policy is config.** `policy.py` (tolerances, materiality, approval limit, ageing days) is versioned and
   stamped on every run.
7. **Reproducible.** Audit log is hash-chained; each case records analyst, classification, problems, disposition.
8. **Nothing posts.** The agent is read-only; it recommends, a human decides.

## Layout

```
src/vra/
  records.py  evidence.py  findings.py  policy.py  money.py  textutil.py
  ingest.py  extract.py  intake.py  ledger.py  source_rows.py  linking.py  adjudicator.py  programmes.py   # document vouching
  sampling.py  analytics.py  llm.py (Foundry client, retries, usage metering, OCR)  analysts.py  verifier.py  reports.py  orchestrator.py
  engine/     recompute · three_way · duplicates · matching · bank_rec · aging
  audit_log.py workpaper.py loaders.py sample_data.py sample_docs.py evaluation.py envfile.py cli.py
  web/        app.py (API) service.py runner.py exports.py auth.py db.py main.py static/ (the UI)
tests/        105 tests: formats, grounding, linking, AI paths with a stand-in model, web API, permissions, uploads
docs/         ARCHITECTURE.md (target design and roadmap)
```

## Known limits / next steps

See `docs/ARCHITECTURE.md` and `docs/WEB_APP.md`. Open items: Foundry paths untested against a live
deployment; SQLite is single-server (move to Postgres for larger teams); sign-in is local accounts (Microsoft
Entra ID SSO is the planned replacement); split payments (one entry, several documents) are not linked; FX
conversion; sampling support; the structured three-way-match and bank-rec workflows are command-line only.
