# Onboarding

Onboarding is deterministic. A small state machine asks the questions, captures
typed answers and writes canonical facts directly. The model is not needed to
collect a birth year or an income; it is used where judgment adds value: reading
a free-text answer that does not fit the card, and writing the first synthesis.

## Goals

- A complete, correct picture in about two minutes.
- Value before the end: every answer already updates a visible picture.
- Statements are the fast path; typing is the fallback.
- Resumable, skippable, and never asked twice.
- Identical logic in the web chat and in text channels (WhatsApp, iMessage).

## Shape

Each step is a card inside the conversation, not a separate form page. One
question per card, sentence case, value first. Answers are chips, a single amount
field with currency, or an upload. Every card has "Skip" and accepts free text;
free text is parsed by the model into the step's schema and shown back for a
one-tap correction.

## Steps

Order is by value and trust, and steps are conditional on earlier answers.

| # | Question (en / es) | Input | Writes | Condition |
|---|---|---|---|---|
| 1 | What should I call you, and where do you live? / ¿Cómo te llamo y dónde vives? | name, country (+ state) | `client.profile.name`, `residence` | always |
| 2 | What year were you born? Anyone who depends on you? / ¿En qué año naciste? ¿Alguien depende de ti? | year, count | `birth_year`, `dependents` | always |
| 3 | What do you take home each month? / ¿Cuánto recibes al mes, neto? | amount + currency; chips: aguinaldo, bonus, PTU, rent, other | `income.*` | always |
| 4 | What do you spend in a typical month? / ¿Cuánto gastas en un mes normal? | amount, or "Not sure, I'll upload a bank statement" | `spending.monthly` | always |
| 5 | Where is your money today? / ¿Dónde tienes tu dinero hoy? | multi-select chips; per selection: amount or upload | `cash.*`, `account.*`, ledger | chips vary by country (MX: Nu/bank, CETES, GBM/casa de bolsa, AFORE, PPR, US broker; US: bank, brokerage, 401(k)/IRA, HSA) |
| 6 | Do you owe anything? / ¿Debes algo? | chips; per selection: balance, rate, payment | `liability.*` | skip on "No" |
| 7 | What matters most right now? / ¿Qué es lo más importante ahora? | one or two chips: emergency fund, pay off debt, home down payment, retirement, grow wealth, education; optional amount and date | `goals` | always |
| 8 | If your investments fell 20% in a month, you would… / Si tus inversiones cayeran 20% en un mes… | sell / hold / buy more; experience: none / some / experienced | `preference.risk` | always |
| 9 | Add statements for an exact picture / Agrega estados de cuenta | upload per institution selected in step 5 | ingest → ledger | offered, never required |

## Reveal

When the last step is done or skipped, the conversation shows **your picture**:
net worth (liquid and illiquid), monthly surplus, reserve in months, debts with
payoff dates, and the one or two findings that matter most, computed
deterministically from the situation model. The model then writes one short
paragraph and proposes a single next step. This is the first moment the person
should feel understood.

## Rules

- Never ask what a statement already answered; prefill and confirm instead.
- Unknown stays unknown. "Not sure" is a valid answer and is recorded as such.
- Tax residence is asked, never inferred, and only when residence alone is
  ambiguous (for example a US citizen living in Mexico).
- Nothing identifying beyond a first name is collected: no RFC, CURP, SSN,
  account numbers or address.
- Returning users see "Continue setup" only if steps remain, and later turns ask
  for the next missing item only when it matters to the question at hand.

## Implementation

- `wealth/onboarding.py`: step definitions (id, prompt in en/es, input schema,
  condition, writer), `next_step(situation)`, `apply(step, answer)`. Pure and
  tested; writes via `remember` with source `user` and confidence `confirmed`.
- Web: `GET /api/onboarding` returns the next card; `POST /api/onboarding` with
  an answer writes it and returns the next card and the updated picture.
- Chat renders cards inline in Dot style; text channels render the same step as
  a numbered text question.
- The agent sees onboarding progress in the situation brief and does not repeat
  answered questions.
