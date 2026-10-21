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
re-runs do not pay for the same document twice. Sending client documents to a model leaves this server: get approval first.

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

## Troubleshooting
* *Port in use*: `vra-web serve --port 8010`.
* *Run failed*: the Results tab shows the reason (for example "could not find a header row" for the ledger).
* *AI options greyed out*: Foundry variables are not set on the server; see Settings.
* *"Interrupted because the server restarted"*: runs only continue while the server is up; start a new run.
