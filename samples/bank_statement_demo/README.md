# Bank statement vouching: sample evidence

A small, realistic exercise: **a cash book (the entries to vouch) tested against the bank statement that should support it.**
All names and numbers are made up.

| File | What it is |
|---|---|
| `cash_book_march_2026.xlsx` | The source of truth: 19 cash book entries for March 2026 (payments and receipts). |
| `evidence/statement_2026-03-01_to_03-15.pdf` | Bank statement part 1, a PDF (1-15 March, 10 lines). |
| `evidence/statement_2026-03-16_to_04-07.xlsx` | Bank statement part 2, an Excel file (16 March to 7 April, 7 lines). |
| `answer_key.csv` | The right answer for every entry, to score the agent on the **Performance** tab. |

## Run it in the app
1. **New engagement.** Audit period **2026-03-01 to 2026-03-31**, default transaction type **bank_payment**.
2. **Files & run.** Upload `cash_book_march_2026.xlsx` as the source of truth and the two files in `evidence/` as documents.
3. **Run vouching** (the built-in reader is enough; try the AI reader to compare).
4. **Results.** Review the 5 exceptions below. On **Performance** paste `answer_key.csv` into *Score against an answer key*.

Or from the command line: `python -m vra demo-bank --out demo_bank_output` (regenerates these files and runs the exercise).

## What is planted (and what the agent should say)

| Cash book row | Entry | Planted situation | Expected |
|---|---|---|---|
| R8 | CHQ-1043 Umbrella Trading, 4,500.00 | The statement shows 4,050.00: two digits transposed when the cheque was booked | **Amount differs** (450.00) |
| R9 | TRF-2204 "Initech Office Co", 2,300.00 | The statement shows the money went to **Hooli Services** | **Wrong counterparty** |
| R11 | CHQ-1044 Stark Industries, 1,980.00 (second time) | The same payment entered twice; the bank paid it once | **Document reused** |
| R19 | CHQ-1047 Tyrell Corp, 1,100.00 | Cheque written 28 March, not on the statement: not yet presented | **No document** (an outstanding cheque) |
| R20 | RCT-3330 Stark Industries, 3,000.00 | Booked on 31 March, but the bank credited it on **2 April** | **Cut-off** |
| R12-R14 | Three receipts: 1,500 + 1,800 + 1,200 | The bank shows one **batched deposit** of 4,500.00 on 17 March | **Clean**: matched as a batch, not flagged |
| (statement) | Bank charges 25.00 and interest 12.34 | On the statement but **not in the cash book** | Listed under *documents not linked to any entry* |

Everything else (11 entries) is clean. A good result is **5 of 5 exceptions found, no false alarms**, and the two unrecorded statement lines listed.

## Tips
* The agent reads each statement **line** as its own piece of evidence (date, description, reference, amount) and ignores the running balance.
* Statement dates are allowed to differ from the book date by a few days (cheques take time to clear); the cut-off test uses the audit period.
* Try changing something yourself in the cash book (an amount, a name, a date), re-run, and see what it catches.
