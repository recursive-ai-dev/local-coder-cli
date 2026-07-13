# Performance Audit — local-cli

Read-only audit of `local_coder.py`. Findings ranked by realistic impact (frequency × cost). Every item names the concrete wasteful mechanism with line evidence and a directly implementable fix. No files were modified.

---

## 1. Markdown re-parse of the entire response on every streaming token (O(n²) parse)

- **Files / lines:** `local_coder.py:640-642` (`run_single_prompt_async` `StreamMessage.update_content`) and `local_coder.py:675-677` (`run_agent_loop_async` `StreamMessage.update_content`).
- **Mechanism:** On every streamed chunk the code does `self.content += chunk` and then `self.update(Panel(Markdown(self.content), ...))`. `Markdown(...)` fully re-parses the *entire accumulated* content from scratch, and a brand-new `Panel` is constructed, on **every chunk**. With `n` chunks the total work is Σ O(length_so_far) = **O(n²)** in total response length, and the dominant cost is the markdown parse (far heavier than string copy). Each chunk also crosses the thread boundary via `call_from_thread(...)`.
- **Why it matters:** This runs on the hot path of *every* assistant response, in both single-prompt and agent mode. For code-generation outputs (hundreds–thousands of chunks) the quadratic re-parse is the single most expensive thing happening during streaming and directly stalls the UI thread repaint.
- **Fix:** Accumulate text but throttle rendering — only rebuild the Markdown/Panel at a fixed cadence (e.g. every ~100 ms or every N chunks), or keep a single `Markdown` widget and call its `update` with the *new* text rather than re-parsing from zero. Move the `self.content += chunk` out of the render path; parse-once-per-frame, not parse-per-token.
- **Overlaps:** Same `StreamMessage` instances and streaming loop as item 2; fix together.

---

## 2. `assistant_response += content` string concatenation in streaming loops (O(n²))

- **Files / lines:** `local_coder.py:270` (`run_agent_loop`), `:318` (`run_single_prompt`), `:650` (`run_single_prompt_async`), `:692` (`run_agent_loop_async`).
- **Mechanism:** Python strings are immutable, so `assistant_response += content` inside a chunk loop allocates and copies the whole accumulated string on every iteration. Total cost is **O(n²)** in the total response length (n = number of chunks). This is independent of and additive to item 1.
- **Why it matters:** Same hot path as item 1 — every assistant response. For long generations (agent producing large patches/files) the copy overhead is real and compounds with the markdown re-parse.
- **Fix:** Collect chunks in a `list` and join once at the end: `parts.append(content)` … `assistant_response = "".join(parts)`.
- **Overlaps:** Item 1 (same loops/widgets); item 3 (the accumulated string is what gets re-sent).

---

## 3. Full conversation + all tool outputs re-sent to the LLM on every agent iteration (unbounded payload)

- **Files / lines:** `local_coder.py:284,300` (non-TUI `run_agent_loop`) and `:698,715` (`run_agent_loop_async`); the growing `messages` list is fed to `chat.completions.create` at `:261`, `:309`, `:627`, `:682`.
- **Mechanism:** Each agent step appends the *entire* assistant reply and a user message containing *every* tool result (including full file contents from `read_file`/`list_dir`) to `messages`, then re-sends the whole list on the next request. There is no trimming, summarization, or retention policy, so the per-request payload grows linearly with each iteration and re-transmits previously-sent bytes (including whole files) every time.
- **Why it matters:** LLM calls are the dominant cost/latency of this tool, and token/transfer cost scales with `iterations × accumulated_bytes`. In agent mode (`--max-iterations`, default 10) a few file reads quickly bloat every subsequent request; large files make each call markedly slower and more expensive. This is the highest *cost-per-occurrence* item.
- **Fix:** After a tool result has been consumed, replace its verbatim content in older turns with a short placeholder (e.g. keep the result for the immediately following turn, then truncate to a one-line summary), or keep file contents out of the rolling context and re-read on demand. At minimum cap total context and drop/summarize the oldest tool-result turns.
- **Overlaps:** Item 2 supplies the strings; distinct from items 1/2 (this is request payload, not UI rendering).

---

## 4. Chat history accumulates an unbounded number of mounted widgets (no virtualization)

- **Files / lines:** `local_coder.py:573,588,597,612,615,659,667,679,704,712,719` — repeated `self.chat_history.mount(ChatMessage(...))` into the `VerticalScroll` `#chat-history` (composed at `:545`); `ChatMessage.render()` at `:510-516` rebuilds `Panel`/`Markdown` on every repaint.
- **Mechanism:** Every user turn, assistant turn, tool result, and status line appends a new widget to `chat-history` and is never removed (except `/clear`). Textual keeps all widgets mounted and re-renders the scroll container; `render()` re-runs for each on every screen refresh. Widget count grows unbounded across a session.
- **Why it matters:** Long interactive sessions (many turns / agent steps) accumulate dozens–hundreds of widgets, increasing memory and per-refresh render cost with no cap or windowing. This is the "large list rendered without virtualization" case.
- **Fix:** Cap retained widgets (remove the oldest beyond N, or coalesce consecutive system/tool messages), or render the history from a bounded data model instead of one widget per message.
- **Overlaps:** Shares `ChatMessage.render` with item 1's streaming widget; otherwise separate region.

---

## 5. `tool_patch_file` performs redundant full-file scans (`in` + `count` + `replace`)

- **Files / lines:** `local_coder.py:139-156`, specifically `:141` (`search in original_content`) then `:142` (`original_content.count(search)`), and `:151` (`normalized_search in normalized_original`) then `:152` (`normalized_original.count(...)`).
- **Mechanism:** For each patch the code does a linear `in` membership scan and then a *second* full linear `count()` scan of the same file content, followed by `replace`. The `count` is computed even in the common single-match case and duplicates work the `in`/scan already did. Two O(n) passes over the whole file where one (`str.find` for first occurrence + a second `find` from that offset) suffices.
- **Why it matters:** Runs on every `patch_file` tool call; source files in a coding agent can be large (tens–hundreds of KB), so the redundant scan is real, though lower-frequency than items 1–3.
- **Fix:** Locate the first match with `idx = original_content.find(search)`; if it equals `-1`, try the normalized variant; then check for a *second* occurrence with `original_content.find(search, idx + len(search)) == -1` instead of `count()`. One scan instead of two/three.
- **Overlaps:** None of the above; isolated tool function.

---

## 6. `detect_provider` issues two network probes sequentially (minor, startup-only)

- **Files / lines:** `local_coder.py:329-347`.
- **Mechanism:** Two `httpx.Client.get(...)` probes (LM Studio `:333`, llama.cpp `:341`) run **sequentially** with `timeout=1.0` each. They are independent and could be issued concurrently (e.g. `httpx` async/`concurrent.futures`) to cap total probe time at ~1s instead of up to ~2s.
- **Why it matters:** Low — runs once at startup and connection-refused returns immediately, so the 1s timeout rarely elapses. Included for completeness as a "sequential network calls" candidate; not worth prioritizing.
- **Fix:** Fire both probes concurrently and take the first success.
- **Overlaps:** None.

---

### Observations (not fixes — would need infra/dependency change)
- `local_coder.py:494` `atexit.register(lambda: proc.terminate())` registers a new handler (capturing `proc`) on every server start; handlers accumulate across repeated `/serve` uses. Low impact in practice (one server typical) but unbounded over the process lifetime.
- No external cache layer / batch API exists for the filesystem tools, so the items above are all in-process fixes; introducing batching would be an infra change and is out of scope per the audit constraints.
