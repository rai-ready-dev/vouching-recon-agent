# Architecture and roadmap

## Principle
The model reads and explains; code decides and calculates; every conclusion cites evidence; a person signs off.

```
 documents (pdf/scan/xlsx/docx/txt/eml)      ledger (xlsx/csv)
        │ ingest.py (+OCR)                          │ ledger.py
        ▼                                           ▼
   extract.py ── value must appear in the doc ──► Voucher        Transaction
        └───────────────► Evidence store (hashed, immutable) ◄────────┘
                                   │
                  linking.py  → programmes.py (tests per transaction type) → Findings
                                   │
              analyst (rule-based | Foundry GPT) proposes classification + explanation
                                   │
                    verifier.py  (rejects invented figures/evidence, can't clear risky cases)
                                   │
                  router (auto-clear | review | escalate) → workpaper + hash-chained audit log
```

## Why "versatile" is done with programmes, not special cases
A vouching programme is data: a list of assertion tests per transaction type (`programmes.py`, overridable
by JSON). New transaction types and new tests are configuration, not new workflows. The same ledger can mix
purchases, sales, expenses, payroll and assets.

## Trust controls already in place
* Extracted values are grounded in document text (LLM must quote; quote and value are verified).
* The verifier rejects fabricated evidence ids, mismatched figures, unsupported money figures, and any
  attempt to clear a medium/high finding.
* Documents are untrusted input (wrapped, never executed as instructions).
* Unreadable/unsupported/scanned-without-OCR documents are reported as issues.
* Evidence hashes, tamper-evident audit log, policy version stamped on each run.

## Roadmap to a team application
1. **Web app (next).** FastAPI backend + browser UI: create an engagement, upload ledger and documents,
   run, review queue with the document beside each finding, accept/override with reason, sign-off, export
   workpaper to Word/Excel/PDF.
2. **Storage.** Postgres (runs, findings, decisions, users) + object storage for documents (Azure Blob).
3. **Identity.** Microsoft Entra ID sign-in; roles: preparer, reviewer, admin; preparer cannot sign off own work.
4. **Deployment.** Azure Container Apps/App Service in the firm's tenant; Foundry model in the same tenant;
   private networking; no document data leaves the tenant.
5. **Quality.** Golden dataset from anonymised past engagements; track recall on true exceptions per
   transaction type; shadow-mode period before relying on auto-clear.
6. **Learning.** Reviewer overrides become vendor aliases, per-client rules and few-shot examples.
7. **Breadth.** Split payments, FX, multi-voucher statements, sampling, AR/AP-to-GL tie-outs,
   fixed-asset registers, payroll registers, tax filings, intercompany.
