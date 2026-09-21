# Live review, 2026-09-21

One Spanish session as a Mexico City resident: rough situation, GBM statement
upload, confirmation, then "S&P 500 via SIC or Interactive Brokers?". Model
gpt-5.6-sol, reasoning low. Judged as a paying private-banking client.

## Verdict

Each turn is individually competent: natural Spanish, correct arithmetic, one
question, sources. The conversation as a whole is not. There is no connecting
tissue: no running picture of the person, no plan that later answers must
respect, and the web profile cannot read what the conversation saved.

## Findings

### The conversation has no memory of its own advice
1. Turn 2 advised paying off the 13% car loan from the monthly surplus. Turn 4
   planned MXN 10,000/month into equities without reconciling the two. A banker
   splits the surplus: reserve, then the loan, then investing, with dates.
2. The loan was never sized: no payment, remaining term or payoff date was
   asked for or computed. "Pay it soon" is not a plan.
3. It asked whether the person is a Mexican tax resident after they said they
   live in CDMX. State the assumption and proceed; ask only if it changes the
   answer materially.

### Statements are reconciled, not understood
4. The upload summary confirmed totals and asked to save. It did not compare
   the statement (217,837 MXN + US$1,000) with what the person had said
   ("unos 200 mil en GBM"), and it said nothing about the contents: a UDIBONO
   worth 40% of the account, IVV and CSPX duplicating the same index.
5. After confirmation it said "Listo" and stopped. The moment new data lands is
   the moment to show the updated whole picture in one or two lines.
6. The "Remembered" line does not appear for statement saves (only for
   wealth_remember writes).

### The decisive Mexico point was missing
7. It noticed IVV and CSPX overlap but did not say which to keep buying. For a
   Mexican resident CSPX (Irish UCITS) avoids US estate-tax exposure above
   US$60k and has lower dividend withholding; IVV does not. This is the single
   most valuable sentence in the answer and it was absent.
8. Source links pile up at paragraph ends as run-on text
   ("Comisiones de GBM Artículo 129 de la LISR").

### The profile cannot read the conversation's memory
9. The agent saved correct facts in its own shapes (`plan.resources.cash[]`,
   `plan.resources.debts[]`, `income.schedule.items[]`, `account.<id>`), while
   the profile reads other field names. Result: no net worth, empty rows
   ("Income schedule", "Spending"), "Missing: spending, savings, investments,
   debts" after all were given, a raw goal id (`sp500_monthly_investing`) and
   raw account ids (`gbm-4321`).
10. Monthly spending (45k) was stated but does not appear anywhere.

## Root cause

There is no canonical personal balance-sheet model. Memory is free-form JSON
per key, so the agent, the planning tasks, the ledger and the profile each
interpret it differently, and nothing turns the saved facts into one current
picture that every turn starts from.

## Required changes (wave 2, first)

1. **Canonical household model**: typed sections (income, spending, cash and
   reserve, accounts and holdings from the ledger, debts with rate/payment/term,
   goals with target/date/priority, residence and tax context, preferences).
   `remember` validates against it; the profile, planning tasks and agent all
   read it; ledger accounts roll into it automatically.
2. **Situation brief every turn**: a compact, deterministic summary built from
   that model (net worth, monthly surplus and where it is committed, reserve in
   months, debts with cost, open goals, open decisions, unresolved questions,
   what changed since last turn) injected into the turn prompt, so each answer
   is situated in the whole picture.
3. **Open threads**: advice given and questions pending are recorded (as
   lightweight decisions/notes) and must be reconciled by later answers.
4. **Statement insight step**: after upload, compare with what the person said,
   name the one or two findings that matter, and after confirmation show the
   updated picture.
5. **Mexico investor defaults in the prompt and research**: estate-tax situs,
   UCITS vs US-domiciled, withholding, SIC premium and lot size are always
   considered for a Mexican resident buying US exposure.
6. **Citations render as quiet superscript source marks**, not run-on text.
7. **Memory line for every write path** (remember, ingest confirm, decisions).

## Re-run after wave 2 (same script, same model)

Fixed:
- States assumptions (pesos, Mexican tax residence) instead of asking; asks the
  sharper question (rate and whether the car payment is inside spending).
- Monthly spending is saved; memory line is natural Spanish.
- Statement upload compares with the stated estimate (236,087 vs 200,000), names
  the UDIBONO at 39% and the CSPX/IVV duplication, and asks before replacing.
- After confirmation it states the new net worth (290,000 → 326,087) and why.
- "Guardado: cuentas de GBM".

Still wrong:
- The car-loan thread is not closed: given rate and payment (13%, 4,500/month)
  it never said when the loan ends (about 15 months) or how that frees cash.
  The brief computes payoff; the prompt must require closing a thread when the
  missing input arrives.
- Latency: first turn about 60 s, statement turn about 50 s at low reasoning.
  Needs profiling (tool-call count per turn, prompt size).
- The error for a missing client says "Local memory couldn't be read…"; a
  vanished profile deserves its own message. (The trigger here was a test
  harness overwriting the live database, not the product.)
