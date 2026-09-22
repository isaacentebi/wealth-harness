# Wealth: full scope of a world-class household wealth manager (MX + US)

Researched 2026-09-21. The spine is the CFP Board's 7-step process: understand circumstances, identify goals, analyse, develop, present, implement, then monitor and update [1]. Private banks and family offices add trust/estate, philanthropy, family stewardship, lending, consolidated reporting and outsourced admin [2]. Wealth's edge: the monitoring loop costs it almost nothing, so it can run continuously instead of quarterly.

**Quantified value, in brief.**
- **Vanguard Advisor's Alpha: about 3%/yr net** [3]. The parts:
  - behavioural coaching, about 150 bp
  - spending/withdrawal order, 0–110 bp
  - asset location, 0–75 bp
  - cost-effective implementation, 34 bp
  - rebalancing, 26 bp
- **Morningstar Gamma: +22.6% certainty-equivalent retirement income, about 1.59%/yr alpha-equivalent** [4]. Dynamic withdrawal is the largest component.
- **Tax-loss harvesting: 1.08%/yr before the wash-sale rule, 0.82% after, about 0.95% after costs** [5].
- **Behaviour gap: 1.2 pp/yr** [6]. This is disputed; one FAJ paper puts the timing cost at about 0.10% [7].

Use these as ranges, not promises.

**Legend.**
- **Form:**
  - **CALC** = deterministic calculation task
  - **MON** = monitor rule
  - **PLAY** = conversation playbook
  - **UI** = UI surface
- **"Have"** = already in Wealth.
- **Refer-out** means a licensed human (CPA/contador, abogado/notario, insurance broker, actuary) signs off.

---

## 1. Domains

### 1.1 Cash flow and budgeting — P0
- **(a) What a top advisor delivers.**
  - Net-worth and cash-flow statement at onboarding, then a monthly review.
  - Surplus allocation ("where the next peso or dollar goes").
- **(b) Data.** Ledger (have), income cadence (quincena, biweekly), fixed versus variable costs, household members.
- **(c) Split.**
  - Deterministic: categorisation rules, surplus, savings rate, runway.
  - LLM: labelling ambiguous merchants, and narrating trade-offs.
  - Refer out: nothing.
- **(e) Form.** CALC + MON (surplus drift) + UI (monthly "money map").

### 1.2 Emergency reserve — P0
- **(a) What a top advisor delivers.**
  - Set a target of 3–6 months of essential spending, adjusted for income volatility and single versus dual earners.
  - Choose where to park it: US HYSA/T-bills; MX Cetes directo or pagarés from IPAB-insured banks.
- **(b) Data.** Essential spend, income stability, liquid balances, insurance deductibles.
- **(c) Split.** Deterministic: target and gap. LLM: setting the target when self-employed or earning in USD. Refer out: nothing.
- **(d) Value.** Vanguard (2025): $2,000 of emergency savings raised financial well-being by 21%, against 12% for a $500k income [8]. It also prevents 401(k) leakage [8].
- **(e) Form.** CALC + MON (reserve < target; reserve > 12 months = drag).

### 1.3 Debt and credit — P0
- **(a) What a top advisor delivers.**
  - Payoff order (avalanche versus snowball) and refinancing checks.
  - Credit-report hygiene, run annually or before any major loan.
  - US: FICO and the free weekly reports on AnnualCreditReport.
  - MX: the free *Reporte de Crédito Especial* every 12 months from each of Buró de Crédito and Círculo de Crédito [9].
  - MX: compare offers by CAT (Banxico's total annual cost), not by the nominal rate.
- **(b) Data.** Balances, APR/CAT, minimum payments, promo expiries, utilisation, report dates.
- **(c) Split.**
  - Deterministic: payoff schedules, utilisation, promo-expiry alerts.
  - LLM: disputes, and the behavioural choice of snowball.
  - Refer out: insolvency and debt settlement (US bankruptcy attorney; MX CONDUSEF complaints).
- **(d) Value.** Guaranteed "return" equal to the APR avoided.
- **(e) Form.** CALC + MON + PLAY (credit repair).

### 1.4 Saving systems — P0
- **(a) What a top advisor delivers.**
  - Pay-yourself-first automation and sinking funds.
  - Windfall rules for aguinaldo, PTU and bonuses.
  - Save-more-tomorrow step-ups.
- **(b) Data.** Pay dates, goals, accounts.
- **(c) Split.** Deterministic: sweep amounts. LLM: framing and commitment devices. Refer out: nothing.
- **(d) Value.** Automatic escalation is one of the most robust effects in behavioural finance (Thaler–Benartzi SMarT).
- **(e) Form.** CALC + MON (windfall arrives) + PLAY.

### 1.5 Investing — IPS, allocation, implementation, costs, rebalancing, location — P0
- **(a) What a top advisor delivers.** A written IPS per the CFA Institute's *Elements of an IPS for Individual Investors* [10]:
  - objectives
  - risk ability versus willingness
  - time horizon, liquidity, tax, legal and unique constraints
  - allocation bands
  - rebalancing rules
  - review cadence
  
  Also: annual IPS review, a quarterly drift check, threshold rebalancing, and household-level asset location.
- **(b) Data.** Holdings and lots (have), account tax types, goals, horizon, risk questionnaire.
- **(c) Split.**
  - Deterministic: drift, rebalance trades, location optimiser, fee drag, DCA.
  - LLM: eliciting risk willingness, drafting IPS prose, handling client exceptions.
  - Refer out: discretionary execution unless Wealth or a partner is registered (US RIA; MX *asesor en inversiones* registered with CNBV).
- **(d) Value.** Rebalancing 26 bp, location 0–75 bp, costs 34 bp [3].
- **(e) Form.** CALC + MON + UI (IPS document, drift gauge). **In progress; finish first.**

### 1.6 Speculation / play money — P1
- **(a) What a top advisor delivers.**
  - A capped "satellite" sleeve (for example ≤5–10% of liquid net worth).
  - Loss limits, a sandbox (paper trading), crypto custody education.
  - Options education before approval.
  - Cool-off delays on impulsive trades.
  - A post-mortem journal.
- **(b) Data.** Sleeve balance, trade log, realised P&L, leverage.
- **(c) Split.**
  - Deterministic: cap breaches, drawdown stop, cool-off timer, leverage flag.
  - LLM: reflective prompts, and spotting FOMO language.
  - Refer out: nothing, but never give specific options or crypto calls.
- **(d) Value (loss avoidance).**
  - ESMA found 74–89% of retail CFD accounts lose money [11].
  - Brazilian day-trader studies (Chague et al.) find about 97% of persistent traders lose.
  - Morningstar: "the more investors traded, the less they made" [6].
  - Emulate ESMA's leverage caps and loss warnings, and FINRA options-approval tiers.
- **(e) Form.** MON + PLAY + UI (sandbox, cool-off modal).

### 1.7 Research — P1 (have core)
- **(a) What a top advisor delivers.**
  - Due diligence on funds, stocks, private deals and real estate, run on demand.
  - Fund due diligence covers cost, tracking and tax wrapper.
  - MX-specific: SIC listing, and whether a fund is PFIC-exposed for US persons.
- **(b) Data.** Tickers, prospectus/KID, deal memos.
- **(c) Split.**
  - Deterministic: fees, overlap, factor exposure.
  - LLM: qualitative thesis and red flags.
  - Refer out: legal review of private-placement documents.
- **(e) Form.** CALC + PLAY.

### 1.8 Tax planning — P0
- **(a) What a top advisor delivers.** An annual calendar (§2) and a multi-year bracket plan.
  - **US:**
    - Roth conversions in low-income years.
    - Gain and loss harvesting, and 0% LTCG gain harvesting.
    - Bunching and donor-advised funds.
    - Withholding and safe-harbour estimates.
  - **MX** (all under Art. 151 LISR [12]):
    - Personal deductions capped at the lesser of 5 annual UMA or 15% of income.
    - PPR/AFORE voluntary contributions capped separately at 10% of income, up to 5 UMA [13].
    - Colegiaturas outside the cap.
    - Constancias, CFDI capture, and the declaración anual in April.
- **(b) Data.** Prior returns, pay stubs, CFDI XMLs, broker 1099s / constancias de retenciones, residency.
- **(c) Split.**
  - Deterministic: projections, bracket headroom, deduction caps, CFDI completeness.
  - LLM: explaining trade-offs, and reading unusual documents.
  - Refer out: filing, and anything with dual residency, a treaty position or an entity (CPA / contador público).
- **(d) Value.** TLH 0.82–1.08% [5]; location and withdrawal order up to 185 bp combined [3].
- **(e) Form.** CALC + MON + UI (tax-year dashboard).

### 1.9 Retirement — P0
- **(a) What a top advisor delivers.**
  - **US:**
    - Contribution maximisation. 2026 limits: 401(k) $24,500 (+$8,000; +$11,250 at ages 60–63), IRA $7,500, HSA $4,400/$8,750 [14].
    - Mandatory Roth catch-up above $150k of FICA wages [14].
    - Social Security timing: each year of delay from FRA to 70 adds 8%, about 24% in total [15].
    - RMDs, and a decumulation order.
  - **MX:**
    - Determine Ley 73 versus Ley 97 by first IMSS registration before or after 1 July 1997.
    - Ley 97 minimum weeks: 875 in 2026, rising to 1,000 by 2031.
    - Ley 73 Modalidad 40 ROI analysis.
    - AFORE fee and performance check, and voluntary contributions [16].
- **(b) Data.** Age, salary history / semanas cotizadas (IMSS statement), AFORE statement, SSA earnings record, balances.
- **(c) Split.**
  - Deterministic: projections, Monte Carlo (have), claim-age IRR, Mod 40 break-even, RMDs.
  - LLM: framing longevity and spousal trade-offs.
  - Refer out: IMSS pension filing (actuary or gestor), annuity purchase.
- **(d) Value.** Gamma: about 1.59%/yr [4]; spending strategy 0–110 bp [3].
- **(e) Form.** CALC + MON + UI (retirement readiness).

### 1.10 Insurance — P1
- **(a) What a top advisor delivers.** An annual needs analysis covering:
  - life (DIME or human-capital method)
  - disability
  - MX gastos médicos mayores (premiums are deductible under Art. 151)
  - property and auto
  - umbrella (US)
  
  Also a coverage-gap map and renewal reminders.
- **(b) Data.** Policies (declarations pages), dependants, income, assets, deductibles.
- **(c) Split.** Deterministic: needs gap, renewal dates. LLM: reading policy PDFs, exclusions. Refer out: purchase through a licensed agent.
- **(d) Value.** Tail-risk protection; no bp figure. Gamma treats protection as part of total-wealth allocation [4].
- **(e) Form.** CALC + MON + PLAY.

### 1.11 Estate — P1 (have US situs)
- **(a) What a top advisor delivers.**
  - Document inventory reviewed every 3–5 years and at every life event:
    - MX: testamento, beneficiarios on AFORE, bank and insurance, poder notarial, voluntad anticipada.
    - US: will, revocable trust, POA, healthcare proxy.
  - Marital regime: sociedad conyugal versus separación de bienes.
  - Cross-border: US situs for non-residents, which Wealth already has.
  - MX campaign: *Septiembre, Mes del Testamento*, with notary fees up to 50% off [17].
- **(b) Data.** Marital regime, children, document dates, beneficiary designations, asset situs.
- **(c) Split.**
  - Deterministic: missing-document checklist, beneficiary mismatch, situs exposure.
  - LLM: explaining intestacy consequences.
  - Refer out: drafting (notario / estate attorney).
- **(d) Value.** Intestate succession in MX can cost MXN 30k–100k and take years [17].
- **(e) Form.** MON + PLAY + UI (estate vault).

### 1.12 Real estate and mortgage — P1
- **(a) What a top advisor delivers.**
  - Rent-versus-buy analysis and affordability.
  - MX: Infonavit/Fovissste points, Unamos Créditos, cofinanciamiento.
  - Refinance triggers, prepay-versus-invest, and deductibility of real interest on the mortgage (MX Art. 151 IV).
- **(b) Data.** Loan terms, property value, Infonavit subcuenta balance.
- **(c) Split.** Deterministic: amortisation, break-evens. LLM: lifestyle trade-offs. Refer out: appraisal, notary, lender.
- **(e) Form.** CALC + PLAY.

### 1.13 Education funding — P2
- **(a) What a top advisor delivers.**
  - US: 529 plans (including the 529-to-Roth rollover), with state-deduction checks.
  - MX: education-savings instruments.
  - The colegiaturas deduction, with 2025 caps from MXN 12,900 to MXN 24,500 by level [12].
- **(b) Data.** Children's ages, target schools, balances.
- **(c) Split.** Deterministic: cost projection, funding gap. LLM: school and country choices. Refer out: nothing.
- **(e) Form.** CALC + MON.

### 1.14 Life events — P0 as a routing layer
- **(a) What a top advisor delivers.** A checklist per event:
  - marriage (régimen)
  - birth
  - divorce
  - job change (401k rollover; MX finiquito/liquidación tax)
  - move across the border (residency change)
  - inheritance
  - death of a spouse
- **(b) Data.** Event detection from the ledger and chat (memory, have).
- **(c) Split.** LLM detects the event; deterministic checklists follow; refer out legal and tax.
- **(e) Form.** PLAY + MON.

### 1.15 Career income and equity compensation — P1
- **(a) What a top advisor delivers.**
  - RSU vest-and-sell rules.
  - ISO/NSO exercise and AMT modelling.
  - The 83(b) election, which must be filed within 30 days.
  - ESPP, and concentration limits.
  - Business owners: entity choice, reasonable salary, SEP/Solo 401(k); MX RESICO versus actividad empresarial.
- **(b) Data.** Grant agreements, vest schedules, 409A/FMV, company.
- **(c) Split.**
  - Deterministic: vest calendar, concentration %, AMT, the 30-day 83(b) timer.
  - LLM: reading grant documents.
  - Refer out: 83(b) filing and entity setup (CPA/attorney).
- **(e) Form.** CALC + MON.

### 1.16 Charitable giving — P2
- **(a) What a top advisor delivers.**
  - Appreciated-stock gifts, donor-advised funds, bunching, QCDs from age 70½.
  - MX donatarias autorizadas, capped at 7% of the prior year's income.
- **(b) Data.** Giving intent, lots, age.
- **(c) Split.** Deterministic: best lot to gift, bunching savings. Refer out: large or private-foundation gifts.
- **(e) Form.** CALC + PLAY.

### 1.17 Behavioural coaching — P0
- **(a) What a top advisor delivers.**
  - "Circuit-breaker" outreach in drawdowns [3].
  - Pre-commitment in the IPS, and bias diagnosis (Pompian / CFA behavioural taxonomy).
- **(b) Data.** Market state, the client's portfolio moves, chat sentiment.
- **(c) Split.**
  - Deterministic: trigger (drawdown and a panic-sell attempt).
  - LLM: the conversation itself, which is where an LLM wins on availability.
  - Refer out: gambling and compulsive spending, to mental-health resources.
- **(d) Value.** About 150 bp, and up to 200 bp [3]; the behaviour gap is 1.2 pp [6].
- **(e) Form.** PLAY + MON.

### 1.18 Quarterly reporting — P0
- **(a) What a top advisor delivers.** A quarterly consolidated report:
  - performance by TWR and XIRR (have)
  - progress towards goals
  - realised and unrealised tax
  - fees paid
  - actions taken and next actions
- **(b) Data.** All of the above.
- **(c) Split.** Deterministic numbers; LLM narrative.
- **(e) Form.** UI + CALC.

### 1.19 Fee audit — P0
- **(a) What a top advisor delivers.**
  - An annual all-in cost audit covering expense ratios, advisory wraps, AFORE comisión, bank fees, FX spreads and card fees.
  - Switch recommendations.
- **(b) Data.** Statements (have), fund TERs.
- **(c) Split.** Deterministic. LLM finds fees buried in PDFs.
- **(d) Value.** 34 bp for implementation cost alone [3].
- **(e) Form.** CALC + UI.

### 1.20 Fraud and scam protection — P0
- **(a) What a top advisor delivers.**
  - Anomaly alerts.
  - Scam-pattern checks before a transfer.
  - Credit freeze (US).
  - MX: REUS (the do-not-call registry) and CONDUSEF's SIPRES registry of authorised institutions.
  - A trusted-contact person.
- **(b) Data.** Ledger, new payees, counterparties the user mentions in chat.
- **(c) Split.**
  - Deterministic: new-payee spikes, unregistered-entity lookups.
  - LLM: recognising scam scripts ("guaranteed returns", "bank impersonator").
  - Refer out: police, the bank, CONDUSEF / FTC.
- **(d) Value.** US reported fraud losses were $15.9B in 2025; impostor scams $3.5B [18].
- **(e) Form.** MON + PLAY.

### 1.21 Financial literacy — P2
- **(a) What a top advisor delivers.** Just-in-time explainers attached to the user's own data, next-generation education, and family-stewardship sessions [2].
- **(c) Split.** LLM.
- **(e) Form.** PLAY.

### 1.22 Fun and lifestyle goals — P1
- **(a) What a top advisor delivers.**
  - A guilt-free spending budget: the conscious-spending plan, or 50/30/20.
  - Goal jars for travel and hobbies.
  - "Permission to spend" when on track.
- **(b) Data.** Goals, surplus.
- **(c) Split.** Deterministic: how much is safe to spend. LLM: values elicitation.
- **(d) Value.** Adherence; retiree under-spending is a real failure mode [4].
- **(e) Form.** CALC + UI + PLAY.

---

## 2. Proactive annual calendar

### Mexico
| When | Item |
|---|---|
| Monthly, day 17 | Pagos provisionales ISR/IVA (actividad empresarial, RESICO, arrendamiento) |
| Jan | Update the UMA (Jan–Feb), new colegiaturas year, AFORE aportación plan; aguinaldo leftover into savings |
| Feb | AFORE and bank constancias de retenciones / intereses reales issued; they pre-fill the anual [16] |
| Mar 31 | Personas morales annual return (drives PTU timing) |
| Apr 1–30 | **Declaración anual, personas físicas**; claim deductions [12] |
| by May 30 | **PTU** from companies; by Jun 29 from employers who are individuals [19] |
| May–Jun | Put PTU into the emergency fund / PPR / debt per windfall rule |
| Jul | Mid-year IPS and allocation check; hurricane-season property-insurance check |
| Sep | **Mes del Testamento** [17]; review beneficiarios |
| Oct–Nov | Gastos médicos mayores renewal (many policies); Buen Fin card-debt guardrail |
| Dec | **Top up PPR/AFORE by Dec 31** to count for the year; **aguinaldo by Dec 20** [19]; get CFDIs for deductibles |
| Rolling | e.firma renewal (4-year validity); free Buró/Círculo report every 12 months [9]; AFORE statements (three a year) |

### United States
| When | Item |
|---|---|
| Jan 15 | Q4 estimated tax |
| Jan–Feb | W-2 / 1099 intake; new-year 401(k)/HSA deferral update [14] |
| Apr 15 | File or extend; **prior-year IRA/HSA contribution deadline**; Q1 estimated tax; FBAR (automatic extension to Oct 15) |
| Jun 15 | Q2 estimated tax |
| Sep 15 | Q3 estimated tax |
| Oct 15 | Extended returns due; the SS COLA is announced in October |
| Oct 15–Dec 7 | Medicare open enrolment [20] |
| Nov | Employer benefits open enrolment (HDHP/HSA, FSA, life/disability); ACA marketplace opens Nov 1 |
| Nov–Dec | TLH / gain harvesting; Roth conversion (must finish by Dec 31); DAF bunching; RMDs by Dec 31; annual gift exclusion; FSA use-it-or-lose-it |
| Rolling | 83(b) within 30 days of a grant; weekly free credit reports |

---

## 3. Thirty proactive nudges

| # | Nudge | Trigger | Data source |
|---|---|---|---|
| 1 | Surplus unallocated | Checking balance > 1.5× monthly spend for 2 cycles | Ledger |
| 2 | Emergency fund below target | Liquid reserve < target months | Ledger + balances |
| 3 | Cash drag | Reserve > 12 months of essentials | Balances |
| 4 | Promo APR expiring | 0% promo ends in ≤ 60 days | Card statement |
| 5 | Utilisation spike | Card utilisation > 30% | Statements |
| 6 | Credit report due | 12 months since last Buró/Círculo report | Memory |
| 7 | Windfall arrived | Deposit ≥ 1.5× typical in May–Jun (PTU) or Dec (aguinaldo, bonus) | Ledger |
| 8 | Drift | Asset class outside its IPS band | Holdings |
| 9 | Harvestable loss | Lot loss > $X / 5%, with no wash-sale conflict | Lots |
| 10 | 0% LTCG headroom | Projected taxable income below the 0% bracket top | Tax projection |
| 11 | Roth conversion window | Low-income year detected (gap year, sabbatical) | Tax projection + memory |
| 12 | 401(k) not maxed / match missed | YTD deferral pace below limit or match | Pay stubs |
| 13 | IRA/HSA prior-year deadline | March 15, with room remaining | Contributions |
| 14 | PPR/AFORE deduction headroom | November, when voluntary contributions < 10% of income / 5 UMA | Constancias + income |
| 15 | Missing CFDI | Deductible spend (medical, colegiatura) with no CFDI | Ledger + CFDI inbox |
| 16 | Declaración anual prep | April 1, with checklist incomplete | Tax model |
| 17 | Estimated tax due | 10 days before each quarterly date, when there is income without withholding | Ledger |
| 18 | Fee creep | New or increased fee, or fund TER above peer median + 25 bp | Statements |
| 19 | AFORE underperforming | AFORE net return in the bottom quartile of its SIEFORE | CONSAR data |
| 20 | Concentration | Single stock > 10% of net worth (often employer RSUs) | Holdings |
| 21 | RSU vest | Vest in the next 7 days, needing a sell and diversify plan | Grant schedule |
| 22 | 83(b) clock | Restricted grant detected, day 1 of 30 | Grant documents |
| 23 | Panic-sell circuit breaker | Market drawdown > 10% plus the user asks to sell all | Market + chat |
| 24 | Speculation cap breach | Play sleeve > cap, or its drawdown > limit | Holdings |
| 25 | Cool-off | Trade entered within 1 h of a > 5% move or late at night | Broker (IBKR Flex) |
| 26 | Scam pattern | New payee plus a large transfer plus words like "guaranteed" or "urgent" | Ledger + chat |
| 27 | Beneficiary gap | Life event (marriage, birth) with no update in 60 days | Memory |
| 28 | Testamento | September and no will on file | Estate vault |
| 29 | Insurance renewal / gap | Renewal in 30 days, or dependants added without life cover | Policies |
| 30 | Guilt-free spend | On track for goals but fun budget unspent 3 months running | Ledger + goals |

---

## 4. Build order
P0 first (finish IPS/rebalancing/location; then reporting, fee audit, reserve/debt/scam monitors, tax calendar, Ley 73/97 and SS timing), then P1, then P2. Always refer out filing, legal drafting, insurance purchase, IMSS paperwork and cross-border entity work.

---

## Sources
1. CFP Board, 7-Step Financial Planning Process — https://www.cfp.net/ethics/compliance-resources/2018/11/focus-on-ethics---the-7-step-financial-planning-process
2. Goldman Sachs PWM / Family Office Solutions — https://pwm.gs.com/global/en-us/what-we-do/goldman-sachs-family-office/family-office-solutions ; https://www.goldmansachs.com/disclosures/customer-relationship-summary-form-crs/docs/pwm-relationship-guide.pdf
3. Vanguard, Quantifying Advisor's Alpha — https://advisors.vanguard.com/content/dam/fas/pdfs/IARCQAA.pdf ; https://www.investmentnews.com/practice-management/advisors-continue-to-shine-as-emotional-circuit-breakers-vanguard-says/259609
4. Blanchett & Kaplan, Alpha, Beta, and Now… Gamma — https://www.morningstar.com/content/dam/marketing/shared/research/foundational/677796-AlphaBetaGamma.pdf
5. Chaudhuri, Burnham & Lo (FAJ 2020) — https://rpc.cfainstitute.org/research/financial-analysts-journal/2020/empirical-evaluation-tax-loss-harvesting-alpha
6. Morningstar, Mind the Gap 2025 — https://www.morningstar.com/business/insights/research/mind-the-gap
7. Fulkerson et al., "Bad Timing Does Not Cost Investors 15%…" (FAJ 2026) — https://rpc.cfainstitute.org/research/financial-analysts-journal/2026/bad-timing-does-not-cost-investors-funds-returns
8. Vanguard, Emergency savings and financial well-being (2025) — https://corporate.vanguard.com/content/dam/corp/research/pdf/relationship_between_emergency_savings_financial_well_being_financial_stress.pdf
9. Buró de Crédito, Reporte de Crédito Especial — https://www.burodecredito.com.mx/personas-f%C3%ADsicas/productos/reporte-de-cr%C3%A9dito-especial/ ; CONDUSEF — https://www.condusef.gob.mx/?p=contenido&idc=267&idcat=3
10. CFA Institute, Elements of an Investment Policy Statement for Individual Investors (2010) — https://www.cfainstitute.org/ (research foundation publication)
11. ESMA, product intervention on CFDs and binary options — https://www.esma.europa.eu/press-news/esma-news/esma-agrees-prohibit-binary-options-and-restrict-cfds-protect-retail-investors
12. SAT, Deducciones personales / Art. 151 LISR — https://www.sat.gob.mx/minisitio/DeduccionesPersonales/index.html
13. CONSAR, Beneficios fiscales del ahorro voluntario — https://www.gob.mx/consar/articulos/beneficios-fiscales-del-ahorro-voluntario
14. IRS, 2026 limits (Notice 2025-67) — https://www.irs.gov/newsroom/401k-limit-increases-to-24500-for-2026-ira-limit-increases-to-7500
15. SSA, Delayed retirement credits — https://www.ssa.gov/benefits/retirement/planner/agereduction.html
16. Tec de Monterrey, Pensión IMSS 2026 Ley 73 vs 97 — https://conecta.tec.mx/es/noticias/nacional/sociedad/pension-imss-2026-aumento-requisitos-y-diferencias-entre-ley-73-y-97
17. Colegio Nacional del Notariado, Septiembre Mes del Testamento — https://colegiodenotarios.org.mx/septiembre-mes-testamento ; Infobae 2026-09-17
18. FTC, imposter scams 2025 — https://www.ftc.gov/news-events/news/press-releases/2026/06/ftc-data-show-people-reported-losing-3-point-5-billion-imposter-scams-2025
19. LFT Art. 122 (PTU) / Art. 87 (aguinaldo), summarised at https://www.buk.mx/blog/reparto-de-utilidades
20. Medicare Open Enrollment — https://www.medicare.gov/health-drug-plans/open-enrollment
