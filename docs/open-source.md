# Dependencies and reuse

The dependency declarations live in [pyproject.toml](../pyproject.toml); exact
resolved versions live in [uv.lock](../uv.lock). Runtime dependencies install
with `uv sync`; development checks use `uv sync --extra dev`.

| Dependency | Role |
| --- | --- |
| SQLite (Python standard library) | Authoritative local facts, revisions and decisions |
| MCP Python SDK | Stdio tool transport |
| NumPy, pandas, SciPy | Data handling, statistics and portfolio calculations |
| statsmodels | Factor regressions |
| yfinance | Explicit live market-data adapter |
| openpyxl, pypdf | Spreadsheet and PDF ingestion |
| Pillow (optional `images` extra) | PNG views for text channels; SVG is written without it |
| pytest, scikit-learn, fpdf2 (development) | Regression checks, method comparisons, generated PDF fixtures |

External services are reached only through explicit adapters, with the
Python standard library's HTTP client: the IBKR Flex Web Service, the Alpaca
Trading API (read-only connector and person-confirmed orders), the Cuenca API,
SEC EDGAR for 13F filings (with a `WEALTH_SEC_USER_AGENT` contact) and,
optionally, OpenFIGI for CUSIP-to-ticker mapping.

Graphiti, Mem0, Letta, skfolio, PyPortfolioOpt, Riskfolio-Lib and OpenBB are not
integrated dependencies. Earlier candidate evaluations are in Git history.
Wealth creates no embeddings: optional semantic retrieval uses compatible
vectors supplied by the host. SQLite remains authoritative.

When reusing code, record its source and preserve the applicable license and
attribution. Software licensing and market-data permissions are separate;
provider terms govern retrieval, caching and redistribution. Free-data access
alone is not evidence of production data rights.

Add a dependency for a demonstrated capability or correctness benefit, with
reproducible checks against the current implementation. Keep external providers
behind explicit adapters and report missing or stale data honestly.
