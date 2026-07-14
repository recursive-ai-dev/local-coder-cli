# Code Quality Audit: local-cli

## 1. Silent Corrupted File Encodings & Permissions (Correctness Risk)
- **Files and lines:** `local_coder.py` lines 124-132 (`tool_write_file`) and 149-164 (`tool_patch_file`)
- **Problem:** Files are unconditionally written back using `encoding="utf-8"`, ignoring the original file's encoding (e.g., latin-1, utf-16), and written without preserving original file permissions (`chmod`).
- **Why it matters:** Editing a shell script strips its executable flag (`+x`). Editing a non-UTF-8 file permanently destroys its encoding. Both break the user's environment silently.
- **Proposed Fix:** Capture permissions with `os.stat(safe_path).st_mode` before patching/writing, and re-apply with `os.chmod` after writing. Ideally, use a safe encoding fallback mechanism.
*Note: Overlaps with Item 6 in `tool_patch_file`.*

## 2. UI Event Loop Starvation on File Download (Resilience / Redundant Work)
- **Files and lines:** `local_coder.py` lines 520-524 (`DownloadModelScreen.do_download`)
- **Problem:** Issues a `self.app.call_from_thread` to update the progress bar for *every single 8KB chunk* downloaded over the network.
- **Why it matters:** For a typical 4GB model (500,000+ chunks), this queues half a million UI thread updates in rapid succession. Textual's event loop will saturate entirely, locking the UI, maxing a CPU core, and making the "Cancel" button unresponsive.
- **Proposed Fix:** Throttle the UI update. Add `last_update = time.time()` and only call `call_from_thread` if `time.time() - last_update > 0.1` seconds.

## 3. Incomplete Partial Downloads Break Server (Resilience)
- **Files and lines:** `local_coder.py` lines 508-531 (`DownloadModelScreen.do_download`)
- **Problem:** If a network exception occurs during `iter_bytes()` (e.g. timeout), the `except Exception as e:` block updates the status but leaves the corrupted, partially written `.gguf` file on disk.
- **Why it matters:** The UI will list the partial file as a valid, downloaded model. When the user tries to serve it, `llama-server` will crash or segfault on load, giving an obscure error and rendering the CLI functionally broken for that model.
- **Proposed Fix:** In the exception handler, add `if destination_file.exists(): destination_file.unlink()` to clean up partial downloads.

## 4. Unreaped Zombie Subprocesses (Real Bug)
- **Files and lines:** `local_coder.py` lines 614-620 (`ServeModelScreen.stop_server`)
- **Problem:** Stops the local LLM subprocess using `self.proc.terminate()` but never calls `self.proc.wait()`.
- **Why it matters:** On UNIX-like systems, un-`wait`ed terminated child processes become "zombies" (defunct) in the OS process table. Rapidly starting and stopping the server will leak process IDs and clutter the process tree until the main Python app exits.
- **Proposed Fix:** Add `self.proc.wait(timeout=2)` immediately after `self.proc.terminate()`.

## 5. OOM / Decode Crash on Binary File Reads (Resilience)
- **Files and lines:** `local_coder.py` lines 112-122 (`tool_read_file`)
- **Problem:** Reads any requested file blindly into memory via `safe_path.read_text(encoding="utf-8")` without checking file type or size.
- **Why it matters:** If the LLM hallucinates and requests to read a 1GB `.gguf` file or an image, the CLI will either throw an unhandled `UnicodeDecodeError` or crash the agent completely via an `OutOfMemoryError`.
- **Proposed Fix:** Check `safe_path.stat().st_size` against a limit (e.g., `1024 * 500` bytes) before reading, and wrap the read in a `try...except UnicodeDecodeError:` returning a specific tool error string.

## 6. Accidental Windows CRLF Destruction (Correctness Risk)
- **Files and lines:** `local_coder.py` lines 153-164 (`tool_patch_file`)
- **Problem:** When applying the normalized whitespace patch fallback, `original_content.replace("\r\n", "\n")` strips Windows line endings. The modified string is then written directly to disk.
- **Why it matters:** Silently overwrites the entire file with Unix (LF) line endings. In a Windows codebase, this creates massive Git diff noise on unmodified lines and violates local formatting rules.
- **Proposed Fix:** Detect if `\r\n` is in `original_content`. If true, apply `.replace("\n", "\r\n")` to `new_content` just before saving to `safe_path.write_text`.
*Note: Overlaps with Item 1 in `tool_patch_file`.*

## 7. Unbounded Context Window Growth Crash (Resilience)
- **Files and lines:** `local_coder.py` lines 783-821 (`run_agent_loop_async`)
- **Problem:** User prompts and assistant responses are appended to `self.messages` infinitely. Tool *results* are compacted, but the conversational turns are never dropped.
- **Why it matters:** In a long agent session (e.g., `--max-iterations 30`), the history will eventually exceed the local LLM's absolute context limit (e.g., 8192 tokens). The API will return an unrecoverable HTTP 400 error, breaking the agent loop permanently.
- **Proposed Fix:** Implement a rolling context window. Trim the oldest user/assistant message pairs (while preserving the `system` prompt at index 0) once the array length exceeds a safe threshold (e.g., 20 messages).
*Note: Overlaps with Item 13.*

## 8. Missing Timeout on LLM Network Calls (Resilience)
- **Files and lines:** `local_coder.py` lines 304-309 (`stream_completion`)
- **Problem:** The `client.chat.completions.create()` call specifies no `timeout`.
- **Why it matters:** Local LLM servers (like LM Studio or `llama.cpp`) often hang or lock up under heavy VRAM swapping. Without a timeout, the background worker blocks forever, permanently freezing the TUI in the `is_processing = True` state (locking the input box).
- **Proposed Fix:** Pass a reasonable explicit timeout (e.g., `timeout=120.0`) to the `.create()` call to guarantee control flow returns and the UI can report the network failure.
*Note: Overlaps with Item 11 in `stream_completion`.*

## 9. Strict XML Regex Parsing Fails on Valid Spaces (Correctness Risk)
- **Files and lines:** `local_coder.py` lines 211-222 (`parse_and_execute_tools`)
- **Problem:** The regex matches mandate exact closing tags without spaces (e.g., `</read_file>`).
- **Why it matters:** LLMs frequently inject arbitrary spaces (e.g., `</read_file >`). When this occurs, the parser silently ignores the tool call. The LLM then loops, confused why the file contents were not returned, burning iterations.
- **Proposed Fix:** Relax the four closing tag regexes to tolerate optional whitespace before the closing bracket: `</read_file\s*>`, `</patch_file\s*>`, etc.

## 10. Fragile Tool Result Compaction via Prefix Matching (Correctness Risk)
- **Files and lines:** `local_coder.py` lines 264-278 (`compact_old_tool_results`)
- **Problem:** Compaction identifies previous tool results merely by checking if the user's string starts with `TOOL_RESULT_PREFIX` ("### Execution result of ").
- **Why it matters:** If the user manually types or copies a block starting with that exact phrase, the compactor will irreversibly destroy their prompt data, replacing it with a placeholder.
- **Proposed Fix:** Instead of raw string checking, inject a hidden dictionary key (e.g., `{"role": "user", "content": ..., "is_tool_result": True}`) when appending tool results, and check this boolean during the compaction phase.

## 11. Quadratic String Allocation in Streaming Loop (Redundant Work)
- **Files and lines:** `local_coder.py` lines 315-316 (`stream_completion`)
- **Problem:** Inside the streaming `for` loop, `"".join(parts)` recreates the entire accumulated string from scratch on every throttle tick (or every newline).
- **Why it matters:** For large code generation (thousands of chunks), joining an expanding array hundreds of times incurs completely unnecessary O(N²) string memory allocation and copying, creating CPU spikes on the background thread that degrade stream fluidity.
- **Proposed Fix:** Maintain a `full_text = ""` accumulator, use `full_text += content` on each iteration, and pass `full_text` to the `on_update` callback.
*Note: Overlaps with Item 8 in `stream_completion`.*

## 12. Directory Listing Token Bloat Crash (Resilience)
- **Files and lines:** `local_coder.py` lines 100-108 (`tool_list_dir`)
- **Problem:** Indiscriminately maps over `iterdir()` and returns a string with every single file in the requested directory.
- **Why it matters:** If the agent investigates a dense directory (like `node_modules` or `.venv`), it generates a massive text blob (tens of thousands of lines). This instantly blows out the LLM's token limit and crashes the agent.
- **Proposed Fix:** Limit the output array `entries` to a maximum size (e.g., 200 items), and if the directory is larger, truncate the list and append a footer like `... [N more items hidden]`.

## 13. Layout Race Condition on Scroll (Correctness Risk)
- **Files and lines:** `local_coder.py` lines 715-716 & 814-815 (`run_agent_loop_async`)
- **Problem:** `self.chat_history.scroll_end(animate=False)` is invoked synchronously immediately after `self.chat_history.mount()`.
- **Why it matters:** Textual computes new layout dimensions asynchronously. A synchronous scroll will often trigger before the newly mounted widget's height is fully resolved, leaving the scroll position stranded above the new text and forcing the user to manually scroll down.
- **Proposed Fix:** Wrap the scroll call to delay until the next frame: `self.call_after_refresh(self.chat_history.scroll_end, animate=False)`.
*Note: Overlaps with Item 7 in `run_agent_loop_async`.*
