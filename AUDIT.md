# Code Quality Audit: local-cli

## 1. Deadlock in Subprocess Pipes (Resilience)
**Location:** `local_coder.py` lines 447-451
**Problem:** `subprocess.Popen` in `serve_local_model` captures `stdout` and `stderr` as pipes (`subprocess.PIPE`), but the code never reads from or drains them.
**Why it matters:** Once the OS pipe buffer (typically 64KB) fills up with `llama-server` background logs, the subprocess will block indefinitely on I/O. This causes the server to hang silently and stop responding to API requests.
**Proposed Fix:** Redirect the output to `subprocess.DEVNULL` (`stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL`) to discard the logs, or launch a background thread to continually read lines from the pipes.

## 2. Race Condition on Concurrent Submissions (Correctness Risk)
**Location:** `local_coder.py` lines 532, 576, 619
**Problem:** `on_input_submitted` checks `if self.is_processing: return` synchronously, but the flag is set to `True` asynchronously inside the worker threads (`run_single_prompt_async` and `run_agent_loop_async`).
**Why it matters:** If a user presses Enter rapidly or submits commands back-to-back, multiple background threads will spawn before the flag is set, leading to concurrent mutations of the `self.messages` list and overlapping API calls.
**Proposed Fix:** Move `self.is_processing = True` to execute synchronously inside `on_input_submitted` immediately before calling the async worker thread methods.

## 3. TUI Main Thread Starvation (Redundant Work)
**Location:** `local_coder.py` lines 608-609, 649-650
**Problem:** Inside the token streaming loop (`for chunk in response:`), `call_from_thread(stream_msg.update_content, content)` and `call_from_thread(self.chat_history.scroll_end, animate=False)` are triggered for *every single token*.
**Why it matters:** Textual processes `call_from_thread` on the main async event loop. Flooding the loop with hundreds of UI layout and scroll recalculations per second starves the app, causing severe visual stuttering and high CPU usage.
**Proposed Fix:** Throttle UI updates (e.g., buffer tokens and dispatch every ~50ms). At a minimum, remove the per-token `scroll_end()` call and only trigger it periodically (e.g., when a newline is encountered) or after the stream completes.
*Note: This overlaps identically across `run_single_prompt_async` and `run_agent_loop_async`.*

## 4. Incomplete Regex for Tool Parsing (Resilience)
**Location:** `local_coder.py` lines 203-207
**Problem:** The regex strings, such as `r"<write_file\s+path=([\"']?)(.*?)\1>(.*?)</write_file>"`, strictly mandate that `>` appears immediately after the path attribute string/quote.
**Why it matters:** LLMs frequently inject trailing spaces (`<write_file path="x.py" >`) or additional XML attributes (like `lang="python"`). If they do, the regex silently fails to parse the block, entirely ignoring the LLM's intended action.
**Proposed Fix:** Relax the closing bracket match by allowing optional spaces and attributes before the `>`: `r"<write_file\s+path=([\"']?)(.*?)\1[^>]*>(.*?)</write_file>"`.
*Note: This must be fixed for both the `write_file` and `patch_file` regex patterns.*

## 5. Stream Chunk IndexError (Real Bug)
**Location:** `local_coder.py` lines 263, 311, 605, 646
**Problem:** The streaming loops unconditionally access `chunk.choices[0].delta.content` without verifying that the `choices` list is populated.
**Why it matters:** OpenAI-compatible APIs (especially local servers) occasionally emit chunks with an empty `choices` array (e.g., keep-alive pings or final termination chunks). This triggers an immediate `IndexError` that crashes the entire LLM loop.
**Proposed Fix:** Add a bounds check: `if chunk.choices and chunk.choices[0].delta.content:`.
*Note: This overlaps across four separate functions handling LLM streaming.*

## 6. Indentation Drift in Patch Tool (Correctness Risk)
**Location:** `local_coder.py` lines 145-149
**Problem:** `tool_patch_file` normalizes the search block by using `.strip()`, removing all leading spaces from the first line. However, the `original_content` is not stripped. The `replace()` call matches the unindented string but leaves the original leading spaces in place.
**Why it matters:** If the LLM generates a replacement block that correctly includes the standard leading indentation, the patch merges them, duplicating the indentation on the first line (e.g., `    ` + `    def func():`), breaking Python's indentation rules.
**Proposed Fix:** Replace `.strip()` with `.strip("\r\n")` to strictly preserve horizontal indentation on the first and last lines while still removing leading/trailing empty lines.
*Note: Overlaps with Item 7 below.*

## 7. Missing Multi-Match Validation in Patch Tool (Correctness Risk)
**Location:** `local_coder.py` lines 140-143
**Problem:** `original_content.replace(search, replace, 1)` replaces only the very first occurrence of the search block in the file without verifying uniqueness.
**Why it matters:** If the LLM provides a generic search block (e.g., a simple `return True` or an empty `__init__`), the agent might blindly modify the wrong function, silently introducing hard-to-detect logic bugs.
**Proposed Fix:** Before replacing, check `if original_content.count(search) > 1:`. If true, return an error back to the LLM instructing it to provide a larger, unique search block.
*Note: Overlaps with Item 6 above.*

## 8. Unreachable/Incompatible CLI Commands (Dead Code)
**Location:** `local_coder.py` lines 344-474 vs 554-572
**Problem:** Features like `/models`, `/serve`, and `/help` are fully implemented (e.g., `download_huggingface_model`, `serve_local_model`), but are entirely absent from `handle_slash_command`. Furthermore, these functions use `rich.prompt.Prompt`, which blocks standard input.
**Why it matters:** The code is completely unreachable dead code. Furthermore, naive attempts to wire them into the TUI will fail, because standard `sys.stdin` prompts are fundamentally incompatible with Textual's raw-mode event loop, leading to immediate deadlocks.
**Proposed Fix:** Extract these management workflows into dedicated CLI flags (e.g., `local-coder --download-model`) so they run in the standard terminal before Textual boots, or completely rewrite them as native Textual screens.
