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

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest                       # 24 tests
vra demo                     # runs both workflows on seeded data -> demo_output/
```

`vra demo` writes, per workflow: `*_workpaper.md` (draft audit workpaper), `*_report.json`,
`*_evidence_manifest.json` (SHA-256 of every record) and `*_audit_log.jsonl` (hash-chained).

Your own data:

```bash
vra vouch --data my_folder          # invoices.json, purchase_orders.json, goods_receipts.json
vra reconcile --bank bank.csv --gl gl.csv --as-of 2026-03-31
vra --policy my_policy.json vouch --data my_folder    # thresholds live in config, not prompts
```
Schemas are in `src/vra/records.py` (the demo writes example JSON to `demo_output/data/`).
Bank/GL CSV columns: `id,date,amount,reference,description` (GL also needs `account`).

## Plugging in a model

The default `rule` analyst needs no API key. To use a real model:

```bash
pip install -e ".[anthropic]"   # or ".[openai]"
export ANTHROPIC_API_KEY=...    vra --analyst anthropic demo
export VRA_OPENAI_BASE_URL=https://your-endpoint/v1 VRA_MODEL=<model> OPENAI_API_KEY=...
vra --analyst openai demo       # any OpenAI-compatible endpoint, e.g. your GPT-6 Astra deployment
```
> The LLM adapters (`src/vra/analysts.py`) are written but **not exercised by the test-suite** (no network
> in CI). Try them on the demo data first; if a model's output fails validation the case is escalated and
> the rule-based baseline is used, never silently accepted.

To add another provider, subclass `_JsonAnalyst` and implement `_call(system, user) -> str`.

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
  engine/     recompute · three_way · duplicates · matching · bank_rec · aging
  analysis.py analysts.py  verifier.py  reports.py (routing)  orchestrator.py
  audit_log.py workpaper.py loaders.py sample_data.py evaluation.py cli.py
tests/        unit tests + end-to-end seeded-error recall tests + adversarial analyst tests
```

## Known limits / next steps

- **No document extraction yet.** Input is structured JSON/CSV. Next: PDF/OCR extraction stage emitting the
  same records plus field confidence and page coordinates, with validation (line sums, tax) before matching.
- The LLM currently receives a prepared case bundle; a tool-calling loop (LLM requests extra engine calls) is
  the planned next step.
- Matching is greedy by pass with subset search capped by `max_pool`/`max_group`; large many-to-many sets
  need an optimiser (e.g. OR-Tools).
- Fuzzy-duplicate and split-invoice rules are deliberately sensitive (recall first) and will need tuning
  per client; the demo data shows overlap between them (e.g. a split pair is also a same-amount pair).
- Not yet: FX, multi-currency, AR/AP sub-ledger-to-GL tie-out, fixed-asset vouching, reviewer UI and the
  feedback loop that turns overrides into vendor aliases/rules, security hardening (SSO/RBAC).
- Evaluation (`evaluation.py`) scores recall against seeded errors; extend with real anonymised engagements
  and run in shadow mode before relying on auto-clearing.

## Moving this to its own repo

This folder is self-contained:
```bash
cp -r vouching-recon-agent ~/vouching-recon-agent && cd ~/vouching-recon-agent
git init && git add . && git commit -m "Initial commit" && git branch -M main
git remote add origin git@github.com:<you>/vouching-recon-agent.git && git push -u origin main
```
