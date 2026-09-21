# Open-source decisions

Inspected upstream repositories and primary documentation on 2026-09-20.
SQLite and the official MCP Python SDK provide storage and transport.
The integrated financial engine uses NumPy, pandas, SciPy, statsmodels, yfinance,
openpyxl, and pypdf. Runtime dependencies are installed together.
No code from a memory framework or optimizer was copied in this change.

| Project | Upstream license inspected | Decision and reason |
| --- | --- | --- |
| [SQLite](https://www.sqlite.org/copyright.html) | Public domain | Adopt now. Transactions, client scoping, correction history, portable local storage. The database owns truth. |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | MIT | Adopt at the boundary. Pin the v2 major and lock dependencies; test actual stdio protocol calls, not only Python functions. |
| [Graphiti](https://github.com/getzep/graphiti) | [Apache-2.0](https://raw.githubusercontent.com/getzep/graphiti/main/LICENSE) | Borrow the temporal provenance idea. An optional derived recall index later, evaluated against deterministic retrieval. Do not add its LLM, embeddings and graph runtime to installation now. |
| [Mem0](https://github.com/mem0ai/mem0) | [Apache-2.0](https://raw.githubusercontent.com/mem0ai/mem0/main/LICENSE) | Optional recall adapter only after measured benefit. Extracted memories do not own balances, dates, corrections or permission. |
| [Letta Code](https://github.com/letta-ai/letta-code) | [Apache-2.0 with branding exclusions](https://raw.githubusercontent.com/letta-ai/letta-code/main/LICENSE) | Inspiration for tiered memory and compact working context. The user already has an agent harness; embedding another one would change the product. |
| [skfolio](https://github.com/skfolio/skfolio) | [BSD-3-Clause](https://raw.githubusercontent.com/skfolio/skfolio/main/LICENSE) | Preferred candidate for a future risk/construction adapter. First demonstrate parity, stable constraints, sensitivity and walk-forward evaluation against a simple baseline. Not installed or adopted yet. |
| [PyPortfolioOpt](https://github.com/PyPortfolio/PyPortfolioOpt) | [MIT](https://raw.githubusercontent.com/PyPortfolio/PyPortfolioOpt/main/LICENSE) | Potential reference implementation for method comparisons. Do not maintain two overlapping optimization stacks by default. |
| [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | [BSD-3-Clause](https://raw.githubusercontent.com/dcajasn/Riskfolio-Lib/master/LICENSE.txt) | Optional specialist adapter only when a specific missing method justifies its dependency/solver footprint. |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB) | [AGPL-3.0](https://raw.githubusercontent.com/OpenBB-finance/OpenBB/develop/LICENSE) | Potential data integration subject to a deliberate code-license and provider-rights review. Not bundled. Running it separately does not itself establish license compliance. |

## Rules for reuse

1. The source of truth is explicit financial evidence, not an LLM's extracted
   memory. Semantic indexes can be rebuilt from it and may retrieve evidence;
   they cannot silently revise it.
2. A library's software license does not establish rights to market data.
   Provider contracts independently determine retrieval, caching, redistribution
   and display. Existing free-data prototypes are not a commercial data plan.
3. Keep dependencies behind narrow adapters and immutable input/output records.
   The ability to reproduce and explain a calculation matters more than how many
   optimization methods the package advertises.
4. Adopt a dependency when it beats the current implementation in a named
   acceptance test. Popularity is not an integration requirement.
5. Context is selective. A general company question should not leak a client's
   full wealth history to a research provider. A future remote server needs real
   identity/authorization; local client IDs alone are not multi-tenant security.

## Current boundary

The local core uses current facts plus preserved revision history. It is not a
full bitemporal database or a semantic knowledge graph. Recall combines bounded lexical/concept matching with optional cosine ranking
of host-supplied embeddings. Wealth does not call an embedding provider. Decision invalidation is
conservative at the client-revision level. More precise dependency invalidation
and historical state reconstruction need separate implementations and tests.
