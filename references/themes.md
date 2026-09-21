# Themes → instruments

A starting map so "I want AI" resolves to candidates without a search. Not a
recommendation list — these are things to *measure* with `wm.py analyze`, and
most of them will turn out to be the same bet.

**Read before using.**
- Expense ratios are annual, in %, and drift. Anything marked **(verify)** is a
  number or a listing I am not fully confident in — confirm it before quoting
  it to a user. `analyze` may also return an `expense_ratio` field; prefer that
  when it is non-null.
- Single names are *representative*, not endorsements. They exist so you can
  show the user the correlation cluster, not so you can pick one.
- The "correlation trap" column is the point of this file. Read it out loud.
- Non-US listings (`.KS`, `.DE`, `.MX`) may not download via yfinance and may
  be untradeable in the user's brokerage. Check `warnings` in the engine output.

---

## Growth / technology

### AI & semiconductors
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | SMH | VanEck Semiconductor | 0.35 |
| ETF | SOXX | iShares Semiconductor | 0.35 |
| ETF | XSD | SPDR S&P Semiconductor (equal-weight) | 0.35 |
| Name | NVDA | Nvidia | — |
| Name | AVGO | Broadcom | — |
| Name | TSM | TSMC (ADR) | — |
| Name | ASML | ASML (ADR) | — |

*Trap:* SMH is ~20–25% NVDA. Buying SMH **and** NVDA is doubling one position,
not diversifying. The whole complex has run >0.8 to each other and ~1.4–1.6
beta to SPY in recent years — most of an "AI view" is levered market.

### AI memory / storage
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | — | no clean pure-play; use SMH / SOXX as proxy | — |
| Name | MU | Micron | — |
| Name | 000660.KS | SK Hynix (verify tradeability) | — |
| Name | WDC | Western Digital | — |
| Name | SNDK | SanDisk (post-2025 spin-off) (verify) | — |

*Trap:* memory is a **commodity cycle**, not a secular theme. These names move
on DRAM/NAND pricing together — correlation within the group is typically the
highest in this file, and they crack as one when the cycle turns. One bet.

### Cloud & software
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | IGV | iShares Expanded Tech-Software | 0.41 |
| ETF | SKYY | First Trust Cloud Computing | 0.60 |
| ETF | WCLD | WisdomTree Cloud Computing | 0.45 |
| Name | MSFT | Microsoft | — |
| Name | CRM | Salesforce | — |
| Name | NOW | ServiceNow | — |
| Name | SNOW | Snowflake | — |

*Trap:* long-duration growth — this is a **rates** bet as much as a software
bet. 2022 is the reference regime. Also overlaps heavily with any US
large-cap-growth position the user already owns via VOO/QQQ.

### Cybersecurity
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | CIBR | First Trust Nasdaq Cybersecurity | 0.60 |
| ETF | BUG | Global X Cybersecurity | 0.51 |
| ETF | HACK | Amplify Cybersecurity | 0.60 |
| Name | PANW | Palo Alto Networks | — |
| Name | CRWD | CrowdStrike | — |
| Name | FTNT | Fortinet | — |
| Name | ZS | Zscaler | — |

*Trap:* marketed as defensive ("spending is non-discretionary"); trades as
high-multiple software. Correlation to IGV is usually >0.85. Not a diversifier
from cloud/software — the same bet at a higher fee.

### Robotics & automation
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | BOTZ | Global X Robotics & AI | 0.68 |
| ETF | ROBO | ROBO Global Robotics | 0.95 |
| ETF | ARKQ | ARK Autonomous Tech & Robotics | 0.75 |
| Name | ISRG | Intuitive Surgical | — |
| Name | ROK | Rockwell Automation | — |
| Name | FANUY | Fanuc (ADR) (verify) | — |
| Name | ABBNY | ABB (ADR) (verify) | — |

*Trap:* the ETFs are semis + industrials in a costume — substantial NVDA and
Japanese industrial overlap, at 0.68–0.95% fees. Check what's actually inside
before treating it as a separate theme from AI/semis.

---

## Energy & resources

### Clean energy
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | ICLN | iShares Global Clean Energy | 0.41 |
| ETF | TAN | Invesco Solar | 0.69 |
| ETF | QCLN | First Trust Clean Energy | 0.58 |
| Name | FSLR | First Solar | — |
| Name | ENPH | Enphase Energy | — |
| Name | NEE | NextEra Energy | — |

*Trap:* a **rates and policy** bet, not a climate bet. Long-duration cash flows
plus subsidy dependence — 2022 and the 2021 peak are the regimes to show.
Drawdowns here have exceeded 70%; quote the number before anything else.

### Nuclear & uranium
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | URA | Global X Uranium | 0.69 |
| ETF | URNM | Sprott Uranium Miners | 0.75 |
| ETF | NLR | VanEck Uranium & Nuclear | 0.61 (verify) |
| Name | CCJ | Cameco | — |
| Name | CEG | Constellation Energy | — |
| Name | BWXT | BWX Technologies | — |
| Name | LEU | Centrus Energy | — |

*Trap:* two different things wearing one label. URA/URNM are **miners**
(commodity beta, brutal drawdowns); CEG/BWXT are utilities and defence
contractors. NLR mixes them. Since 2023, CEG has traded largely as an AI
datacentre-power story — expect high correlation to the AI complex, not to
uranium.

### Energy / oil
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | XLE | Energy Select Sector SPDR | 0.09 |
| ETF | XOP | SPDR Oil & Gas E&P | 0.35 |
| ETF | AMLP | Alerian MLP (midstream, income) | 0.85 (verify) |
| Name | XOM | Exxon Mobil | — |
| Name | CVX | Chevron | — |
| Name | COP | ConocoPhillips | — |
| Name | SLB | SLB (Schlumberger) | — |

*Trap:* genuinely low correlation to tech — one of the few real diversifiers in
this file — but it is a crude-price bet, and XOP is roughly 1.5× the swing of
XLE. AMLP has K-1/tax quirks in some structures (do not advise; just flag).

### Gold & precious metals
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | IAU | iShares Gold Trust | 0.25 |
| ETF | GLD | SPDR Gold Shares | 0.40 |
| ETF | SGOL | abrdn Physical Gold (verify) | 0.17 |
| ETF | GDX | VanEck Gold Miners | 0.51 |
| Name | NEM | Newmont | — |
| Name | AEM | Agnico Eagle | — |
| Name | WPM | Wheaton Precious Metals | — |

*Trap:* bullion and miners are **not the same asset**. Bullion (IAU) has near-
zero equity beta; GDX is a levered equity bet on the gold price with ~1.0+
market beta that falls with stocks in liquidity crises. If the user wants a
diversifier, that is bullion. Also: IAU is 15bps cheaper than GLD for the same
exposure — say so.

---

## Defensive & real economy

### Defence
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | ITA | iShares US Aerospace & Defense | 0.40 (verify) |
| ETF | XAR | SPDR S&P Aerospace & Defense | 0.35 |
| ETF | PPA | Invesco Aerospace & Defense | 0.58 (verify) |
| ETF | EUAD | Select STOXX Europe Aerospace & Defense (verify) | 0.50 (verify) |
| Name | LMT | Lockheed Martin | — |
| Name | RTX | RTX Corporation | — |
| Name | NOC | Northrop Grumman | — |
| Name | RHM.DE | Rheinmetall (verify tradeability) | — |

*Trap:* ITA/PPA carry heavy **commercial aerospace** (BA, GE) — you may be
buying the air-travel cycle, not defence budgets. European and US defence have
diverged sharply since 2022; they are not interchangeable.

### Healthcare & biotech
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | XLV | Health Care Select Sector SPDR | 0.09 |
| ETF | IBB | iShares Biotechnology | 0.45 |
| ETF | XBI | SPDR S&P Biotech (equal-weight) | 0.35 |
| Name | UNH | UnitedHealth | — |
| Name | JNJ | Johnson & Johnson | — |
| Name | MRK | Merck | — |
| Name | VRTX | Vertex Pharmaceuticals | — |

*Trap:* XLV is a defensive mega-cap sector; XBI is equal-weight small-cap
biotech with speculative-growth behaviour and rate sensitivity. Calling both
"healthcare" is how people accidentally buy the wrong risk.

### GLP-1 / obesity
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | — | no clean, liquid pure-play; use XLV or IBB | — |
| Name | LLY | Eli Lilly | — |
| Name | NVO | Novo Nordisk (ADR) | — |
| Name | VKTX | Viking Therapeutics | — |
| Name | AMGN | Amgen | — |

*Trap:* a **two-stock duopoly** with a tail of binary clinical bets. LLY+NVO is
one regulatory/trial-outcome bet; VKTX is a lottery ticket that can gap 40% on
a data readout. If the user wants the theme without single-name binary risk,
there isn't a clean instrument — say that rather than inventing one.

---

## Geography

### Emerging markets (broad)
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | IEMG | iShares Core MSCI EM | 0.09 |
| ETF | VWO | Vanguard FTSE EM | 0.08 (verify) |
| ETF | AVEM | Avantis EM Equity | 0.33 |
| ETF | EEM | iShares MSCI EM (legacy, expensive) | 0.70 (verify) |

*Trap:* "EM" is ~50–60% China/Taiwan/Korea, and Taiwan/Korea means TSMC and
Samsung — i.e. more semis. EM is often a second helping of the AI trade plus a
dollar bet. Also: EEM and IEMG hold nearly the same thing at 8× the fee.

### India
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | INDA | iShares MSCI India | 0.62 (verify) |
| ETF | SMIN | iShares MSCI India Small-Cap | 0.75 (verify) |
| ETF | EPI | WisdomTree India Earnings | 0.87 (verify) |
| ETF | INDY | iShares India 50 | 0.89 (verify) |

*Trap:* no cheap option exists — every India vehicle is 60bps+, which is a
permanent drag on a long-horizon holding. It is also already inside any EM
fund the user owns (roughly 15–20%), so check for double-counting first.

### Mexico
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | EWW | iShares MSCI Mexico | 0.50 |
| ETF | NAFTRAC.MX | BMV IPC tracker, peso-listed (verify) | 0.25 (verify) |
| Name | AMX | América Móvil (ADR) | — |
| Name | WALMEX.MX | Walmart de México (verify) | — |
| Name | GFNORTEO.MX | Banorte (verify) | — |
| Name | FEMSAUBD.MX | FEMSA (verify) | — |

*Trap:* the index is a handful of names — telecoms, banks, consumer staples —
so "Mexico" is a concentrated financials-and-consumer bet, not a country's
economy. For a peso-based investor it is also **triple** concentration: their
salary, their currency, and their portfolio all ride the same economy. EWW is
USD-denominated, so it carries MXN/USD exposure on top.

---

## Income & ballast

### Dividend / cash-flow equity
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | SCHD | Schwab US Dividend Equity | 0.06 |
| ETF | VYM | Vanguard High Dividend Yield | 0.06 |
| ETF | DGRO | iShares Core Dividend Growth | 0.08 |
| ETF | VIG | Vanguard Dividend Appreciation | 0.05 (verify) |

*Trap:* **this is not the safe part.** Beta is typically 0.8–0.95 and these fell
hard in 2020 and 2008. They have a value/profitability tilt and will lag badly
in a mega-cap-growth market — which is fine, if chosen knowingly. Don't let a
dividend yield be mistaken for a bond.

### US Treasuries & T-bills
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | SGOV | iShares 0–3 Month Treasury | 0.09 (verify) |
| ETF | BIL | SPDR 1–3 Month T-Bill | 0.14 (verify) |
| ETF | SHV | iShares Short Treasury (<1y) | 0.15 |
| ETF | SHY | iShares 1–3 Year Treasury | 0.15 |
| ETF | IEF | iShares 7–10 Year Treasury | 0.15 |
| ETF | TLT | iShares 20+ Year Treasury | 0.15 |
| Direct | CETES 28d | Mexican 28-day bills, via cetesdirecto (MXN) | n/a |

*Trap:* duration is the whole story. SGOV/BIL are genuine cash-equivalents and
the right home for 12–24 month needs. TLT is **not** a safe asset — it fell
roughly 48% peak-to-trough in the 2022–23 rate cycle, worse than many equity
funds. For a peso-spending investor, USD T-bills are a currency bet; CETES are
the ballast.

### Broad market core
| Type | Ticker | Name | ER % |
|---|---|---|---|
| ETF | VTI | Vanguard Total US Stock Market | 0.03 |
| ETF | VOO | Vanguard S&P 500 | 0.03 |
| ETF | ITOT | iShares Core S&P Total US Market | 0.03 |
| ETF | VT | Vanguard Total World Stock | 0.07 (verify) |
| ETF | ACWI | iShares MSCI ACWI | 0.32 |
| ETF | BND | Vanguard Total Bond Market | 0.03 |
| ETF | AGG | iShares Core US Aggregate Bond | 0.03 |

*Trap:* VOO/VTI are now ~30–35% mega-cap tech by weight. Someone who "only owns
the index" plus an AI sleeve is more concentrated in the AI complex than they
think — run `analyze` on the combination before agreeing they're diversified.
VT and ACWI hold nearly the same universe; ACWI costs ~25bps more.

---

## Cross-theme correlation warning

These are, in practice, **one cluster** and should be sized as one bet:
AI/semis · AI memory · cloud/software · cybersecurity · robotics · large-cap
growth · (often) Taiwan/Korea-heavy EM.

Genuine diversifiers from that cluster, in rough order of independence:
gold bullion · short government paper (home currency) · energy/oil ·
defence · healthcare mega-cap.

And check it with `wm.py regimes` before you believe it — most of these
correlations converge toward 1 in a liquidity crisis.
