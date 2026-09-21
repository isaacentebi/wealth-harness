# Wealth capability: product specification

## Promise

This product gives any capable agent the memory, financial arithmetic, and evidence discipline of a thoughtful wealth adviser. A person can begin with whatever is on their mind—an investment idea, a company, a statement, a life goal, an income need, or a tax question—and receive an answer grounded in the whole household rather than the latest ticker.

It does not promise alpha, predict prices, issue orders, or impersonate a regulated adviser. It helps a person understand what they own, what life requires, how choices fit together, and what evidence would change a decision. It should feel direct, calm, warm, and financially literate.

The product is a portable capability. A host agent owns conversation and research; this package owns client state, evidence, calculations, and decision freshness. [The implementation contract](implementation-contract.md) defines the working foundation. Everything beyond it here is roadmap, not current behavior.

## Product principles

1. **Start anywhere.** There is no mandatory intake. Ask one question only when its answer could materially change the next judgment.
2. **Know the household.** Include people, income, businesses, property, liabilities, currencies, goals, accounts, and constraints. A household is not necessarily one tax unit; ownership and jurisdiction remain explicit.
3. **Treat goals as dated cash flows.** “Buy a home” becomes an amount, currency, date or range, priority, and funding source. Uncertain values stay uncertain.
4. **Separate facts, evidence, inference, and decisions.** Inference never silently becomes fact. Unknown never means zero. Every material financial fact has a source, observation date, freshness boundary, and revision history.
5. **Engines do arithmetic; agents do judgment.** Numbers come from deterministic tools or cited sources. The agent explains their meaning and unknowns.
6. **Diagnose before optimizing.** Establish a stable, understandable baseline first. Optimization is optional, sensitive to assumptions, and never presented as a uniquely correct portfolio.
7. **Acceptance is not execution.** The system can record an accepted plan or decision. It does not place trades, move money, or imply that an action occurred.

## Six start-anywhere journeys

These dialogues are hypothetical interaction examples, not live financial evidence.

### 1. A view of the world

> **Client:** I want more exposure to AI infrastructure.
> **Agent:** We can express that. You already own several funds with the same large technology companies, so I want to measure what this adds rather than count new tickers as new diversification. Is this money needed for any goal in the next few years?

The agent protects dated cash needs, analyzes holdings, uses verified look-through where available, and compares a few alternatives. It records the view, disconfirming condition, size, and largest risk.

### 2. A returning client revises a choice

> **Client:** I’m thinking of doubling the memory sleeve.
> **Agent:** Since we last sized it, your home goal moved forward and the account values changed. The old proposal is stale, so I would refresh those two facts before treating it as current. Is the purchase still planned for next spring?

The client does not repeat stable information. The agent refreshes only material inputs, refuses acceptance on stale evidence, and shows the revision’s effect on goals and household exposure. The new decision preserves the earlier rationale.

### 3. Company research in context

> **Client:** Is this still a good business after the earnings release?
> **Agent:** The company case needs fresh source work. I also remember that it is already a meaningful exposure through your employer shares and two funds. I’ll separate what changed in the business from what another dollar would change in your household.

Research tests economics, expectations, valuation inputs, risks, and disconfirming evidence, then returns a dated, cited case. Household policy asks whether the exposure belongs in this client’s life. Portfolio construction compares alternatives. These are connected stages, not one “stock score.”

### 4. A household goal

> **Client:** Can we afford a home in three years?
> **Agent:** I can map that as a dated cash need beside your reserve, debt, and investable capital. I know the target amount, but your current outside savings are stale. What is that balance now?

The system represents the goal in its currency and date range, identifies responsible household members and tax units, and shows funded, protected, and uncommitted resources. It exposes missing liabilities and double-counted funds rather than making a hard-date goal appear funded.

### 5. Income and withdrawals

> **Client:** I need the portfolio to pay me every month. Should I use dividend stocks?
> **Agent:** The goal is dependable spending, not the highest yield. Let’s compare dividends, interest, and planned sales after costs and tax, then see how the plan behaves if poor returns arrive early. Which account is meant to fund the spending?

Income planning begins with dated spending and other income. It models ownership, tax treatment, distributions, withdrawals, inflation, fees, and sequence risk. It compares after-tax, after-cost scenarios without treating dividends as free return. Sustainability claims require an explicit method and sensitivity analysis; the foundation only computes stated monthly gaps.

### 6. Tax-aware review

> **Client:** Are there losses worth harvesting this year?
> **Agent:** I can screen once the relevant tax unit, jurisdiction, account types, cost lots, realized gains, and recent related-account trades are complete. I have positions but not verified lots, so I cannot estimate savings yet.

Tax is a jurisdiction-specific module, never a universal rule layer. It consumes account and lot records: owner, tax unit, jurisdiction, account type, security, quantity, basis, acquisition date, currency, and dispositions. It can produce review candidates, constraints, conflicts, assumptions, and scenario estimates, but no orders or savings claims on incomplete evidence.

## Durable client, evidence, and decisions

SQLite is the authoritative store now. It provides transactional revisions, client isolation, idempotent writes, and history. It is local and unencrypted in the foundation; hosts must state where it lives, what context reaches a model provider, and how export and deletion work. Encryption can be added without replacing the logical model.

The canonical model grows from three linked records:

- **Client and household:** people, relationships, tax units, residences, jurisdictions, currencies, preferences, and constraints.
- **Financial state:** dated goals and cash flows; income, spending, entities, assets, liabilities, accounts, positions, and lots. Ownership is explicit. A snapshot names its scope and completeness; “complete brokerage account” does not mean “complete household.”
- **Evidence and decisions:** facts carry provenance, confidence, observation date, expiry, and revision. Proposals cite evidence and alternatives. Changed revisions, expired evidence, or required inference block acceptance. Later changes mark decisions for review rather than rewriting history.

The context compiler selects the smallest eligible packet for the intent, including gaps and contradictions. Source text is untrusted data, never instructions. The implementation contract supports the first facts and decision lifecycle; richer household, lot, and research schemas are roadmap.

## Policy, research, and construction

**Research** establishes what an asset, fund, company, or thesis is and what the evidence says. It owns source quality, dates, conflicts, and case updates. It does not decide household suitability.

**Household policy** translates lives into constraints: reserves, dated goals, currencies, liabilities, income risk, tax units, liquidity, concentration tolerance, and accepted views. It decides which capital is available and which risks matter.

**Portfolio construction** compares ways to express that policy. It must distinguish ticker labels, issuer concentration, verified economic look-through, and observed correlation. Correlation can reveal shared movement but cannot substitute for holdings look-through; look-through can reveal shared holdings but cannot establish future co-movement.

Allocations state whether they equalize dollars, volatility, or modeled risk contribution. Equal dollars are not equal risk. Use a transparent, low-cost baseline a person can explain, and compare changes under the same window, currency, rebalancing, fees, and tax assumptions.

Retain `tools/wm.py`: ingestion, historical diagnosis, factors, regimes, construction, comparison, capital reservation, drift, fees, and cards. Future methods may add shrunk covariance, HRP, and CVaR. Black–Litterman requires explicit recorded views and visible input mapping. Optimizers must pass walk-forward, perturbation, and sensitivity evaluation, disclose unstable weights, and fail back to the baseline. A sophisticated solver with fragile inputs is optimization theater.

## Portable front door and adaptive conversation

The same core ships as a JSON CLI and local MCP stdio server. Installation requires a Python package, writable database location, and no model key. Adapters may render cards; JSON remains authoritative.

The response contract adapts to the moment:

- A simple factual question gets a direct sentence.
- A material unknown gets one precise question.
- A decision gets the recommendation shape, the reason, the largest remaining risk, and one useful comparison.
- Detail, sources, assumptions, and calculations remain available on request or in an artifact.

Warmth comes from remembering the person and explaining trade-offs plainly, not from canned empathy or a forced turn template. The agent should never read a report aloud, dump every metric, or end every response with a question.

## Monitoring

Monitoring is future and opt-in; the foundation has no active scheduler. Later triggers may cover an underfunded goal, allocation drift, material thesis evidence, or expired facts. Price movement alone is rarely enough. Monitors state scope, cadence, source, and notification rule; silence is the default. Notifications invite review and never imply a trade.

## Open-source policy

- **Retain the existing analytics engine.** Replace a calculation only when the new path has verified parity and improves maintainability or capability.
- **SQLite is authoritative now; MCP integration is part of the foundation now.** Neither is a speculative dependency.
- **skfolio is a candidate, not an adopted runtime.** Evaluate it behind an adapter for calculation parity, constraints, reproducibility, dependency weight, and licensing before adopting selected methods.
- **Graphiti, Mem0, and Letta are design references for temporal memory.** They may later provide an optional derived retrieval index. None should become the source of truth or a runtime dependency now.
- **OpenBB is an optional licensed data adapter.** Add it only when a deployment has reviewed provider terms, field provenance, redistribution, and failure behavior. The product must still expose source and freshness rather than hiding them behind an aggregation layer.

## Evaluation gates

1. **State integrity:** client isolation, revision conflicts, idempotency, export/delete boundaries, and deterministic replay.
2. **Evidence integrity:** no inference-to-fact promotion, no silent timeless financial facts, unknown preserved, stale inputs excluded, and accepted decisions blocked when evidence is stale or inferred.
3. **Financial correctness:** currencies, ownership, tax units, lots, goal cash flows, costs, and assumptions reconcile. Reference calculations cover each new method.
4. **Portfolio honesty:** complete scope is named; ticker concentration, look-through exposure, correlation, and modeled risk are not conflated. Stable baseline precedes optimization.
5. **Model robustness:** parity before library adoption; walk-forward and sensitivity tests for advanced construction; no claimed advantage from in-sample fit alone.
6. **Conversation quality:** users start anywhere, repeat no known fact, inspect evidence, and correct memory. Direct questions receive direct answers.
7. **Operational truth:** distinguish proposed, saved, accepted, scheduled, executed, and verified states. The core never claims deployment, monitoring, advice, or execution it did not perform.

## Build sequence

1. Complete and stabilize the contracted SQLite store, context workflows, JSON CLI, and MCP server.
2. Connect the existing analytics engine through a typed adapter with shared provenance, currency, scope, and freshness rules.
3. Ship the first vertical slice: a returning client asks to revise an exposure; the agent loads the prior decision, identifies changed or stale facts, refreshes only those inputs, compares current and revised choices against the stable baseline, and saves a new evidence-bound proposal. Acceptance remains separate and execution absent.
4. Expand the household model to people, tax units, accounts, liabilities, income exposures, fund look-through, and lots; add imports with explicit completeness and reconciliation.
5. Add source-grounded company and fund research packets linked to theses and household exposure.
6. Add income and withdrawal scenarios with sequence risk, then jurisdiction-specific tax modules behind explicit capability and data-completeness gates.
7. Add opt-in monitoring and evaluate advanced construction methods and optional OSS adapters only after the earlier gates pass.

The first vertical slice is successful when the second conversation is materially better because the product remembers the client, can name exactly what changed, refuses stale evidence, and preserves the reasons behind both the old and revised decisions.
