# Wealth pattern library: product teardown

Sept 2026. Sources: Appllama screens, cited as **[A] App › Screen**, which I looked at image by image. Web and help-centre material is cited as **[W]**. Robinhood, Wealthfront, Public, Nu, GBM, Bitso and Fintual aren't in the Appllama library, so their entries come from the web. P0 = before launch, P1 = next quarter, P2 = later.

Every "Adopt" note follows the repo's `docs/ux-bar.md`: value first, one primary vermilion action, cobalt only for live data, ink for settled numbers, hairlines instead of cards, Roboto Mono tabular figures, and es-MX and en as equals.

---

## 1. Onboarding and first value

**1.1 Try it on fake numbers before signing up.** Best: YNAB. [A] YNAB › Budget Demo Intro → Fully Funded Demo → Create Account. The app shows a $5,000 balance with "Tap to fill" pills on each category, and the account form comes last ("Now it's your turn"). *Why:* the aha comes before any friction. *Adopt:* on the landing page, run a live chat turn against a sample household ("Ana, CDMX, 42k MXN/mes") so visitors can ask real questions. Put one vermilion "Use my numbers" button after the first answer. **P1**

**1.2 A demo-mode escape hatch at the linking wall.** Best: Copilot. [A] Copilot › Bank Link Dashboard shows "Not ready to link yet? Try demo mode →". A "Continue without them" modal lets the user skip linking but explains what they lose. *Why:* people who won't link don't churn at the wall. *Adopt:* the onboarding card for step 5 offers "Upload a statement", "Type amounts" and "Later". Skipping keeps the picture visible, with each unknown labelled *unknown* rather than shown as zero. **P0**

**1.3 One card for every way data gets in.** Best: CoinStats. [A] CoinStats › Connect Portfolio stacks "Connect Portfolio" (providers), "Track Any Wallet" and "Add Manual Transaction". Binance CSV Import offers API Sync / CSV and a CSV template. *Why:* aggregation barely covers GBM, Cetesdirecto, AFORE or PPR. *Adopt:* "Where is your money?" chips branch to PDF statement, CSV, screenshot or typed amount. A PDF statement is the default for MX institutions. **P0**

**1.4 Echo each answer into a visible picture.** Best: YNAB. [A] YNAB › Categories Built → Category List Start turns the quiz answers into *Morris's Plan*. *Why:* each answer visibly produces something. *Adopt:* after each onboarding card, a hairline "picture" strip above the composer updates (net worth, monthly free cash, goals) in ink mono. New values pulse cobalt once. **P0**

## 2. Home / today

**2.1 A narrative feed, not a dashboard.** Best: Copilot and ChatGPT Pulse. The Copilot web dashboard feed centres on "Mark as reviewed" [W copilot.money]. Pulse delivers a few cards a day that *expire at day end unless saved*, and users steer it with "curate" and thumbs [W openai.com]. *Why:* the feed is bounded and ages out, so it never becomes clutter. *Adopt:* chat opens with at most three dated "today" lines above the composer, such as "Nómina de $38,400 llegó", "2 cargos por revisar" and "CETES vence el jueves". Nothing else. **P0**

**2.2 One headline number, with change and date.** Best: Copilot. [A] Copilot › Bank Link Dashboard shows "$0.00 in assets / $0.00 in debt" with 1W/1M/3M/YTD/1Y chips. *Adopt:* the You page leads with net worth in large ink mono, the change since the last statement, and "al 15 sep". **P0**

**2.3 Honest "still computing" states.** Best: CoinStats. [A] CoinStats › Portfolio Overview says "Your historical chart is generating. It might take a few minutes." *Adopt:* while a statement is being ingested, show "Leyendo tu estado de GBM…" in cobalt text and no placeholder chart. **P0**

## 3. Net worth, holdings, performance, allocation

**3.1 Grouped account ledger with manual rows.** Best: Copilot. [A] Copilot › Bank Link Account List has sections for Credit cards, Depository, Real estate and Others ("Manual account"). *Adopt:* the You › Accounts table uses hairline rows grouped Cash / Investments / Retirement (AFORE, 401k) / Property / Debt. Each row shows its source and date, e.g. "Estado de cuenta ago" or "Manual", and live rows get a cobalt dot. **P0**

**3.2 Re-group the same holdings by any lens.** Best: Seeking Alpha. [A] SA › Portfolio Group By (Sector, Country, Asset Type, Custom). *Adopt:* in chat, "¿cómo estoy repartido?" returns an allocation card with a lens switch for asset class, currency (MXN/USD, which matters for Mexican households), country and account. **P1**

**3.4 Label the return method.** Best: Wealthfront and Betterment, which both show time-weighted versus money-weighted returns. *Adopt:* every return figure carries a mono caption such as "TWR 1a" or "Tu rendimiento (MWR)", with a one-sentence explainer on tap. The engine owns the math. **P0**

## 4. Spending and cash flow

**4.1 Three buckets and one safe-to-spend number.** Best: Monarch. Flex Budgeting splits spending into Fixed, Flexible and Non-monthly, and the page leads with a "Safe to Spend" number. [W nerdwallet] *Adopt:* the ledger computes "Libre este mes: $12,300" (income minus fixed minus non-monthly accruals) and pins it as the answer to "¿cuánto puedo gastar?". **P0**

**4.2 Plan for irregular expenses at setup.** Best: YNAB. [A] YNAB › Future Bills Intro and Infrequent Expenses ask about annual card fees, medical, taxes and vet. *Adopt:* add a Mexico-specific chip set: predial, tenencia, seguro de auto, colegiaturas, aguinaldo inflow, PTU inflow, reinscripción. Each becomes a monthly accrual in the engine. **P0**

**4.3 Recurring-charge watch with the three alerts people actually want.** Best: Rocket Money. [A] Rocket Money › Update Preferences previews "Your Doordash subscription renews in 5 days", "Unusually large transaction detected" and "Your Spotify price increased by $4.99 per month". *Adopt:* after each statement ingest, the recurring detector writes only three nudge types: renews soon, price went up, and new recurring charge. **P1**

**4.4 A review queue with "Was this you?"** Best: Copilot. [A] Copilot › Notification Preferences shows "Was this you? Did you recently spend $1,287 at Macy's?", and › Types of Transactions separates Regular, Internal transfer and Excluded. *Why:* App Store reviewers credit the habit with catching fraud. *Adopt:* after ingest, chat posts "3 movimientos por revisar" as one card with inline chips (Correcto / Transferencia / Excluir). The ledger learns from each answer. **P0**

## 5. Goals and planning

**5.1 An outcome cone instead of a single line.** Best: Fintual. The simulator draws a blue triangle and explains that bad scenarios land near the bottom and good ones near the top [W fintualist.com]. *Adopt:* goal cards show P10/P50/P90 as a hairline band plus one sentence, e.g. "En 9 de cada 10 escenarios llegas a $1.2M o más en 2034." Say "escenarios", never "garantizado". **P0**

**5.2 Show the trade-off before asking for the decision.** Best: Apple Card and Wealthfront Path. Apple's payment wheel estimates interest in real time as you pick an amount [W support.apple.com/102294]. Path lets you drag savings rate or retirement age and redraws immediately. *Adopt:* in chat, "¿y si ahorro $3,000 más?" returns a before/after card (date reached and probability, both in mono) with a "Probar otro monto" stepper. **P0**

## 6. Investing actions (Wealth only advises; the broker executes)

**6.1 A deterministic review card before any action.** Best: Robinhood options, which shows max loss and breakeven on every order [W robinhood.com/support/profit-loss-chart]. *Adopt:* every recommendation ends with a "Revisión" block in mono: amount, where it goes, fees, tax effect, worst 1-year drawdown, and what we're assuming. The user taps "Hecho, lo hice en GBM", which writes to the ledger. **P0**

**6.2 Recurring buys in fixed amounts with fixed dates.** Best: Cetesdirecto Ahorro Recurrente. Weekly (Tuesdays), biweekly (1st/15th) or monthly, $100–$12,000 MXN, editable or cancellable without penalty [W excelsior.com.mx]. *Adopt:* "Plan de aportación" is a first-class ledger object. Wealth reminds on the charge day and reconciles against the next statement ("¿Se hizo tu compra de CETES del martes?"). **P1**

**6.3 Show the tax-loss harvesting value in money.** Best: Wealthfront. The Tax Savings tab shows estimated YTD benefit as harvested losses × the user's inferred combined rate, [W wealthfront.com/blog/tlh-results-2025]. *Adopt:* US users get "Pérdidas aprovechables: $4,200 ≈ $1,000 menos de impuestos", with wash-sale warnings from the engine. MX users get the equivalent for the ISR on BMV/SIC gains. **P1**

**6.4 Let the profile questionnaire drive the portfolio.** Best: GBM. Portafolios recomendados come from a required profiling questionnaire [W gbm.com]. *Adopt:* the answer to onboarding step 8 (the 20% drop question) sets a visible "Perfil: moderado". Every allocation answer cites it: "porque dijiste que mantendrías". **P0**

## 7. Speculation guardrails

**7.1 Graduated permissions with a learning route.** Best: Robinhood options, with Levels 0–3 gated by experience, objective and income.; declined users are sent to learning material [W robinhood.com/options-knowledge-center]. *Adopt:* when a user asks about options, leverage or perps, Wealth first checks the risk profile and the emergency-fund state. If either fails, it answers educationally and states the one condition that would change the advice. **P0**

**7.2 No celebration of trading activity.** Best: Robinhood, by removing confetti in 2021. The MA settlement bans celebratory imagery tied to trade frequency [W cnbc.com 2021-03-31]. *Adopt:* celebrate only progress milestones (emergency fund complete, debt paid), never trades or deposits. Use a single ink check mark and no motion. **P0**

**7.3 Put the risk warning in the same sentence as the yield.** Best: Bitso. Disclosures state perps carry "risk of total loss" [W support.bitso.com]. *Adopt:* whenever crypto or stablecoin yield comes up, the same sentence names the counterparty and states that the money has no IPAB protection, next to a Cajita or CETES yield for comparison. **P0**

## 8. Research

**8.1 A contextual question on every asset page.** Best: Seeking Alpha and Robinhood. [A] SA › TSM Stock Summary shows "✦ Explain recent price moves in TSM →" directly under the price. Robinhood Cortex Digests answer "why is it moving" from news, analyst notes and technicals (95% of surveyed users loved it) [W newsroom.aboutrobinhood.com]. *Adopt:* when a holding is mentioned, the chat card offers "¿Por qué se movió?" and "¿Qué pesa en mi portafolio?". **P1**

**8.2 Say how the question was interpreted, with an as-of date.** Best: Ask Seeking Alpha. [A] SA › Ask Seeking Alpha Answer: "The screen applies these criteria: Quant rating: Strong Buy… Minimum share price: $1", followed by "Results from 09/08/2026 – market data may have changed". *Adopt:* every research answer starts with a collapsed "Cómo lo leí" line (criteria and data date). **P0**

**8.3 Inline citations you can check.** Best: Perplexity Finance, whose citations were verified about 89% of the time in one editorial test [W]. *Adopt:* numeric claims link to ledger rows or statement pages ("estado GBM ago, p.2"). Market claims link to the source. Use mono superscripts and no footnote essay. **P0**

## 9. Memory, profile and "what we know"

**9.1 An editable summary with two memory layers.** Best: ChatGPT. It separates "Saved memories" (an explicit list) from "Reference chat history". The newer screen is a prose summary you correct by typing or by highlighting a sentence and deleting it. [W help.openai.com/8590148] *Adopt:* the You page memory already uses sentences. Add highlight-to-delete and "Corregir" inline editing. Keep facts derived from statements (ledger) visually apart from facts the user told us (said). **P0**

**9.2 Visible "memory updated" acknowledgements.** Best: ChatGPT, which shows a "Memory updated" chip in the thread. *Adopt:* after the deferred save, show a quiet mono line under the answer ("Guardado: renta $14,000/mes"). Tapping it opens the fact with an undo. **P0**

## 10. Permissions and trust

**10.1 Name the permission scope in the first sentence.** Best: Copilot and CoinStats. [A] Copilot › Link bank accounts: "Securely give Copilot read-only access… We don't sell your financial data." [A] CoinStats › Binance CSV Import: "We are only requesting view permissions. This does not give us access to your private keys nor the ability to move your funds." *Adopt:* the upload card says "Solo leemos. No movemos dinero ni guardamos contraseñas. Borra el archivo cuando quieras." **P0**

**10.2 A capability table for each connector.** Best: ChatGPT Apps. [A] ChatGPT › App Detail Info is a table with Category, **Capabilities: Interactive, Writes**, Developer, Privacy Policy. *Adopt:* You › Conexiones lists each source (upload, email forward, MCP) with its capabilities (Lee / Escribe), last sync and "Desconectar". Revoking a source prompts for whether to delete its data. **P0**

**10.3 Public connection status.** Best: Monarch, whose public Connection Status Dashboard lets users choose between Plaid, Finicity and MX per institution [W monarch.com/blog/connectivity-dashboard]. *Adopt:* each account row shows freshness ("Estado: ago 2026 · 36 días") and turns vermilion when stale. **P1**

**10.4 Export and delete in plain view.** Best: ChatGPT › Data controls [A]. *Adopt:* You › Datos offers "Descargar todo (CSV + PDFs)" and "Borrar mi cuenta", each with a one-line consequence. **P0**

## 11. Notifications and nudges

**Loved:** "Was this you?" on large charges (Copilot [A]); renewal and price-increase alerts (Rocket Money [A]); Cajita maturity and yield-change alerts (Nu); anything with a number and a date.
**Hated:** engagement pings with nothing to do; tone that shames (Cleo Roast Mode works for some, but the FTC's $17M settlement shows the risk when personality pushes a product [W lendedu.com]); "markets are down" alarms.

**11.1 Preview notifications during setup.** Best: Copilot and Rocket Money. [A] Copilot › Notification preferences shows a sample notification above each toggle. *Adopt:* in onboarding, preview "CETES vence el jueves: ¿reinvierto?" with one toggle. Default to three categories only. **P0**

**11.2 A weekly or monthly letter instead of a stream.** Best: Monarch Month in Review and ChatGPT Pulse. *Adopt:* one Sunday note ("Tu semana: +$2,100 libres, 1 cargo nuevo") delivered to chat and email. Everything else is batched into it. **P1**

## 12. Chat and assistant UX

**12.1 Suggestions built from the user's own data.** Best: CoinStats AI. [A] CoinStats › Connect Portfolio sheet asks "Which single position is quietly driving most of my risk?" *Adopt:* empty-state prompts are computed from the ledger ("¿Por qué gasté 18% más en agosto?", "¿Me alcanza para el enganche en 2027?"). Show at most three, and never generic ones. **P0**

**12.2 Answer as a card, with provenance in reserve.** Best: Ask Seeking Alpha. *Adopt:* the answer is a sentence plus one deterministic card (table, cone or ledger rows), then 👍/👎 and "Guardar". The engine produces the numbers and the model writes the sentence. **P0**

**12.3 Actions from chat that confirm before writing.** Best: ChatGPT connectors, which label Writes capability [A]. *Adopt:* chat proposes ledger edits as a diff card ("Cambiar renta: $12,000 → $14,000") with one vermilion "Aplicar". Nothing is written silently. **P0**

**12.4 Disclose limits and usage quietly.** Best: Warren AI. [A] Warren AI › Home shows "0/5 credits used this month" and "may provide incorrect information and is never financial advice" as small text under the composer. *Adopt:* one line under the composer, "Wealth puede equivocarse; los números vienen de tus estados", with nothing repeated per answer. **P0**

## 13. Microcopy and tone

- Use **tú**, always, as Nu, Klar and Fintual do [W]: short, warm and concrete ("Tu dinero crece todos los días"). Avoid "usted" and anglicisms like "trackear".
- Say MX terms natively: *aguinaldo, PTU, predial, tenencia, nómina, quincena, CLABE, SPEI, AFORE, PPR, CETES*. Say "estado de cuenta", not "statement".
- Money in es-MX uses "$12,300" with "MXN" only when USD is also present. Dates look like "15 sep".
- Borrow: "Ninguna aplica" (YNAB), "Seguir sin conectar" (Copilot), "Solo lectura" (CoinStats), and Nu's honest "no podrás retirar hasta que finalice".
- Skip forced cheer (YNAB's "Hold for applause"). It doesn't suit a private-banker voice.

## 14. Information architecture

- The norm is 4–5 bottom tabs ([A] SA, Warren AI, Buddy). ChatGPT uses a drawer plus the composer instead ([A] ChatGPT › Navigation Drawer).
- **Adopt:** keep two surfaces, **Chat** and **Tú**, and nothing more. Tú has four hairline sections: Patrimonio, Flujo, Metas, Memoria/Datos. Every chat card deep-links into one of them, and every section has "Preguntar sobre esto". Navigation should be at most two levels deep.

---

## The 15 highest-leverage patterns, ranked

1. Deterministic review card before any action (6.1) — P0
2. Ask about the data you hold: suggested prompts built from the user's ledger (12.1) — P0
3. Safe-to-spend number from fixed, flexible and non-monthly buckets (4.1) — P0
4. Outcome cone with a plain-language probability sentence (5.1) — P0
5. Statement-first, multi-path data intake with a skip that keeps the picture (1.2, 1.3) — P0
6. Transaction review queue with "¿Fuiste tú?" chips (4.4) — P0
7. Say how the question was interpreted, plus an as-of date and citations to ledger rows (8.2, 8.3) — P0
8. Editable memory summary with ledger-versus-told provenance and undo (9.1, 9.2) — P0
9. Live trade-off stepper for "what if" (5.2) — P0
10. Mexico irregular-expense accruals: predial, tenencia, aguinaldo, PTU (4.2) — P0
11. Read-only promise and a connector capability table with revoke (10.1, 10.2) — P0
12. Answers checked against the risk profile, with guardrails for speculation (6.4, 7.1, 7.3) — P0
13. Three bounded "today" lines instead of a dashboard (2.1) — P0
14. Recurring-charge watch limited to three alert types (4.3) — P1
15. Weekly letter in place of a notification stream (11.2) — P1

## Anti-patterns to avoid

- Confetti, streaks or badges tied to trades or deposits (Robinhood 2021).
- Personality that funnels users into a product (Cleo's cash advances, FTC $17M).
- Walls of marketing before first value: testimonials, "how did you hear about us", award laurels ([A] Rocket Money › Testimonial Intro).
- Paywall teasers inside answers ([A] SA "Go Premium to see TSM Quant Rating", locked factor grades). Answers should never be partial.
- Linking as a hard requirement. Coverage in Mexico can't support it.
- Zeros for unknown values ([A] Copilot's "$0.00 in assets" before linking reads as fact).
- A disclaimer on every AI answer. State it once, under the composer.
- A single-line projection with no range, or "guaranteed" language about yield (Nu's Cajita Turbo rate fell from 15% to 13%).
- Memory that saves silently with no visible acknowledgement or undo.
