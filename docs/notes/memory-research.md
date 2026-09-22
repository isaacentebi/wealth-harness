# "What I know about you": memory research brief (Sept 2026)

Claims marked (blog) come from third-party write-ups and need checking before anyone relies on them.

## 1. Memory UIs

- **ChatGPT** has two layers.
  - **Saved memories** are short third-person sentences ("Is vegetarian"), shown under Settings → Personalization → Manage memories with a trash icon per row. There is also a second, uninspectable "Reference chat history" layer.
  - An inline **"Memory updated"** chip appears in chat and links to the list.
  - Since late 2025 the list manages itself (no "memory full" limit), and you can search, sort by recency and reprioritise entries.
  - No sources are shown. Deleting a chat does not delete its memories.
  - https://openai.com/index/memory-and-new-controls-for-chatgpt/
- **Claude** replaced its 24-hour rolling summary with individual entries, grouped by **topic** and written in real time.
  - Each entry can be edited or deleted, or corrected by saying "remember / forget / change" in chat.
  - Each project has its own memory. Incognito chats are excluded.
  - Sensitive memories are opt-in, and a notice appears above the composer each time one is saved.
  - https://support.claude.com/en/articles/11817273
- **Gemini** "Saved info" is a flat list of user-stated sentences with a ⋮ menu to edit or delete; learned "Personal context" is a separate layer.
  - Anti-pattern: Gemini says "OK, I've saved that" even when the save failed because a toggle was off. https://memx.app/blog/gemini-memory-personal-context-explained/ (blog)
- **Origin** (the closest finance analogue) only saves what you explicitly ask it to.
  - Memories are grouped as **Goals, Upcoming Dates, Life Factors, Income Details, Investments, Properties, Expenses**. You can view, edit and delete them, or switch memory off.
  - https://support.useorigin.com/hc/en-us/articles/42089045600909 (from the search snippet; the page returned 403)
- **Monarch, Copilot Money, Range (Rai), Wealthfront Path:** I found no public "what we know" page for any of them.
  - They personalise from linked accounts (Path models "what you actually do").
  - This is a gap: nobody shows stated facts and observed behaviour side by side.
  - https://www.range.com/blog/introducing-rai-the-first-ai-wealth-advisor
- **Era** holds a finance profile shared over MCP; retracting a fact removes it everywhere.
- **Notion AI** stores memory as ordinary editable pages and instructions. It feels like a document, not a table. (blog)
- **Apple Siri AI** takes personal context from the Spotlight semantic index, with **attribution back to the source app**. https://developer.apple.com/wwdc26/guides/apple-intelligence/
- **Rewind/Limitless** was a raw lifelog, shut down after Meta bought it (Dec 2025). Transcripts are not a memory page.

**Patterns across these products:** facts are sentences, grouped into categories, and editable in place or by chat, with an inline moment when something is saved. None of them shows **source, confidence or "true since"** well. That is the opening.

## 2. Open-source memory systems

- **mem0** stores atomic facts per user/agent/run, with metadata, an `expiration_date` and entity links.
  - The classic pipeline had an LLM choose **ADD/UPDATE/DELETE/NOOP** against similar facts.
  - The V3 algorithm (April 2026) is **ADD-only single-pass**: old and new facts coexist with temporal context. mem0 reports roughly +30 points on temporal questions.
  - Retrieval fuses semantic, BM25 and entity-match scores.
  - https://docs.mem0.ai/migration/platform-v2-to-v3
- **Letta (MemGPT)** pins labelled **memory blocks** (description, value, char limit, read_only) in the prompt, plus archival and recall stores.
  - The agent edits blocks through tools, and sleep-time agents consolidate in the background. The last write wins.
  - https://docs.letta.com/guides/agents/memory-blocks
- **Zep/Graphiti** turns episodes into entities and fact edges.
  - Edges are **bi-temporal**: `valid_at`/`invalid_at` (when the fact was true in the world) and `created_at`/`expired_at` (when the system recorded it).
  - A contradiction **invalidates** the old edge instead of deleting it.
  - Retrieval combines vector, BM25 and graph search with no LLM reranker.
  - https://help.getzep.com/graphiti/getting-started/overview
- **LangMem** splits semantic memory into *collections* (need insert/update/delete reconciliation) and *profiles* (one schema document, overwritten). It also has episodic and procedural memory, and extraction runs either in the hot path or in the background. https://langchain-ai.github.io/langmem/concepts/conceptual_guide/
- **cognee** builds a graph plus vectors, validates entities against an ontology, and can date its edges. `memify` prunes stale nodes and reweights by usage. The API verbs are remember / recall / improve / forget. https://github.com/topoteretes/cognee
- **Memobase** keeps a **topic → subtopic profile** plus an **event timeline**. Profile reads are plain SQL, under 100 ms. https://github.com/memodb-io/memobase
- **claude-mem** (a local checkout) uses SQLite tables (`sessions`, `memories`, `overviews`, `transcript_events`) with an `origin` column, plus Chroma vectors.
  - Hooks capture observations, and the model compresses them into summaries.
  - Its read path is three layers: `search` (index with IDs) → `timeline` (context around a hit) → `get_observations` (full detail).

**Cheap adoptions for the existing SQLite store** (it already has revisions, provenance, confidence, review dates and append-only history):
1. Add `valid_from`/`valid_to` to each fact revision, alongside the existing recorded/superseded times. Supersede, never overwrite.
2. Log every extraction decision: ADD / UPDATE / SUPERSEDE / NOOP / CONFLICT, with the reason and the source span.
3. Contradictions of *user-stated or confirmed* facts open a `pending_conflict` for the user. Only *inferred* facts are auto-superseded.
4. Split the page into a **profile** (current facts) and a **timeline** (changes and life events), as Memobase does.
5. Add an entity table (people, accounts, properties, employers) that facts link to. No graph database needed.
6. Search with FTS5 plus optional sqlite-vec, fusing the scores, and read through an index-then-detail path.

## 3. Recommendations (in priority order)

1. **[UI]** Write each fact as a sentence in the user's language, with the value emphasised. Render it from a per-kind template, with the structured value kept underneath.
2. **[Pipeline]** Bi-temporal validity and supersession, shown as "since March 2026" and "was $62k until Feb".
3. **[Pipeline+UI]** Contradiction cards with three choices: **Keep mine / Use the statement / It changed**.
4. **[UI]** Group facts by life area: Income & work · Home · Family · Goals · Investments · Taxes (US/MX) · Estate · Preferences. Put a one-line human summary at the top of each group.
5. **[UI]** A quiet provenance chip on each fact ("You told me · 12 Mar", "From your Banorte statement", "My guess"). Tapping it opens the source.
6. **[UI]** Confidence in tone, not percentages. Inferred facts are italic, with a one-tap "Yes, that's right" that makes them confirmed.
7. **[UI]** A "Memory updated" chip in chat that names the exact change, with Undo and Edit. Show it only after a real commit.
8. **[UI]** A readable timeline of the append-only history. Forgotten facts appear as greyed tombstones, so the user can see deletes happened.
9. **[Pipeline+UI]** Review dates become gentle check-ins in a "Worth a quick check" tray, three at most.
10. **[UI]** Each fact is inline-editable, and a "Tell me what changed" box routes through the same pipeline. Forget is one tap, with an undo toast.
11. **[UI]** Empty state: a warm prompt, three starter questions and "or upload a statement". Each group appears once its first fact exists.
12. **[Pipeline]** Keep sensitive facts in an opt-in store. Show "Patterns I've noticed" from transactions apart from what the user told the assistant.

### Phrasing examples

The **bold** marks the value the page should emphasise.

- EN: "You take home **$85,000 MXN a month** from Grupo X, *since March 2026*."
  ES-MX: "Ganas **$85,000 al mes** netos en Grupo X, *desde marzo de 2026*."
- EN: "Your rent is **$18,500 a month**, paid on the 5th."
  ES-MX: "Pagas **$18,500 de renta** al mes; te toca el día 5."
- EN: "You want **$400,000 for a down payment** by **December 2027**."
  ES-MX: "Quieres juntar **$400,000 para el enganche** antes de **diciembre de 2027**."
- Inferred, ES-MX: "*Parece* que le mandas unos **$6,000 al mes** a tu mamá. ¿Es correcto?"
