# Product contract

A person should be able to start with a question—“Can I buy this?”, “What am I
exposed to?”, “Can this fund my life?”—and get an answer grounded in their
circumstances. They should not have to learn portfolio terminology or fill out
an entire onboarding form before asking it.

The host assistant owns conversation. Wealth supplies durable client memory
and financial tools that can be inspected, corrected, and reused by any harness.
The working feature list and installation are in [README](../README.md); exact
inputs are discoverable through `wealth_context`.

## One connected client journey

1. Recall what matters about this person: goals, deadlines, obligations,
   preferences, financial state, and the reasoning behind previous decisions.
2. Ask only for missing information that changes this answer. A generic research
   question should work without creating a client or importing a portfolio.
3. Measure the relevant financial question with an explicit scope. Research an
   investment on its merits, then connect it to direct and indirect household
   exposure, available capital, liquidity, taxes, and goals.
4. Compare a small number of useful alternatives, including a simple baseline.
   Show the consequence in money, timing, risk, and trade-offs before metrics.
5. Save relevant explicit facts and corrections automatically, without a memory
   command. Respect requests not to save; keep hypothetical choices separate
   from actual facts and decisions. Corrections
   preserve history and invalidate earlier conclusions that depended on them.
6. Return later with continuity. Opt-in checks surface changed evidence, stale
   facts, approaching goals, and allocation drift; unchanged checks stay quiet.

## Depth without burden

The same `run` interface supports household exposure, historical risk, factors,
stress, constrained allocation, dated walk-forward validation, company/fund
research, valuation scenarios, protected capital, cash calendars, retirement
and withdrawal simulations, liability matching, and scoped tax-lot review.

The person sees a direct answer and the reason it matters. The harness receives
sources, dates, coverage, assumptions, missing inputs, and structured results.
The financial machinery should be available when needed, not recited every turn.

## Boundaries that affect interpretation

- Household coverage is explicit; missing liabilities or holdings are not zero.
- Risk tolerance, risk capacity, and modeled risk are separate considerations.
- Look-through identifies shared ownership; correlation measures past movement.
- Research evidence does not establish suitability for a particular household.
- Scenario and simulation results depend on supplied assumptions; they are not
  forecasts or promises. Walk-forward comparisons do not establish an edge.
- Tax logic is jurisdiction-specific: US federal taxable securities and Mexico
  Article 129 qualifying listed shares. No universal tax engine is implied.
- Accepted decisions, saved rules, active polling, and executed transactions are
  distinct. This product does not execute transactions or send external messages.

Current behavior is defined by the code, task catalog, and
[verification record](verification.md).

## Conversation and onboarding

A new client starts with an empty profile and their current question. Ask one
or two related questions only when the answer needs them. Save partial facts
and reuse them; do not force a full household questionnaire. A general research
question does not require financial disclosure. Explain once that relevant
facts are remembered automatically in the host setup, without repeating it in chat.

Metaphors and analogies are prohibited. Explain unfamiliar terms briefly where
they matter, using literal examples and explicit assumptions. Lead with the
answer and its practical consequence. Add technical depth when the decision or
the person asks for it. Avoid routine disclaimers, tool narration, and repetitive
closing questions. The shared runtime contract is `wealth/behavior.py`.
