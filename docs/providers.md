# Model providers

Wealth is an MCP server and deterministic financial library. It makes no LLM
calls, so it does not need an OpenRouter, OpenAI, or Anthropic key. Your existing
assistant provides the model and calls Wealth's six MCP tools.

## OpenRouter as the host's model provider

Use an assistant that supports both an OpenAI-compatible model endpoint and MCP
tools. Configure that assistant with:

- API base URL: `https://openrouter.ai/api/v1`
- API key: `OPENROUTER_API_KEY` in the host's environment or secret store
- Model: an explicit OpenRouter model ID supporting tool use
- MCP server: the `wealth-mcp` command shown in [README](../README.md)

The exact host configuration field names vary. The OpenRouter key belongs in
that host's provider configuration, not the Wealth MCP server configuration.
Wealth does not load `.env` files or read this key. `.env` and private keys are
excluded from this repository; never commit a real key.

See the [official OpenRouter quickstart](https://openrouter.ai/docs/quickstart)
for authentication and the API endpoint. The host sends selected conversation
and tool results to its model provider; keep the client context deliberate.

A standalone chat application would require an additional model/tool loop.
This repository currently supplies the financial capability, not that chat UI.
