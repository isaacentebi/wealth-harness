# The bar

Wealth ships when it replaces a private wealth manager for the person using it
and does the job better: complete across their financial life, correct to the
cent, coherent from one turn to the next, and a pleasure to use. The test is a
product people would pay for and investors would back, not a demo.

## Definition of done

**Conversation**
- Judge ≥ 4.5 overall on the eval set (2+ samples per scenario), no dimension
  below 4.2, statement flow ≥ 4.3; zero deterministic failures.
- Live sessions in Mexican Spanish and English read like one continuous
  relationship: no re-asked facts, advice honoured or revised, threads closed
  with numbers.
- Median answer latency ≤ 20 s; saving never delays an answer.

**Completeness**
- Every domain in the scope map (cash flow, reserve, debt, saving, investing,
  speculation, research, tax, retirement, insurance, estate, property,
  education, life events, income, giving, behaviour, reporting, fees, fraud,
  lifestyle) has at least its P0 capability: a calculation, a monitor rule, a
  conversation playbook or a UI surface.
- A proactive annual calendar for Mexico and the US drives check-ins.

**Correctness**
- Every financial figure comes from deterministic code with its sources,
  assumptions and missing inputs; tax parameters are dated and verified or the
  task fails closed.
- Unknown is never zero anywhere: engine, brief, UI.

**Interface**
- Chat, onboarding, You and every view pass the UX bar at 390 and 1280 px in
  both languages: every element justified, Dot contract respected, accessible,
  no overflow, no layout shift.

**Product**
- One-command install on a clean machine; clear recovery from every error;
  export and deletion; a documented privacy and security stance; no data loss
  across restarts.
- Tests green on Python 3.11 and 3.13; docs describe exactly what exists.

## The loop, after every wave

1. **Adversarial QA fan-out**, each reviewer trying to break one thing:
   backend and data integrity, quant and tax maths, UI pixels and states,
   end-to-end coherence (statement → ledger → situation → brief → answer →
   profile), security and privacy.
2. **Fix** every confirmed finding with a regression test.
3. **Measure**: eval (2 samples) and a live session in each language.
4. **Record** what improved and what is still wrong, then repeat.
