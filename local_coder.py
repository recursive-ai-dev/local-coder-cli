#!/usr/bin/env python3
import os
import time
import difflib
import sys
import re
import argparse
import subprocess
import atexit
import socket
import concurrent.futures
from pathlib import Path
import httpx
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.live import Live
from rich.table import Table
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.align import Align

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll, Vertical, Horizontal
from textual.widgets import Header, Footer, Static, Input, Button, Select, ProgressBar, Label
from textual.screen import ModalScreen
from textual import work


console = Console()
TUI_MODE = False

MODELS_PRESETS = {
    "1": {
        "name": "Llama 3.1 8B Instruct",
        "repo": "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "quants": {
            "Q4_K_M": "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
            "Q5_K_M": "Meta-Llama-3.1-8B-Instruct-Q5_K_M.gguf",
            "Q8_0": "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"
        }
    },
    "2": {
        "name": "Qwen 2.5 7B Instruct",
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "quants": {
            "Q4_K_M": "qwen2.5-7b-instruct-q4_k_m.gguf",
            "Q5_K_M": "qwen2.5-7b-instruct-q5_k_m.gguf",
            "Q8_0": "qwen2.5-7b-instruct-q8_0.gguf"
        }
    },
    "3": {
        "name": "Mistral 7B Instruct v0.3",
        "repo": "MaziyarPanahi/Mistral-7B-Instruct-v0.3-GGUF",
        "quants": {
            "Q4_K_M": "Mistral-7B-Instruct-v0.3.Q4_K_M.gguf",
            "Q5_K_M": "Mistral-7B-Instruct-v0.3.Q5_K_M.gguf",
            "Q8_0": "Mistral-7B-Instruct-v0.3.Q8_0.gguf"
        }
    },
    "4": {
        "name": "Phi-3.5 Mini Instruct (3.8B)",
        "repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "quants": {
            "Q4_K_M": "Phi-3.5-mini-instruct-Q4_K_M.gguf",
            "Q5_K_M": "Phi-3.5-mini-instruct-Q5_K_M.gguf",
            "Q8_0": "Phi-3.5-mini-instruct-Q8_0.gguf"
        }
    }
}

def get_models_dir() -> Path:
    if getattr(sys, 'frozen', False):
        m_dir = Path(sys.executable).parent / "models"
    else:
        m_dir = Path(__file__).parent / "models"
    m_dir.mkdir(parents=True, exist_ok=True)
    return m_dir

def get_safe_path(target_dir: Path, subpath_str: str) -> Path:
    """Resolve subpath safely, ensuring it is within the target directory."""
    target_dir = target_dir.resolve()
    subpath_str = subpath_str.strip()
    if subpath_str.startswith("/") or Path(subpath_str).is_absolute():
        raise ValueError(f"Security error: Absolute path '{subpath_str}' is not allowed.")
    
    resolved = (target_dir / subpath_str).resolve()
    if not resolved.is_relative_to(target_dir):
        raise ValueError(f"Security error: Path '{subpath_str}' escapes target directory '{target_dir}'")
    return resolved

def tool_list_dir(target_dir: Path, path: str) -> str:
    try:
        target_dir = target_dir.resolve()
        safe_path = get_safe_path(target_dir, path)
        if not safe_path.exists():
            return f"Error: Path '{path}' does not exist."
        if not safe_path.is_dir():
            return f"Error: Path '{path}' is a file, not a directory."
        
        entries = []
        for entry in safe_path.iterdir():
            rel_path = entry.relative_to(target_dir)
            prefix = "[DIR] " if entry.is_dir() else "[FILE]"
            entries.append(f"{prefix} {rel_path}")
        
        if not entries:
            return "Directory is empty."

        entries = sorted(entries)
        if len(entries) > 200:
            hidden = len(entries) - 200
            entries = entries[:200]
            entries.append(f"... [{hidden} more items hidden]")

        return "\n".join(entries)
    except Exception as e:
        return f"Error listing directory: {str(e)}"

def tool_read_file(target_dir: Path, path: str) -> str:
    try:
        target_dir = target_dir.resolve()
        safe_path = get_safe_path(target_dir, path)
        if not safe_path.exists():
            return f"Error: File '{path}' does not exist."
        if not safe_path.is_file():
            return f"Error: Path '{path}' is not a file."

        file_size = safe_path.stat().st_size
        if file_size > 500 * 1024:
            return f"Error: File '{path}' is too large to read ({file_size} bytes). Maximum allowed size is 500KB."

        try:
            return safe_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: File '{path}' is not a valid UTF-8 text file (could be binary)."
    except Exception as e:
        return f"Error reading file: {str(e)}"

def tool_write_file(target_dir: Path, path: str, content: str) -> str:
    try:
        target_dir = target_dir.resolve()
        safe_path = get_safe_path(target_dir, path)

        orig_mode = None
        if safe_path.exists():
            orig_mode = os.stat(safe_path).st_mode

        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content, encoding="utf-8")

        if orig_mode is not None:
            os.chmod(safe_path, orig_mode)

        return f"Successfully wrote {len(content)} bytes to '{path}'."
    except Exception as e:
        return f"Error writing file: {str(e)}"

def tool_patch_file(target_dir: Path, path: str, search: str, replace: str) -> str:
    try:
        target_dir = target_dir.resolve()
        safe_path = get_safe_path(target_dir, path)
        if not safe_path.exists():
            return f"Error: File '{path}' does not exist."

        original_content = safe_path.read_text(encoding="utf-8")
        orig_mode = os.stat(safe_path).st_mode
        has_crlf = "\r\n" in original_content

        def write_patch(new_content, message):
            if has_crlf:
                new_content = new_content.replace("\n", "\r\n")
            safe_path.write_text(new_content, encoding="utf-8")
            os.chmod(safe_path, orig_mode)

            diff_lines = list(difflib.unified_diff(
                original_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=path,
                tofile=path
            ))
            diff_text = "".join(diff_lines)
            if diff_text:
                message += f"\n\n```diff\n{diff_text}```"
            return message

        idx = original_content.find(search)
        if idx != -1:
            second_idx = original_content.find(search, idx + len(search))
            if second_idx != -1:
                return f"Error: Search block found multiple times in '{path}'. Please provide a larger, unique search block."
            new_content = original_content[:idx] + replace + original_content[idx + len(search):]
            return write_patch(new_content, f"Successfully applied patch to '{path}'.")

        normalized_search = search.replace("\r\n", "\n").strip("\r\n")
        normalized_original = original_content.replace("\r\n", "\n")

        idx = normalized_original.find(normalized_search)
        if idx != -1:
            second_idx = normalized_original.find(normalized_search, idx + len(normalized_search))
            if second_idx != -1:
                return f"Error: Search block found multiple times in '{path}'. Please provide a larger, unique search block."
            normalized_replace = replace.replace("\r\n", "\n")
            new_content = normalized_original[:idx] + normalized_replace + normalized_original[idx + len(normalized_search):]
            return write_patch(new_content, f"Successfully applied patch (normalized whitespace) to '{path}'.")

        return f"Error: Could not find exact search block in '{path}'. Please ensure the search block is identical, including indentation."
    except Exception as e:
        return f"Error patching file: {str(e)}"

def format_and_print_tool_call(tool_name: str, args_info: str, result: str):
    if TUI_MODE: return
    """Print the tool execution beautifully in the console."""
    console.print(Panel(f"[bold yellow]Executed Tool:[/bold yellow] [cyan]{tool_name}[/cyan]\n[bold]Arguments:[/bold] {args_info}", border_style="yellow", title="Tool Invocation"))
    
    if tool_name == "list_dir":
        table = Table(title=f"Files in target directory", show_header=True, header_style="bold magenta")
        table.add_column("Type", style="dim", width=8)
        table.add_column("Path", style="blue")
        for line in result.splitlines():
            if line.startswith("[DIR]"):
                table.add_row("DIR", line.replace("[DIR] ", ""))
            elif line.startswith("[FILE]"):
                table.add_row("FILE", line.replace("[FILE] ", ""))
            else:
                table.add_row("", line)
        console.print(table)
    elif tool_name in ["read_file", "write_file", "patch_file"]:
        lexer = "python"
        if tool_name == "patch_file":
            lexer = "diff"
        elif args_info.endswith(".json"): lexer = "json"
        elif args_info.endswith(".md"): lexer = "markdown"
        elif args_info.endswith(".html"): lexer = "html"
        elif args_info.endswith(".css"): lexer = "css"
        elif args_info.endswith(".js"): lexer = "javascript"
        
        lines = result.splitlines()
        preview_limit = 35
        if len(lines) > preview_limit:
            preview_content = "\n".join(lines[:preview_limit]) + f"\n... [dim]({len(lines) - preview_limit} lines hidden)[/dim]"
        else:
            preview_content = result
            
        line_numbers = tool_name != "patch_file"
        console.print(Panel(Syntax(preview_content, lexer, theme="monokai", line_numbers=line_numbers), title=f"File Content: {args_info}", border_style="green"))
    else:
        console.print(Panel(result, border_style="cyan", title="Tool Result"))

def parse_and_execute_tools(target_dir: Path, text: str) -> list[dict]:
    """Parse XML tags in response and execute tools in the order they appear."""
    matches = []
    
    for m in re.finditer(r"<list_dir>(.*?)</list_dir\s*>", text, re.DOTALL):
        matches.append((m.start(), "list_dir", m))
        
    for m in re.finditer(r"<read_file>(.*?)</read_file\s*>", text, re.DOTALL):
        matches.append((m.start(), "read_file", m))
        
    for m in re.finditer(r"<write_file\s+path=([\"']?)(.*?)\1[^>]*>(.*?)</write_file\s*>", text, re.DOTALL):
        matches.append((m.start(), "write_file", m))
        
    for m in re.finditer(r"<patch_file\s+path=([\"']?)(.*?)\1[^>]*>\s*<search>(.*?)</search>\s*<replace>(.*?)</replace>\s*</patch_file\s*>", text, re.DOTALL):
        matches.append((m.start(), "patch_file", m))
        
    matches.sort(key=lambda x: x[0])
    
    results = []
    for _, tag_type, m in matches:
        if tag_type == "list_dir":
            path = m.group(1).strip()
            res = tool_list_dir(target_dir, path)
            format_and_print_tool_call("list_dir", path, res)
            results.append({"tool": "list_dir", "path": path, "result": res})
            
        elif tag_type == "read_file":
            path = m.group(1).strip()
            res = tool_read_file(target_dir, path)
            format_and_print_tool_call("read_file", path, res)
            results.append({"tool": "read_file", "path": path, "result": res})
            
        elif tag_type == "write_file":
            path = m.group(2).strip()
            content = m.group(3)
            if content.startswith("\n"):
                content = content[1:]
            res = tool_write_file(target_dir, path, content)
            format_and_print_tool_call("write_file", path, res)
            results.append({"tool": "write_file", "path": path, "result": res})
            
        elif tag_type == "patch_file":
            path = m.group(2).strip()
            search = m.group(3)
            replace = m.group(4)
            if search.startswith("\n"):
                search = search[1:]
            if replace.startswith("\n"):
                replace = replace[1:]
            res = tool_patch_file(target_dir, path, search, replace)
            format_and_print_tool_call("patch_file", path, res)
            results.append({"tool": "patch_file", "path": path, "result": res})
            
    return results

TOOL_RESULT_PREFIX = "### Execution result of "

def compact_old_tool_results(messages: list[dict], keep_last: int = 1) -> None:
    """Replace verbatim tool-result content in older turns with a short placeholder.

    Tool results (which can include full file contents) are re-sent to the LLM on
    every subsequent iteration otherwise, so payload size grows with iterations *
    accumulated bytes. Older results are already reflected in the model's own
    replies, so only the most recent `keep_last` need to stay verbatim.
    """
    indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "user" and m.get("is_tool_result", False)
    ]
    for i in indices[:-keep_last] if keep_last else indices:
        content = messages[i]["content"]
        messages[i]["content"] = (
            f"[Tool result omitted to save context — {len(content)} chars, already processed by the agent]"
        )

class StreamInterrupted(Exception):
    """Raised when a user interrupts an in-progress stream (console mode only)."""
    def __init__(self, partial_text: str):
        super().__init__("Streaming interrupted by user")
        self.partial_text = partial_text

def stream_completion(client: OpenAI, model: str, messages: list[dict], on_update, throttle_every: int = 20) -> str:
    """Stream one chat completion, calling on_update(accumulated_text) at a throttled cadence.

    This is the single place that accumulates chunks and applies bounds-checking/
    throttling; both console and TUI front-ends (single-shot and agent-loop modes)
    drive it with a different on_update callback rather than re-implementing the
    accumulation loop.
    """
    parts = []
    chunk_count = 0
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.2,
        stream=True,
        timeout=120.0
    )
    try:
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                parts.append(content)
                chunk_count += 1
                if chunk_count % throttle_every == 0 or "\n" in content:
                    on_update("".join(parts))
    except KeyboardInterrupt:
        partial_text = "".join(parts)
        on_update(partial_text)
        raise StreamInterrupted(partial_text)

    full_text = "".join(parts)
    on_update(full_text)
    return full_text

def build_tool_result_message(tool_results: list[dict]) -> str:
    parts = [
        f"### Execution result of {tr['tool']} on '{tr['path']}':\n{tr['result']}\n"
        for tr in tool_results
    ]
    return "\n".join(parts)

def run_agent_loop(client: OpenAI, model: str, target_dir: Path, messages: list[dict], max_iterations: int):
    """Executes the agentic reasoning & execution loop with live updates."""
    for i in range(1, max_iterations + 1):
        console.print(f"\n[bold blue]🤖 Agent Thinking (Step {i}/{max_iterations}) ...[/bold blue]")

        assistant_response = ""
        try:
            with Live(console=console, refresh_per_second=8) as live:
                def on_update(text):
                    live.update(Panel(Markdown(text), title=f"[bold green]Assistant (Step {i})[/bold green]", border_style="blue"))
                assistant_response = stream_completion(client, model, messages, on_update)
        except StreamInterrupted as e:
            console.print("\n[bold yellow]Generation interrupted by user.[/bold yellow]")
            assistant_response = e.partial_text
            if not assistant_response:
                break
        except Exception as e:
            console.print(f"[bold red]API call failed:[/bold red] {e}")
            break

        if not assistant_response:
            console.print("[bold red]Received empty response from the model.[/bold red]")
            break

        messages.append({"role": "assistant", "content": assistant_response})

        tool_results = parse_and_execute_tools(target_dir, assistant_response)

        if not tool_results:
            console.print("[bold green]✔ No tools triggered or task complete.[/bold green]")
            break

        result_message = build_tool_result_message(tool_results)
        console.print(f"[bold cyan]Sending tool results back to LLM...[/bold cyan]")
        messages.append({"role": "user", "content": result_message, "is_tool_result": True})
        compact_old_tool_results(messages)

    return messages

def run_single_prompt(client: OpenAI, model: str, messages: list[dict]):
    console.print(f"[bold blue]Sending prompt to LLM...[/bold blue]")
    try:
        with Live(console=console, refresh_per_second=8) as live:
            def on_update(text):
                live.update(Panel(Markdown(text), title="Assistant Response", border_style="blue"))
            assistant_response = stream_completion(client, model, messages, on_update)
        messages.append({"role": "assistant", "content": assistant_response})
    except StreamInterrupted as e:
        console.print("\n[bold yellow]Generation interrupted by user.[/bold yellow]")
        if e.partial_text:
            messages.append({"role": "assistant", "content": e.partial_text})
    except Exception as e:
        console.print(f"[bold red]API call failed:[/bold red] {e}")
    return messages

def _probe_provider(url: str) -> bool:
    try:
        with httpx.Client(timeout=1.0) as http_client:
            res = http_client.get(url)
            return res.status_code == 200
    except Exception:
        return False

def detect_provider():
    """Detects active local provider: LM Studio or llama.cpp. Probes concurrently."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        lmstudio_future = executor.submit(_probe_provider, "http://localhost:1234/v1/models")
        llamacpp_future = executor.submit(_probe_provider, "http://localhost:8080/v1/models")

        if lmstudio_future.result():
            return "lmstudio", "http://localhost:1234/v1"
        if llamacpp_future.result():
            return "llamacpp", "http://localhost:8080/v1"

    return None, None

def resolve_provider_config(provider, api_url, api_key, model):
    """Resolve provider/api_url/api_key/model CLI args into one concrete configuration."""
    if not provider and not api_url:
        detected_prov, detected_url = detect_provider()
        if detected_prov:
            provider = detected_prov
            api_url = detected_url
            console.print(f"[bold green]✔ Auto-detected active provider: [yellow]{provider}[/yellow] at {api_url}[/bold green]")
        else:
            provider = "lmstudio"
            console.print("[yellow]⚠ No active provider detected on default ports. Defaulting to LM Studio preset.[/yellow]")

    provider_defaults = {
        "lmstudio": ("http://localhost:1234/v1", "lm-studio"),
        "llamacpp": ("http://localhost:8080/v1", "not-needed"),
    }
    default_url, default_key = provider_defaults.get(provider, provider_defaults["lmstudio"])
    api_url = api_url or default_url
    api_key = api_key or default_key
    model = model or "local-model"

    return provider, api_url, api_key, model

def list_downloaded_models():
    """List GGUF files in the models directory."""
    models_dir = get_models_dir()
    return list(models_dir.glob("*.gguf"))

_running_servers: list[subprocess.Popen] = []

def _terminate_tracked_servers():
    for proc in _running_servers:
        if proc.poll() is None:
            proc.terminate()

atexit.register(_terminate_tracked_servers)

def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("localhost", port)) == 0

class DownloadModelScreen(ModalScreen):
    CSS = """
    DownloadModelScreen {
        align: center middle;
    }
    #download-dialog {
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $background 80%;
        background: $surface;
    }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="download-dialog"):
            yield Label("Select Model to Download")
            options = [(data["name"], k) for k, data in MODELS_PRESETS.items()]
            yield Select(options, id="model-select")
            yield Label("Select Quantization", id="quant-label")
            yield Select([], id="quant-select", disabled=True)
            yield ProgressBar(total=100, show_eta=False, id="progress-bar")
            yield Label("", id="status-label")
            with Horizontal():
                yield Button("Download", variant="success", id="download-btn", disabled=True)
                yield Button("Cancel", variant="error", id="cancel-btn")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "model-select":
            model_key = event.select.value
            if model_key and model_key != Select.BLANK:
                model_data = MODELS_PRESETS[model_key]
                quants = [(k, k) for k in model_data["quants"].keys()]
                quant_select = self.query_one("#quant-select")
                quant_select.set_options(quants)
                quant_select.disabled = False
        elif event.select.id == "quant-select":
            if event.select.value and event.select.value != Select.BLANK:
                self.query_one("#download-btn").disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.app.pop_screen()
        elif event.button.id == "download-btn":
            model_key = self.query_one("#model-select").value
            quant_key = self.query_one("#quant-select").value
            self.do_download(model_key, quant_key)
            event.button.disabled = True
            self.query_one("#cancel-btn").disabled = True

    @work(thread=True)
    def do_download(self, model_key, quant_key):
        model_data = MODELS_PRESETS[model_key]
        filename = model_data["quants"][quant_key]
        repo_id = model_data["repo"]
        destination_file = get_models_dir() / filename
        url = f"https://huggingface.co/{repo_id}/resolve/main/{filename}"
        
        status = self.query_one("#status-label")
        pb = self.query_one("#progress-bar")
        
        self.app.call_from_thread(status.update, "Connecting to HF...")
        
        try:
            with httpx.stream("GET", url, follow_redirects=True) as response:
                if response.status_code != 200:
                    self.app.call_from_thread(status.update, f"HTTP Error {response.status_code}")
                    self.app.call_from_thread(self.query_one("#cancel-btn").__setattr__, "disabled", False)
                    return
                    
                total_size = int(response.headers.get("content-length", 0))
                self.app.call_from_thread(pb.update, total=total_size)
                
                downloaded = 0
                last_update = time.time()
                with open(destination_file, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        now = time.time()
                        if now - last_update > 0.1:
                            self.app.call_from_thread(pb.update, progress=downloaded)
                            last_update = now

            self.app.call_from_thread(pb.update, progress=downloaded)
            self.app.call_from_thread(status.update, "Downloaded successfully!")
            self.app.call_from_thread(self.query_one("#cancel-btn").__setattr__, "label", "Close")
            self.app.call_from_thread(self.query_one("#cancel-btn").__setattr__, "disabled", False)
            
        except Exception as e:
            if destination_file.exists():
                destination_file.unlink()
            self.app.call_from_thread(status.update, f"Error: {e}")
            self.app.call_from_thread(self.query_one("#cancel-btn").__setattr__, "disabled", False)

class ServeModelScreen(ModalScreen):
    CSS = """
    ServeModelScreen {
        align: center middle;
    }
    #serve-dialog {
        padding: 1 2;
        width: 60;
        height: auto;
        border: thick $background 80%;
        background: $surface;
    }
    """
    def compose(self) -> ComposeResult:
        with Vertical(id="serve-dialog"):
            yield Label("Select Model to Serve")
            yield Select([], id="serve-model-select")
            yield Label("Port:")
            yield Input(value="8080", id="port-input")
            yield Label("", id="serve-status")
            with Horizontal():
                yield Button("Start Server", variant="success", id="start-btn")
                yield Button("Cancel", variant="error", id="cancel-serve-btn")

    def __init__(self):
        super().__init__()
        self.proc = None

    def on_mount(self):
        gguf_files = list_downloaded_models()
        options = [(f.name, str(f.absolute())) for f in gguf_files]
        select = self.query_one("#serve-model-select")
        select.set_options(options)
        if not options:
            self.query_one("#serve-status").update("No models found. Download one first.")
            self.query_one("#start-btn").disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-serve-btn":
            self.app.pop_screen()
        elif event.button.id == "start-btn":
            if self.proc is not None:
                self.stop_server()
                return
            file_path = self.query_one("#serve-model-select").value
            port = self.query_one("#port-input").value
            if file_path and file_path != Select.BLANK and port:
                self.start_server(file_path, port)

    def start_server(self, file_path, port):
        status = self.query_one("#serve-status")
        try:
            port_int = int(port)
        except ValueError:
            status.update("Error: Port must be a number.")
            return

        if is_port_in_use(port_int):
            status.update(f"Error: Port {port} is already in use by another process.")
            return

        cmd = ["llama-server", "-m", file_path, "--port", port, "-c", "4096"]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True
            )
            self.proc = proc
            _running_servers.append(proc)
            status.update(f"Server launched on port {port} (PID {proc.pid})")
            start_btn = self.query_one("#start-btn")
            start_btn.label = "Stop Server"
            start_btn.variant = "error"
            self.query_one("#cancel-serve-btn").label = "Close"
        except FileNotFoundError:
            status.update("Error: 'llama-server' binary not found.")
        except Exception as e:
            status.update(f"Failed: {e}")

    def stop_server(self):
        status = self.query_one("#serve-status")
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc in _running_servers:
            _running_servers.remove(self.proc)
        self.proc = None
        status.update("Server stopped.")
        start_btn = self.query_one("#start-btn")
        start_btn.label = "Start Server"
        start_btn.variant = "success"
        self.query_one("#cancel-serve-btn").label = "Cancel"


class ChatMessage(Static):
    def __init__(self, text: str, role: str, is_diff: bool = False):
        super().__init__()
        self.text = text
        self.role = role
        self.is_diff = is_diff

    def render(self):
        if self.role == "user":
            return Align.right(Panel(self.text, title="You", border_style="green", expand=False))
        elif self.role == "assistant":
            return Panel(Markdown(self.text), title="Assistant", border_style="blue", expand=False)
        else: # System or tool
            content = Syntax(self.text, "diff", theme="monokai", line_numbers=False) if self.is_diff else self.text
            return Panel(content, title=self.role.capitalize(), border_style="yellow", expand=False)

class StreamMessage(Static):
    """A chat widget that accumulates streamed text and re-renders it on demand."""
    def __init__(self, title: str = "Assistant"):
        super().__init__("")
        self.content = ""
        self.stream_title = title

    def update_content(self, full_text: str):
        self.content = full_text
        self.update(Panel(Markdown(self.content), title=self.stream_title, border_style="blue", expand=False))

class LocalCoderApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #chat-history {
        height: 1fr;
        padding: 0 1;
    }
    #input-box {
        dock: bottom;
        height: 3;
    }
    """
    
    def __init__(self, client, model, target_dir, messages, agent_mode, max_iterations):
        super().__init__()
        self.client = client
        self.model = model
        self.target_dir = target_dir
        self.messages = messages
        self.agent_mode = agent_mode
        self.max_iterations = max_iterations
        self.is_processing = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield VerticalScroll(id="chat-history")
        yield Input(placeholder="Type your instruction... (or /help)", id="input-box")
        yield Footer()

    def on_mount(self) -> None:
        self.chat_history = self.query_one("#chat-history")
        self.input_box = self.query_one("#input-box")
        self.title = "Local Coder TUI"
        self.sub_title = f"Model: {self.model} | Target: {self.target_dir}"
        
        # Display welcome message
        self.chat_history.mount(ChatMessage("Welcome to Local Coder! Type your instruction below.", "system"))

    def _trim_chat_history(self, max_widgets: int = 300):
        """Cap retained chat widgets so a long session doesn't grow memory/render cost unbounded."""
        children = list(self.chat_history.children)
        if len(children) > max_widgets:
            for child in children[: len(children) - max_widgets]:
                child.remove()

    async def on_input_submitted(self, message: Input.Submitted) -> None:
        if self.is_processing:
            return
            
        user_input = message.value.strip()
        if not user_input:
            return
            
        self.input_box.value = ""
        
        if user_input.startswith("/"):
            await self.handle_slash_command(user_input)
            return

        self.messages.append({"role": "user", "content": user_input})
        await self.chat_history.mount(ChatMessage(user_input, "user"))
        self.call_after_refresh(self.chat_history.scroll_end, animate=False)
        self._trim_chat_history()

        self.is_processing = True
        if self.agent_mode:
            self.run_agent_loop_async()
        else:
            self.run_single_prompt_async()

    async def handle_slash_command(self, cmd_line: str):
        cmd = cmd_line.lower().split()[0]
        if cmd in ["/exit", "/quit"]:
            self.exit()
        elif cmd == "/dir":
            res = tool_list_dir(self.target_dir, ".")
            await self.chat_history.mount(ChatMessage(res, "system"))
            self.chat_history.scroll_end(animate=False)
        elif cmd == "/clear":
            # Just clear UI history
            for child in self.chat_history.children:
                child.remove()
            # Retain system prompt
            sys_prompt = self.messages[0]
            self.messages = [sys_prompt]
            await self.chat_history.mount(ChatMessage("History cleared.", "system"))
        elif cmd == "/models":
            self.push_screen(DownloadModelScreen())
        elif cmd == "/serve":
            self.push_screen(ServeModelScreen())
        elif cmd == "/help":
            help_text = (
                "**Available Commands:**\n"
                "- `/help`: Show this help screen\n"
                "- `/models`: Manage (list & download) local GGUF models\n"
                "- `/serve`: Serve a downloaded model using llama-server\n"
                "- `/dir`: List files in the target directory\n"
                "- `/clear`: Clear history or screen\n"
                "- `/exit` or `/quit`: Exit the interactive session"
            )
            await self.chat_history.mount(ChatMessage(help_text, "system"))
            self.chat_history.scroll_end(animate=False)
        else:
            await self.chat_history.mount(ChatMessage(f"Command {cmd} not supported in TUI yet. Supported: /exit, /clear, /dir, /models, /serve, /help", "system"))
            self.chat_history.scroll_end(animate=False)

    @work(thread=True)
    def run_single_prompt_async(self):
        self.call_from_thread(self.input_box.set_class, True, "-disabled") # just logic

        stream_msg = StreamMessage("Assistant")
        self.call_from_thread(self.chat_history.mount, stream_msg)

        def on_update(text):
            self.call_from_thread(stream_msg.update_content, text)
            self.call_from_thread(self.chat_history.scroll_end, animate=False)

        try:
            assistant_response = stream_completion(self.client, self.model, self.messages, on_update)
            self.call_from_thread(self._trim_chat_history)
            self.messages.append({"role": "assistant", "content": assistant_response})

        except Exception as e:
            self.call_from_thread(self.chat_history.mount, ChatMessage(f"Error: {e}", "system"))
        finally:
            self.is_processing = False

    @work(thread=True)
    def run_agent_loop_async(self):
        try:
            for i in range(1, self.max_iterations + 1):
                self.call_from_thread(self.chat_history.mount, ChatMessage(f"🤖 Agent Thinking (Step {i}/{self.max_iterations}) ...", "system"))
                self.call_after_refresh(self.chat_history.scroll_end, animate=False)

                stream_msg = StreamMessage(f"Assistant (Step {i})")
                self.call_from_thread(self.chat_history.mount, stream_msg)

                def on_update(text, _stream_msg=stream_msg):
                    self.call_from_thread(_stream_msg.update_content, text)
                    self.call_from_thread(self.chat_history.scroll_end, animate=False)

                assistant_response = stream_completion(self.client, self.model, self.messages, on_update)

                self.messages.append({"role": "assistant", "content": assistant_response})

                # Execute tools
                tool_results = parse_and_execute_tools(self.target_dir, assistant_response)

                if not tool_results:
                    self.call_from_thread(self.chat_history.mount, ChatMessage("✔ No tools triggered or task complete.", "system"))
                    self.call_after_refresh(self.chat_history.scroll_end, animate=False)
                    break

                for tr in tool_results:
                    # Show tool result in UI
                    is_diff = tr['tool'] == 'patch_file'
                    self.call_from_thread(self.chat_history.mount, ChatMessage(f"Tool {tr['tool']} on {tr['path']}:\n{tr['result']}", "system", is_diff=is_diff))

                result_message = build_tool_result_message(tool_results)
                self.messages.append({"role": "user", "content": result_message, "is_tool_result": True})
                compact_old_tool_results(self.messages)
                self.call_after_refresh(self.chat_history.scroll_end, animate=False)
                self.call_from_thread(self._trim_chat_history)

        except Exception as e:
            self.call_from_thread(self.chat_history.mount, ChatMessage(f"Error: {e}", "system"))
        finally:
            self.is_processing = False


def main():
    parser = argparse.ArgumentParser(description="Local Coder - Beautiful CLI coding agent.")
    parser.add_argument("--provider", choices=["lmstudio", "llamacpp", "custom"], help="Choose the preset local LLM provider")
    parser.add_argument("--api-url", help="OpenAI-compatible local API base URL (overrides provider default)")
    parser.add_argument("--api-key", help="API key to use")
    parser.add_argument("--model", help="Model identifier to specify in the API calls")
    parser.add_argument("--target-dir", default=".", help="Target folder for file operations (default: current directory)")
    parser.add_argument("--system-prompt", help="Path to custom system prompt txt file")
    parser.add_argument("--agent", action="store_true", help="Enable autonomous agent loop with filesystem tools")
    parser.add_argument("--max-iterations", type=int, default=10, help="Maximum number of loop iterations for agent mode")
    parser.add_argument("-i", "--interactive", action="store_true", help="Force launch the interactive REPL shell")
    parser.add_argument("prompt", nargs="?", help="The programming task / instruction for the LLM")
    
    args = parser.parse_args()
    
    provider, api_url, api_key, model = resolve_provider_config(
        args.provider, args.api_url, args.api_key, args.model
    )

    target_dir = Path(args.target_dir).resolve()
    if not target_dir.exists():
        console.print(f"[bold yellow]Target directory '{target_dir}' does not exist. Creating it...[/bold yellow]")
        target_dir.mkdir(parents=True, exist_ok=True)
        
    system_prompt_content = ""
    if args.system_prompt:
        sys_prompt_path = Path(args.system_prompt)
        if sys_prompt_path.exists():
            system_prompt_content = sys_prompt_path.read_text(encoding="utf-8")
        else:
            console.print(f"[bold red]System prompt file not found at {args.system_prompt}[/bold red]")
            sys.exit(1)
    else:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            default_path = Path(sys._MEIPASS) / "system_prompt.txt"
        else:
            default_path = Path(__file__).parent / "system_prompt.txt"
            
        if default_path.exists():
            system_prompt_content = default_path.read_text(encoding="utf-8")
        else:
            system_prompt_content = "You are a helpful coding assistant. You have local file system access."

    client = OpenAI(base_url=api_url, api_key=api_key)
    
    messages = [{"role": "system", "content": system_prompt_content}]
    
    is_interactive = args.interactive or (not args.prompt)
    

    if is_interactive:
        global TUI_MODE
        TUI_MODE = True
        app = LocalCoderApp(client, model, target_dir, messages, args.agent, args.max_iterations)
        app.run()
    else:
        messages.append({"role": "user", "content": args.prompt})
        console.print(Panel(f"[bold green]Target Directory:[/bold green] {target_dir.resolve()}\n[bold green]Provider:[/bold green] {provider} ({api_url})\n[bold green]Task:[/bold green] {args.prompt}", title="Agent Run Started"))
        if args.agent:
            run_agent_loop(client, model, target_dir, messages, args.max_iterations)
        else:
            run_single_prompt(client, model, messages)

if __name__ == "__main__":
    main()
