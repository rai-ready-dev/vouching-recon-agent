# Web app guide

## Run it
```powershell
pip install -e ".[dev]"
vra-web serve --open                 # team mode: someone other than the preparer must sign off
vra-web serve --solo --open          # one person testing: you may sign off your own run
vra-web serve --host 0.0.0.0 --port 8000   # let colleagues on your network connect (see Security)
vra-web add-user pat --role reviewer       # create a user without the UI
```
Options: `--data-dir D` (default `./vra_data`). The server loads `.env` from the folder you start it in.

## Typical workflow
1. **New engagement**: name, client, audit period (enables cut-off testing), default transaction type.
2. **Files & run**: upload the ledger (one Excel/CSV, header row with at least Date and Amount; optional Type,
   Counterparty, Reference, Currency, Approver, Description) and the documents (files, a folder, or a `.zip`).
   Choose how documents are read and explained (offline built-in, or Microsoft Foundry), then **Run vouching**.
3. **Results**: tiles, warnings (unreadable files, documents matching no ledger entry), and a queue of items
   that need a decision. Click an item: findings, ledger-vs-document comparison, the document itself, and the
   decision box. Arrow keys move between items.
4. **Fix the input**: if the agent misread a document ("Correct what was read") or linked the wrong one ("Link a
   different document"), save the correction and press **Re-run**. Decisions on items whose facts did not change
   are carried forward automatically.
5. **Documents** tab: everything that was read, confidence, which entry each document supports, problems.
6. **Workpaper & sign-off**: download Word / Excel / Markdown / audit log. A reviewer signs off once every
   exception has a decision; the run is then locked.

## The source of truth
Upload one or several: Excel/CSV (any layout), or a PDF/Word/text/email/scan. For a spreadsheet, **Preview & map
columns** shows the detected header row, suggests a mapping and lets you correct it (and the sheet, and the type
for entries without a type column). From a document, entries are read line by line (a date, text and an amount); use
the AI reader for harder layouts. If a statement shows a running balance, the entry amount (not the balance) is used.

## Options when you run
* **Reader / explanations / scans**: built-in (offline) or Microsoft Foundry.
* **Extra risk signals**: weekend postings, round amounts, just below the approval limit (low severity, zero exposure, so they never need a decision on their own).
* **AI link suggestions**: for entries the rules could not match. The AI can only choose among real candidates, and each AI link is flagged *AI-suggested* for you to confirm.
* **Compare readers**: reads every document twice (built-in and AI) and lists the disagreements on the Performance tab.
* **Sampling**: pick one clean entry per N of value (monetary-unit sampling, reproducible with the seed), instead of the simple "above the limit" rule.

## Vouching to bank statements
A bank statement (PDF, Excel or CSV) is read **line by line**: every dated entry becomes its own piece of evidence, the
running balance is never mistaken for the amount, and opening/closing balance lines are ignored. Use transaction types
`bank_payment` and `bank_receipt`. Dates may differ from the book date by a few days (cheques clear later); the audit
period drives the cut-off test. A **batched deposit** (one statement line equal to several cash book receipts of the same
kind close in date) is matched as a batch and not flagged. Statement lines with no cash book entry (bank charges,
interest) are listed individually under *documents not linked to any entry*. A worked example with an answer key is in
`samples/bank_statement_demo/`.

## Teaching the agent
* **Correct what was read** (including subtotal) and **Link a different document**: applied on the next run, recorded with your name, with your other decisions carried forward.
* **These are the same company**: when a document names the supplier differently from the ledger. The alias is remembered for the engagement.

## Performance tab
See the README. The answer key format is one line per entry: the entry id as shown in the app (`R12`) and the findings
it should raise separated by `;`, or `CLEAN`. A header row is ignored. Entries not listed are not scored.

## Connecting Foundry
Set the endpoint, deployment name and key in `.env` in the folder where you start the server, then restart the app.
Real environment variables take priority. Legacy `foundry.env` files in the data folder are ignored.
The Foundry section of **Settings** is read-only; administrators can inspect non-secret connection details and use
**Test connection** for latency, tokens and plain instructions for common failures (401, 403, 404, 429, network).
Leave the key unset to use Microsoft Entra ID (`az login`). Optional `VRA_PRICE_IN_PER_M` and
`VRA_PRICE_OUT_PER_M` values in `.env` enable cost estimates. Keep `.env` private and never commit API keys.

## Roles
| Role | Can do |
|---|---|
| preparer | create engagements, upload, run, correct extraction/links, comment |
| reviewer | everything a preparer can, plus resolve / confirm / follow-up decisions and sign-off (not on their own run) |
| admin | everything, plus manage users |

## Microsoft Foundry
Set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` (and `AZURE_OPENAI_API_KEY`, or leave it out and run
`az login` for Entra ID sign-in) in `.env`, restart, and use **Settings -> Test connection**. Install the extras:
`pip install -e ".[foundry]"` (and `".[scan]"` to OCR scanned PDFs). Reading results are cached per document, so
re-runs do not pay for the same document twice. Rate limits are retried with back-off, and files are read in parallel (4 at a time; `VRA_WORKERS` changes it). Sending client documents to a model leaves this server: get approval first.

## What is stored
`vra_data/vra.sqlite3` (users, engagements, runs, decisions, corrections, activity) and `vra_data/engagements/<id>/blobs/`
(uploaded files, named by SHA-256) and `vra_data/runs/<id>/` (that run's report, evidence manifest, audit log).
Every run keeps an exact snapshot of the files it used. **Back up the whole `vra_data` folder.**

## Security notes
* Passwords are stored as salted PBKDF2-SHA256 hashes (310k iterations). Sessions are server-side, HttpOnly,
  SameSite=Lax cookies; every state-changing request must carry an `X-VRA` header (CSRF protection). Five wrong
  passwords lock that username for 15 minutes.
* Uploads: type allow-list, size limit per file (default 100 MB, `VRA_MAX_UPLOAD_MB`), sanitised paths, zip
  traversal rejected, zip expansion capped, password-protected zips refused.
* Documents are untrusted input: shown as text/images/PDF only, never executed, and wrapped as data for any model.
* The server listens on `127.0.0.1` by default. If you use `--host 0.0.0.0`, put it behind HTTPS (a reverse
  proxy such as IIS ARR, nginx or Caddy) and set `VRA_COOKIE_SECURE=1`. Do not expose it to the internet.
* This is a single-server pilot design. Before wider rollout: Entra ID sign-in, Postgres, central backup, and an
  access review with your IT/security team.

## If Test connection fails
The red message names the likely cause and the grey line shows exactly what was tried (never the key). For a 401 or
404, click **Try other address styles**: the app tries the classic Azure OpenAI address and the OpenAI-compatible
(`/openai/v1/`) address on each host a resource answers on (`.openai.azure.com`, `.cognitiveservices.azure.com`,
`.services.ai.azure.com`) with the server-configured key and shows which one works without changing settings.
Copy the suggested address into `.env` and restart the app. Clear `AZURE_OPENAI_BASE_URL` when switching back to
a classic `AZURE_OPENAI_ENDPOINT`. A Foundry resource
(address ending `services.ai.azure.com`) often needs the `/openai/v1/` style. If none works, the key belongs to a
different resource, key sign-in is disabled (use `az login`), or the deployment name is wrong.

## Languages and scripts
Text is handled as UTF-8 everywhere, company names in any script (Arabic, Chinese, Cyrillic, ...) are matched, and
Arabic-Indic digits and separators (٠١٢٣ ٫ ٬) are read as numbers. The built-in reader recognises English labels
(Invoice No, Total, Supplier, ...); for documents laid out in Arabic or another language, use the AI reader.

## Troubleshooting
* *Port in use*: `vra-web serve --port 8010`.
* *Run failed*: the Results tab shows the reason (for example "could not find a header row" for the ledger).
* *AI options greyed out*: Foundry variables are not set on the server; see Settings.
* *"Interrupted because the server restarted"*: runs only continue while the server is up; start a new run.
