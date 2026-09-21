# Financial judgment and interpretation

Read the section relevant to the current question. Task inputs and examples
come from `wealth_context(intent=...)`; this guide covers interpretation.
A useful conclusion makes its evidence, assumptions and alternatives explicit.

## Separate the questions

1. What is true about the asset, business, fund or strategy?
2. What does the household need, when, and in which currency?
3. What risks can it afford and what risks will the person tolerate?
4. What change improves that situation after costs, tax and constraints?
5. What evidence would change the decision?

Research, planning, construction, implementation and review are separate steps.
An attractive company is not automatically an appropriate holding. A good
portfolio on paper may be inappropriate for the account or household. See
[CFP Board's planning process](https://www.cfp.net/ethics/compliance-resources/2020/01/practice-standards-for-the-financial-planning-process).

## Measure honestly

- Correlation measures linear co-movement of returns. It is not the fraction
  of days with the same sign. Use the engine's `same_sign_fraction` when that
  is the actual question. Neither is a prediction that diversification persists.
- `1 / sum(weight**2)` is effective holding count under a concentration measure,
  not the number of independent economic exposures. Correlated positions can have
  many equal-sized holdings and still share one dominant exposure.
- Beta is a fitted historical sensitivity within a sample. It is not a promise
  about what happens in the next selloff. Report currency, benchmark and window.
- PCA variance share is a statistical property, not proof of one causal driver.
- Historical worst loss is neither the worst possible loss nor a future limit.
  Absence of an old crisis in a young fund's history is not evidence of resilience.
- Equal weights, equal volatility contributions and equal economic exposures
  are different objectives. [Markowitz's original portfolio-selection paper](https://doi.org/10.1111/j.1540-6261.1952.tb01525.x)
  grounds the distinction between holdings and portfolio risk.
- Factor regressions require the appropriate market, currency and methodology;
  preserve data version and sample. The [French data library](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/data_library.html)
  documents its research portfolios and methodology changes.

## Put the household first

A household is not a tax identity. Keep people, tax units, account owners,
wrappers, entities, currencies, restricted assets, liabilities and goals distinct.
A broker statement is complete only within its stated accounts and date. A private
business estimate is not liquid cash. Employer risk can link salary and assets.

Protect explicit hard cash needs using dated, currency-aware planning. Never
subtract outside funding twice or reuse one reserve for several goals. Do not
apply arbitrary universal equity floors, maximums, or gold allocations. Compare
current policy with alternatives against actual goals and capacity.

`plan` reserves declared capital for expenses, debt payments and protected goals.
Its total uncommitted capital can include existing investments; only explicitly
supplied unrestricted cash supports `additional_cash_to_invest`. The calculation
does not reconcile that declared capital against household holdings or restrictions.

For return and inflation scenarios, `project` and `income` simulate supplied
cash flows and assumptions; `ladder` matches dated asset receipts to liabilities.
Withdrawal fulfillment and positive terminal wealth are separate outcomes.
These tools do not infer insurance needs, pension entitlements or a debt schedule.

## Construction that earns its complexity

Start with a transparent feasible baseline. Then ask whether a more elaborate
method produces a material improvement after estimation uncertainty, fees,
turnover, taxes, liquidity and account restrictions. Preserve assumptions, input
windows, solver status, constraints and a comparable baseline.

Shrunk covariance, constrained risk budgets, expected shortfall/CVaR and
hierarchical allocation are candidates, not mandatory features. Black-Litterman
requires explicit views and confidence. A backtest must handle look-ahead,
survivorship, corporate actions, changing constituents and sample selection;
walk-forward evaluation and sensitivity tests precede production claims.

The existing engine estimates historical risk and can construct weights. It does
not establish a complete household policy or account-level implementation plan.
The [SEC's fee discussion](https://www.sec.gov/oiea/investor-alerts-bulletins/investoralertsib_fees_expensespdf)
explains why small recurring fees matter over time.

## Income is a cash-flow problem

Start with spending amount, currency, timing, other income and funding horizon.
Compare distributions, bond maturities and planned sales on the same basis.
Dividends can change. Yield is not total return or a guarantee. Distribution
payments may include return of capital. Show cash shortfalls and sequence risk;
do not infer sustainability by comparing withdrawal rate with recent CAGR.

`calendar` uses supplied `expected_cash_received`, committed outflows and spending
needs. State whether supplied receipts already reflect taxes or other deductions;
the calendar does not calculate them. It does not infer dividend amounts or cuts.
`income` and `project` can model withdrawals using explicit return, inflation,
fee and tax-drag assumptions. Dividends are part of total return, not an extra
return added to it. A simulated success rate depends on those assumptions.

## Taxes are jurisdiction-specific computation

`tax` implements scoped US federal taxable-security scenarios and qualifying
Mexico Article 129 scenarios. Read the selected jurisdiction's current contract:
account type, lots, basis, sale-date evidence, eligibility and coverage matter.
Incomplete inputs can leave candidates visible while withholding a savings
estimate. Missing basis or realized tax results are unknown, never zero.
This is not a general tax-return calculator or support for every jurisdiction.

For U.S. workflows, [IRS Publication 550](https://www.irs.gov/publications/p550)
is a primary starting point for basis, identification and wash sales; future
purchases as well as prior transactions can affect a candidate. Retirement
accounts have separate [Publication 590-B](https://www.irs.gov/publications/p590b)
rules. Neither is a global tax policy.

For Mexico, use the applicable [Income Tax Law](https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf)
and relevant current administrative guidance. Do not transfer U.S. wash-sale,
holding-period or account assumptions. A proposed harvest is an estimate with
conditions and data gaps, not a guaranteed saving or an executed order.

Source links are reference material, not a guarantee of current law. Check the
applicable current primary sources before making a tax recommendation.
