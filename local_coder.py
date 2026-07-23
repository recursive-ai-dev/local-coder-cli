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
import json
import threading
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
from rich.prompt import Prompt, Confirm
from rich.align import Align

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll, Vertical, Horizontal
from textual.widgets import Header, Footer, Static, Input, Button, Select, ProgressBar, Label
from textual.screen import ModalScreen
from textual import work


console = Console()
TUI_MODE = False
_YOLO_MODE = False
_SHOW_IGNORED = False
_MAX_HISTORY = 20
_SESSION_FILE = None
_ALLOWED_TOOLS = None
_TOOL_MODE = "auto"
_APP_INSTANCE = None

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

BUILTIN_AGENTS = {
    "coder": {
        "name": "Coder",
        "description": "Full-featured coding agent with read/write access to the local filesystem.",
        "system_prompt": "You are a helpful coding assistant. You have local file system access. You can read, write, and patch files in the target directory to complete coding tasks.",
        "allowed_tools": ["list_dir", "read_file", "search_files", "write_file", "patch_file", "delete_file", "move_file"]
    },
    "explorer": {
        "name": "Explorer",
        "description": "Read-only agent for exploring the codebase and answering questions. Cannot write or modify files.",
        "system_prompt": "You are a read-only exploration assistant. You can list directories, read files, and search file contents to answer questions about the codebase, but you cannot make any changes.",
        "allowed_tools": ["list_dir", "read_file", "search_files"]
    },
    "reviewer": {
        "name": "Reviewer",
        "description": "Read-only agent for reviewing code. Cannot write or modify files.",
        "system_prompt": "You are a strict code reviewer. Read the requested files and provide constructive feedback on bugs, style, and structure. You cannot modify the files.",
        "allowed_tools": ["list_dir", "read_file", "search_files"]
    }
}

def get_agents_dir(target_dir: Path) -> Path:
    """Project-local agent profiles directory, matching load_agent_config's convention."""
    a_dir = target_dir / ".local-coder" / "agents"
    a_dir.mkdir(parents=True, exist_ok=True)
    return a_dir

def get_all_agents(target_dir: Path) -> dict:
    """Merge built-in presets with project-local (.local-coder/agents/) profiles."""
    agents = BUILTIN_AGENTS.copy()
    a_dir = get_agents_dir(target_dir)
    for file_path in a_dir.glob("*.json"):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "name" in data and "description" in data:
                    agents[file_path.stem] = data
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load agent profile from {file_path}: {e}[/yellow]")
    return agents

def print_list_agents(target_dir: Path):
    agents = get_all_agents(target_dir)
    table = Table(title="Available Agent Profiles", show_header=True, header_style="bold magenta")
    table.add_column("Key", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Type", style="dim")
    table.add_column("Description", style="white")
    table.add_column("Allowed Tools", style="blue")

    for key, data in agents.items():
        agent_type = "Built-in" if key in BUILTIN_AGENTS else "Custom"
        allowed = ", ".join(data.get("allowed_tools", []) or ["(all)"])
        table.add_row(key, data.get("name", "Unknown"), agent_type, data.get("description", ""), allowed)

    console.print(table)

def scaffold_create_agent(target_dir: Path, name: str):
    a_dir = get_agents_dir(target_dir)
    file_path = a_dir / f"{name}.json"
    if file_path.exists():
        console.print(f"[bold red]Error: Agent profile '{name}' already exists at {file_path}[/bold red]")
        sys.exit(1)

    template = {
        "name": name.title(),
        "description": "A custom agent profile.",
        "system_prompt": "You are a helpful coding assistant. You have local file system access.",
        "allowed_tools": ["list_dir", "read_file", "search_files", "write_file", "patch_file"]
    }

    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=4)
        console.print(f"[bold green]✔ Successfully created custom agent profile '{name}' at {file_path}[/bold green]")
        console.print("Edit this file to customize the agent's behavior.")
    except Exception as e:
        console.print(f"[bold red]Error creating agent profile:[/bold red] {e}")
        sys.exit(1)

def get_models_dir() -> Path:
    if getattr(sys, 'frozen', False):
        m_dir = Path(sys.executable).parent / "models"
    else:
        m_dir = Path(__file__).parent / "models"
    m_dir.mkdir(parents=True, exist_ok=True)
    return m_dir

def load_agent_config(target_dir: Path, name: str) -> dict:
    """Load subagent configuration from target_dir/.local-coder/agents/{name}.json
    or fallback to bundled models/agents directory."""
    try:
        agent_file = get_safe_path(target_dir, f".local-coder/agents/{name}.json")
        if agent_file.exists():
            return json.loads(agent_file.read_text(encoding="utf-8"))
    except ValueError:
        pass

    # Fallback to bundled
    if getattr(sys, 'frozen', False):
        m_dir = Path(sys.executable).parent / "agents"
    else:
        m_dir = Path(__file__).parent / "agents"

    fallback_file = m_dir / f"{name}.json"
    if fallback_file.exists():
        return json.loads(fallback_file.read_text(encoding="utf-8"))

    raise FileNotFoundError(f"Agent config '{name}.json' not found in target dir or bundled agents dir.")

def save_session(session_file: Path, messages: list[dict]):
    if not session_file:
        return
    session_file.parent.mkdir(parents=True, exist_ok=True)
    filtered = [m for m in messages if m.get("role") != "system"]
    with open(session_file, "w", encoding="utf-8") as f:
        json.dump(filtered, f, indent=2)

def load_session(session_file: Path) -> list[dict]:
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def resolve_session_file(target_dir: Path, session_arg) -> Path | None:
    if not session_arg:
        return None

    sessions_dir = target_dir / ".local-coder" / "sessions"
    if not sessions_dir.exists():
        return None

    if session_arg == "LATEST":
        files = list(sessions_dir.glob("*.json"))
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    try:
        specific = get_safe_path(sessions_dir, session_arg)
        if specific.exists():
            return specific
    except ValueError:
        pass

    try:
        full = get_safe_path(target_dir, session_arg)
        if full.exists():
            return full
    except ValueError:
        pass

    return None

def get_safe_path(target_dir: Path, subpath_str: str) -> Path:
    """Resolve subpath safely, ensuring it is within the target directory."""
    target_dir = target_dir.resolve()
    subpath_str = subpath_str.strip()
    # Normalize Windows backslashes to forward slashes so a "..\\..\\etc\\passwd"
    # style sequence can't dodge the POSIX absolute/traversal checks below.
    subpath_str = subpath_str.replace("\\", "/")
    if subpath_str.startswith("/") or Path(subpath_str).is_absolute():
        raise ValueError(f"Security error: Absolute path '{subpath_str}' is not allowed.")
    
    resolved = (target_dir / subpath_str).resolve()
    if not resolved.is_relative_to(target_dir):
        raise ValueError(f"Security error: Path '{subpath_str}' escapes target directory '{target_dir}'")
    return resolved

import fnmatch

def _load_gitignore(target_dir: Path) -> list[str]:
    gitignore_path = target_dir / ".gitignore"
    patterns = [".git/"]
    if gitignore_path.exists() and gitignore_path.is_file():
        try:
            for line in gitignore_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except Exception:
            pass
    return patterns

def _is_ignored(rel_path: Path, patterns: list[str], is_dir: bool) -> bool:
    path_str = rel_path.as_posix()
    parts = path_str.split('/')
    if ".git" in parts:
        return True

    for pattern in patterns:
        dir_only = pattern.endswith('/')
        pat = pattern.rstrip('/')

        if not pat:
            continue

        if '/' not in pat:
            for i, part in enumerate(parts):
                if fnmatch.fnmatch(part, pat):
                    if dir_only:
                        if i == len(parts) - 1 and not is_dir:
                            continue
                    return True
        else:
            pat_match = pat.lstrip('/')
            if fnmatch.fnmatch(path_str, pat_match) or fnmatch.fnmatch(path_str, pat_match + '/*'):
                if dir_only and not is_dir and fnmatch.fnmatch(path_str, pat_match):
                    continue
                return True

    return False

def tool_list_dir(target_dir: Path, path: str, show_ignored: bool = False) -> str:
    try:
        target_dir = target_dir.resolve()
        safe_path = get_safe_path(target_dir, path)
        if not safe_path.exists():
            return f"Error: Path '{path}' does not exist."
        if not safe_path.is_dir():
            return f"Error: Path '{path}' is a file, not a directory."

        ignore_patterns = []
        if not show_ignored:
            ignore_patterns = _load_gitignore(target_dir)

        entries = []
        for entry in safe_path.iterdir():
            rel_path = entry.relative_to(target_dir)

            if not show_ignored and _is_ignored(rel_path, ignore_patterns, entry.is_dir()):
                continue

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

def tool_search_files(target_dir: Path, pattern: str, path: str = ".") -> str:
    try:
        target_dir = target_dir.resolve()
        safe_path = get_safe_path(target_dir, path)
        if not safe_path.exists():
            return f"Error: Path '{path}' does not exist."

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex pattern: {str(e)}"

        matches = []
        total_matches = 0

        if safe_path.is_file():
            files_to_check = [safe_path]
        else:
            files_to_check = []
            for root, _, files in os.walk(safe_path):
                for f in files:
                    files_to_check.append(Path(root) / f)

        for f_path in files_to_check:
            try:
                if f_path.stat().st_size > 500 * 1024:
                    continue
                content = f_path.read_text(encoding="utf-8")
                for i, line in enumerate(content.splitlines(), start=1):
                    if regex.search(line):
                        if total_matches < 200:
                            rel_path = f_path.relative_to(target_dir)
                            matches.append(f"{rel_path}:{i}:{line}")
                        total_matches += 1
            except (UnicodeDecodeError, FileNotFoundError, PermissionError):
                continue

        if total_matches == 0:
            return "No matches found."

        if total_matches > 200:
            hidden = total_matches - 200
            matches.append(f"... [{hidden} more items hidden]")

        return "\n".join(matches)
    except Exception as e:
        return f"Error searching files: {str(e)}"

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

def tool_delete_file(target_dir: Path, path: str) -> str:
    try:
        target_dir = target_dir.resolve()
        safe_path = get_safe_path(target_dir, path)
        if not safe_path.exists():
            return f"Error: File '{path}' does not exist."
        if not safe_path.is_file():
            return f"Error: Path '{path}' is not a file."

        safe_path.unlink()
        return f"Successfully deleted file '{path}'."
    except Exception as e:
        return f"Error deleting file: {str(e)}"

def tool_move_file(target_dir: Path, src: str, dst: str) -> str:
    try:
        target_dir = target_dir.resolve()
        safe_src = get_safe_path(target_dir, src)
        safe_dst = get_safe_path(target_dir, dst)

        if not safe_src.exists():
            return f"Error: Source '{src}' does not exist."

        safe_dst.parent.mkdir(parents=True, exist_ok=True)
        safe_src.rename(safe_dst)

        return f"Successfully moved '{src}' to '{dst}'."
    except Exception as e:
        return f"Error moving file: {str(e)}"

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Lists files and folders inside the target directory. .gitignore-matched entries and .git are hidden unless show_ignored is true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list, relative to the target directory ('.' for root)."},
                    "show_ignored": {"type": "boolean", "description": "Include .gitignore-matched and .git entries. Defaults to false."}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Reads the full content of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File to read, relative to the target directory."}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Searches for a regular expression in a file or all files in a directory. Capped at 200 matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for."},
                    "path": {"type": "string", "description": "File or directory to search, relative to the target directory ('.' for root)."}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Creates a new file or completely overwrites an existing file. Asks the user for confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to write, relative to the target directory."},
                    "content": {"type": "string", "description": "Content to write to the file."}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": "Modifies part of an existing file by replacing an exact search block with a replacement. Asks the user for confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to patch, relative to the target directory."},
                    "search": {"type": "string", "description": "Exact text to search for."},
                    "replace": {"type": "string", "description": "Replacement text."}
                },
                "required": ["path", "search", "replace"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Deletes an existing file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File to delete, relative to the target directory."}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Moves or renames a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Source path, relative to the target directory."},
                    "dst": {"type": "string", "description": "Destination path, relative to the target directory."}
                },
                "required": ["src", "dst"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Executes a shell command in the target directory. Asks the user for confirmation. 30s timeout.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell command to run."}},
                "required": ["command"]
            }
        }
    }
]

def execute_native_tools(target_dir: Path, tool_calls: list[dict], allowed_tools: list[str] = None) -> list[dict]:
    """Execute native JSON tool calls, applying the same confirmation gates and
    allowed_tools filtering as the XML dispatch path in parse_and_execute_tools."""
    results = []
    for tc in tool_calls:
        func = tc.get("function", {})
        tool_name = func.get("name")
        tool_id = tc.get("id")
        args_str = func.get("arguments", "{}")

        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            results.append({"tool": tool_name, "id": tool_id, "path": "N/A", "result": f"Error: Invalid JSON arguments: {args_str}"})
            continue

        if allowed_tools is not None and tool_name not in allowed_tools:
            res = f"Error: Tool '{tool_name}' is not permitted by this agent's configuration."
            format_and_print_tool_call(tool_name or "unknown", "N/A", res)
            results.append({"tool": tool_name, "id": tool_id, "path": "N/A", "result": res})
            continue

        if tool_name == "list_dir":
            path = args.get("path", ".")
            show_ignored = args.get("show_ignored", _SHOW_IGNORED)
            res = tool_list_dir(target_dir, path, show_ignored=show_ignored)
            format_and_print_tool_call("list_dir", path, res)
            results.append({"tool": "list_dir", "id": tool_id, "path": path, "result": res})

        elif tool_name == "read_file":
            path = args.get("path", "")
            res = tool_read_file(target_dir, path)
            format_and_print_tool_call("read_file", path, res)
            results.append({"tool": "read_file", "id": tool_id, "path": path, "result": res})

        elif tool_name == "search_files":
            path = args.get("path", ".")
            pattern = args.get("pattern", "")
            res = tool_search_files(target_dir, pattern, path)
            format_and_print_tool_call("search_files", f"path='{path}', pattern='{pattern}'", res)
            results.append({"tool": "search_files", "id": tool_id, "path": path, "result": res})

        elif tool_name == "write_file":
            path = args.get("path", "")
            content = args.get("content", "")
            if not ask_user_confirmation("write_file", path, content):
                results.append({"tool": "write_file", "id": tool_id, "path": path, "result": "Error: User denied permission to write file."})
                continue
            res = tool_write_file(target_dir, path, content)
            format_and_print_tool_call("write_file", path, res)
            results.append({"tool": "write_file", "id": tool_id, "path": path, "result": res})

        elif tool_name == "patch_file":
            path = args.get("path", "")
            search = args.get("search", "")
            replace = args.get("replace", "")
            preview = f"Search:\n{search}\n\nReplace:\n{replace}"
            if not ask_user_confirmation("patch_file", path, preview):
                results.append({"tool": "patch_file", "id": tool_id, "path": path, "result": "Error: User denied permission to patch file."})
                continue
            res = tool_patch_file(target_dir, path, search, replace)
            format_and_print_tool_call("patch_file", path, res)
            results.append({"tool": "patch_file", "id": tool_id, "path": path, "result": res})

        elif tool_name == "delete_file":
            path = args.get("path", "")
            res = tool_delete_file(target_dir, path)
            format_and_print_tool_call("delete_file", path, res)
            results.append({"tool": "delete_file", "id": tool_id, "path": path, "result": res})

        elif tool_name == "move_file":
            src = args.get("src", "")
            dst = args.get("dst", "")
            res = tool_move_file(target_dir, src, dst)
            format_and_print_tool_call("move_file", f"src='{src}' dst='{dst}'", res)
            results.append({"tool": "move_file", "id": tool_id, "path": f"{src} -> {dst}", "result": res})

        elif tool_name == "run_command":
            command = args.get("command", "")
            if not ask_user_confirmation("run_command", "shell", command):
                results.append({"tool": "run_command", "id": tool_id, "path": ".", "result": "Error: User denied command execution."})
                continue
            res = tool_run_command(target_dir, command)
            format_and_print_tool_call("run_command", command, res)
            results.append({"tool": "run_command", "id": tool_id, "path": ".", "result": res})

        else:
            results.append({"tool": tool_name, "id": tool_id, "path": "N/A", "result": f"Error: Unknown tool '{tool_name}'"})

    return results

def tool_run_command(target_dir: Path, command: str) -> str:
    try:
        target_dir = target_dir.resolve()
        # cwd=target_dir confines the working directory, but the command
        # itself could still reference paths elsewhere (e.g. `rm ../x`) -
        # ask_user_confirmation is the actual gate here, same as write_file
        # and patch_file.
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=30.0
            )
        except subprocess.TimeoutExpired:
            return "Error: Command timed out after 30 seconds."

        output = result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += "--- STDERR ---\n" + result.stderr

        if not output.strip():
            output = "Command executed successfully with no output."

        lines = output.splitlines()
        if len(lines) > 200:
            hidden = len(lines) - 200
            lines = lines[:200]
            lines.append(f"... [{hidden} more lines hidden]")
            output = "\n".join(lines)

        return f"Exit code: {result.returncode}\nOutput:\n{output}"
    except Exception as e:
        return f"Error executing command: {str(e)}"

def _tools_info_markdown() -> str:
    return (
        "**Available Tool Tags:**\n"
        "- `<list_dir path=\"...\">`: List files in a directory (add `show_ignored=\"true\"` to bypass .gitignore filtering)\n"
        "- `<read_file>path</read_file>`: Read file content\n"
        "- `<search_files path=\"...\">pattern</search_files>`: Regex search across a file or directory\n"
        "- `<write_file path=\"...\">content</write_file>`: Create/overwrite a file (asks for confirmation)\n"
        "- `<patch_file path=\"...\"><search>...</search><replace>...</replace></patch_file>`: Edit a file (asks for confirmation)\n"
        "- `<delete_file>path</delete_file>`: Delete a file\n"
        "- `<move_file src=\"...\" dst=\"...\" />`: Move/rename a file\n"
        "- `<spawn_agent name=\"...\">task</spawn_agent>`: Spawn a configured subagent for a task\n"
        "- `<run_command>shell command</run_command>`: Execute a shell command in the target directory (asks for confirmation, 30s timeout)"
    )

def _agents_info_markdown(target_dir: Path) -> str:
    agents = get_all_agents(target_dir)
    lines = ["**Available Agent Profiles:**"]
    for key, data in agents.items():
        kind = "Built-in" if key in BUILTIN_AGENTS else "Custom"
        lines.append(f"- `{key}` ({kind}): {data.get('description', '')}")
    return "\n".join(lines)

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
    elif tool_name == "run_command":
        console.print(Panel(result, border_style="red", title="Command Output"))
    else:
        console.print(Panel(result, border_style="cyan", title="Tool Result"))

def ask_user_confirmation(tool_name: str, path: str, preview: str) -> bool:
    if _YOLO_MODE:
        return True

    title = f"{tool_name} on '{path}'"
    if not TUI_MODE:
        console.print(Panel(Syntax(preview, "python", theme="monokai"), title=f"Preview: {title}", border_style="yellow"))
        return Confirm.ask(f"[bold yellow]Allow {tool_name} on {path}?[/bold yellow]")

    # TUI Mode
    result_event = threading.Event()
    user_decision = []

    def on_dismiss(result: bool):
        user_decision.append(result)
        result_event.set()

    screen = ConfirmationScreen(title, preview)
    _APP_INSTANCE.call_from_thread(_APP_INSTANCE.push_screen, screen, on_dismiss)

    result_event.wait()
    return user_decision[0] if user_decision else False

def parse_and_execute_tools(target_dir: Path, text: str, allowed_tools: list[str] = None, subagent_runner=None) -> list[dict]:
    """Parse XML tags in response and execute tools in the order they appear."""
    matches = []
    
    for m in re.finditer(r"<list_dir(?:[^>]*show_ignored=[\"'](true|false)[\"'])?[^>]*>(.*?)</list_dir\s*>", text, re.DOTALL):
        matches.append((m.start(), "list_dir", m))
        
    for m in re.finditer(r"<read_file>(.*?)</read_file\s*>", text, re.DOTALL):
        matches.append((m.start(), "read_file", m))

    for m in re.finditer(r"<search_files(?:\s+path=([\"']?)(.*?)\1)?[^>]*>(.*?)</search_files\s*>", text, re.DOTALL):
        matches.append((m.start(), "search_files", m))

    for m in re.finditer(r"<write_file\s+path=([\"']?)(.*?)\1[^>]*>(.*?)</write_file\s*>", text, re.DOTALL):
        matches.append((m.start(), "write_file", m))
        
    for m in re.finditer(r"<patch_file\s+path=([\"']?)(.*?)\1[^>]*>\s*<search>(.*?)</search>\s*<replace>(.*?)</replace>\s*</patch_file\s*>", text, re.DOTALL):
        matches.append((m.start(), "patch_file", m))

    for m in re.finditer(r"<spawn_agent\s+name=([\"']?)(.*?)\1[^>]*>(.*?)</spawn_agent\s*>", text, re.DOTALL):
        matches.append((m.start(), "spawn_agent", m))
        
    for m in re.finditer(r"<delete_file>(.*?)</delete_file\s*>", text, re.DOTALL):
        matches.append((m.start(), "delete_file", m))

    for m in re.finditer(r"<move_file\s+src=([\"']?)(.*?)\1\s+dst=([\"']?)(.*?)\3\s*/>", text, re.DOTALL):
        matches.append((m.start(), "move_file", m))

    for m in re.finditer(r"<run_command>(.*?)</run_command\s*>", text, re.DOTALL):
        matches.append((m.start(), "run_command", m))

    matches.sort(key=lambda x: x[0])

    # Reject any match whose start falls inside an already-consumed span, so a
    # tag literally appearing in another tag's body (e.g. a <read_file> inside
    # a write_file's own content) is treated as literal text, not re-executed.
    results = []
    last_end = 0
    for start, tag_type, m in matches:
        if start < last_end:
            continue
        last_end = m.end()
        if allowed_tools is not None and tag_type not in allowed_tools:
            res = f"Error: Tool '{tag_type}' is not permitted by this agent's configuration."
            format_and_print_tool_call(tag_type, "N/A", res)
            results.append({"tool": tag_type, "path": "N/A", "result": res})
            continue

        if tag_type == "list_dir":
            show_ignored_attr = m.group(1)
            path = m.group(2).strip()
            show_ignored = _SHOW_IGNORED if show_ignored_attr is None else (show_ignored_attr.lower() == "true")
            res = tool_list_dir(target_dir, path, show_ignored=show_ignored)
            format_and_print_tool_call("list_dir", path, res)
            results.append({"tool": "list_dir", "path": path, "result": res})
            
        elif tag_type == "read_file":
            path = m.group(1).strip()
            res = tool_read_file(target_dir, path)
            format_and_print_tool_call("read_file", path, res)
            results.append({"tool": "read_file", "path": path, "result": res})

        elif tag_type == "search_files":
            path = m.group(2).strip() if m.group(2) else "."
            pattern = m.group(3).strip()
            res = tool_search_files(target_dir, pattern, path)
            format_and_print_tool_call("search_files", f"path='{path}', pattern='{pattern}'", res)
            results.append({"tool": "search_files", "path": path, "pattern": pattern, "result": res})
            
        elif tag_type == "write_file":
            path = m.group(2).strip()
            content = m.group(3)
            if content.startswith("\n"):
                content = content[1:]

            if not ask_user_confirmation("write_file", path, content):
                results.append({"tool": "write_file", "path": path, "result": "Error: User denied permission to write file."})
                continue

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

            preview = f"Search:\n{search}\n\nReplace:\n{replace}"
            if not ask_user_confirmation("patch_file", path, preview):
                results.append({"tool": "patch_file", "path": path, "result": "Error: User denied permission to patch file."})
                continue

            res = tool_patch_file(target_dir, path, search, replace)
            format_and_print_tool_call("patch_file", path, res)
            results.append({"tool": "patch_file", "path": path, "result": res})
            
        elif tag_type == "spawn_agent":
            name = m.group(2).strip()
            task = m.group(3).strip()
            if subagent_runner is None:
                res = "Error: Subagents cannot be spawned in this context."
            else:
                res = subagent_runner(name, task)
            format_and_print_tool_call("spawn_agent", name, res)
            results.append({"tool": "spawn_agent", "path": name, "result": res})
        elif tag_type == "delete_file":
            path = m.group(1).strip()
            res = tool_delete_file(target_dir, path)
            format_and_print_tool_call("delete_file", path, res)
            results.append({"tool": "delete_file", "path": path, "result": res})

        elif tag_type == "move_file":
            src = m.group(2).strip()
            dst = m.group(4).strip()
            res = tool_move_file(target_dir, src, dst)
            format_and_print_tool_call("move_file", f"src='{src}' dst='{dst}'", res)
            results.append({"tool": "move_file", "path": f"{src} -> {dst}", "result": res})

        elif tag_type == "run_command":
            command = m.group(1).strip()
            if not ask_user_confirmation("run_command", "shell", command):
                results.append({"tool": "run_command", "path": ".", "result": "Error: User denied command execution."})
                continue
            res = tool_run_command(target_dir, command)
            format_and_print_tool_call("run_command", command, res)
            results.append({"tool": "run_command", "path": ".", "result": res})

    return results

TOOL_RESULT_PREFIX = "### Execution result of "

def compact_old_tool_results(messages: list[dict], keep_last: int = 1) -> None:
    """Replace verbatim tool-result content in older turns with a short placeholder.

    Tool results (which can include full file contents) are re-sent to the LLM on
    every subsequent iteration otherwise, so payload size grows with iterations *
    accumulated bytes. Older results are already reflected in the model's own
    replies, so only the most recent `keep_last` need to stay verbatim.
    """
    # Group into contiguous blocks so a native tool-calling turn (one or more
    # role="tool" messages sharing an assistant turn) is compacted as a unit,
    # same as an XML turn's single role="user"/is_tool_result message.
    tool_blocks = []
    current_block = []
    for i, m in enumerate(messages):
        is_tool = m.get("role") == "tool" or (m.get("role") == "user" and m.get("is_tool_result", False))
        if is_tool:
            current_block.append(i)
        elif current_block:
            tool_blocks.append(current_block)
            current_block = []
    if current_block:
        tool_blocks.append(current_block)

    for block in tool_blocks[:-keep_last] if keep_last else tool_blocks:
        for i in block:
            content = messages[i]["content"]
            messages[i]["content"] = (
                f"[Tool result omitted to save context — {len(content)} chars, already processed by the agent]"
            )

def trim_messages_context(messages: list[dict], max_history: int = 20) -> None:
    """Trim the actual list of messages to prevent LLM context limit errors.

    Drops the oldest turns in pairs to preserve role alternation where possible,
    while always keeping the system prompt at messages[0].
    """
    if max_history < 3:
        max_history = 3

    while len(messages) > max_history and len(messages) > 3:
        # Preserve messages[0] (system prompt). Pop index 1 and 2 (which becomes 1 after first pop)
        messages.pop(1)
        messages.pop(1)

class StreamInterrupted(Exception):
    """Raised when a user interrupts an in-progress stream (console mode only)."""
    def __init__(self, partial_text: str, tool_calls: list = None):
        super().__init__("Streaming interrupted by user")
        self.partial_text = partial_text
        self.tool_calls = tool_calls or []

def stream_completion(client: OpenAI, model: str, messages: list[dict], on_update, throttle_every: int = 20, tool_mode: str = "xml"):
    """Stream one chat completion, calling on_update(accumulated_text) at a throttled cadence.

    This is the single place that accumulates chunks and applies bounds-checking/
    throttling; both console and TUI front-ends (single-shot and agent-loop modes)
    drive it with a different on_update callback rather than re-implementing the
    accumulation loop.

    Always returns (text, tool_calls). tool_calls is [] unless tool_mode is
    "functions" or "auto", in which case TOOLS_SCHEMA is offered to the model
    and any native function calls it makes are accumulated and returned
    alongside the text. tool_mode="xml" (the default here, used internally by
    run_subagent) never requests native tools - callers that want them must
    opt in explicitly.
    """
    parts = []
    chunk_count = 0
    tool_calls_dict = {}

    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "stream": True,
        "timeout": 120.0
    }
    if tool_mode in ("functions", "auto"):
        kwargs["tools"] = TOOLS_SCHEMA

    response = client.chat.completions.create(**kwargs)
    try:
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content = delta.content
                parts.append(content)
                chunk_count += 1
                if chunk_count % throttle_every == 0 or "\n" in content:
                    on_update("".join(parts))

            if getattr(delta, "tool_calls", None):
                for tc_chunk in delta.tool_calls:
                    idx = tc_chunk.index
                    if idx not in tool_calls_dict:
                        tool_calls_dict[idx] = {
                            "id": tc_chunk.id or "",
                            "type": tc_chunk.type or "function",
                            "function": {
                                "name": (tc_chunk.function.name or "") if tc_chunk.function else "",
                                "arguments": (tc_chunk.function.arguments or "") if tc_chunk.function else ""
                            }
                        }
                    else:
                        if tc_chunk.id:
                            tool_calls_dict[idx]["id"] += tc_chunk.id
                        if tc_chunk.function:
                            if tc_chunk.function.name:
                                tool_calls_dict[idx]["function"]["name"] += tc_chunk.function.name
                            if tc_chunk.function.arguments:
                                tool_calls_dict[idx]["function"]["arguments"] += tc_chunk.function.arguments
    except KeyboardInterrupt:
        partial_text = "".join(parts)
        on_update(partial_text)
        tool_calls_list = [v for _, v in sorted(tool_calls_dict.items())]
        raise StreamInterrupted(partial_text, tool_calls_list)

    full_text = "".join(parts)
    on_update(full_text)
    tool_calls_list = [v for _, v in sorted(tool_calls_dict.items())]
    return full_text, tool_calls_list

def build_tool_result_message(tool_results: list[dict]) -> str:
    parts = [
        f"### Execution result of {tr['tool']} on '{tr['path']}':\n{tr['result']}\n"
        for tr in tool_results
    ]
    return "\n".join(parts)

def run_subagent(client: OpenAI, model: str, target_dir: Path, agent_name: str, task: str, depth: int = 0, tui_app = None) -> str:
    """Executes a subagent with a separate message history based on its configuration."""
    if depth > 5:
        return f"Error: Maximum subagent depth exceeded."

    try:
        config = load_agent_config(target_dir, agent_name)
    except FileNotFoundError as e:
        return f"Error loading subagent '{agent_name}': {e}"

    system_prompt = config.get("system_prompt", "You are a subagent. Do your task.")
    max_iterations = config.get("max_iterations", 5)
    allowed_tools = config.get("allowed_tools", None)

    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": task}]

    subagent_runner_callable = lambda n, task: run_subagent(client, model, target_dir, n, task, depth + 1, tui_app)

    prefix = "  " * depth
    if tui_app:
        tui_app.call_from_thread(tui_app.chat_history.mount, ChatMessage(f"🚀 Spawning subagent '{agent_name}': {task}", "system"))
        tui_app.call_after_refresh(tui_app.chat_history.scroll_end, animate=False)
    else:
        console.print(f"\n[bold magenta]{prefix}🚀 Spawning subagent '{agent_name}' for task: {task}[/bold magenta]")

    for i in range(1, max_iterations + 1):
        if not tui_app:
            console.print(f"[bold magenta]{prefix}🤖 Subagent '{agent_name}' Thinking (Step {i}/{max_iterations}) ...[/bold magenta]")

        assistant_response = ""
        try:
            if tui_app:
                stream_msg = StreamMessage(f"Subagent '{agent_name}' (Step {i})")
                tui_app.call_from_thread(tui_app.chat_history.mount, stream_msg)

                def on_update_tui(text, _stream_msg=stream_msg):
                    tui_app.call_from_thread(_stream_msg.update_content, text)
                    tui_app.call_from_thread(tui_app.chat_history.scroll_end, animate=False)

                assistant_response, _ = stream_completion(client, model, messages, on_update_tui)
            else:
                with Live(console=console, refresh_per_second=8) as live:
                    def on_update_cli(text):
                        live.update(Panel(Markdown(text), title=f"[bold green]Subagent '{agent_name}' (Step {i})[/bold green]", border_style="magenta"))
                    assistant_response, _ = stream_completion(client, model, messages, on_update_cli)
        except StreamInterrupted as e:
            if not tui_app:
                console.print(f"[bold yellow]{prefix}Generation interrupted by user.[/bold yellow]")
            assistant_response = e.partial_text
            if not assistant_response:
                break
        except Exception as e:
            if not tui_app:
                console.print(f"[bold red]{prefix}API call failed:[/bold red] {e}")
            break

        if not assistant_response:
            if not tui_app:
                console.print(f"[bold red]{prefix}Received empty response from the model.[/bold red]")
            break

        messages.append({"role": "assistant", "content": assistant_response})

        tool_results = parse_and_execute_tools(target_dir, assistant_response, allowed_tools=allowed_tools, subagent_runner=subagent_runner_callable)

        if not tool_results:
            if tui_app:
                tui_app.call_from_thread(tui_app.chat_history.mount, ChatMessage(f"✔ Subagent '{agent_name}' finished.", "system"))
                tui_app.call_after_refresh(tui_app.chat_history.scroll_end, animate=False)
            else:
                console.print(f"[bold green]{prefix}✔ Subagent '{agent_name}' finished.[/bold green]")
            break

        if tui_app:
            for tr in tool_results:
                tui_app.call_from_thread(tui_app.chat_history.mount, ChatMessage(f"Tool {tr['tool']} on {tr.get('path', 'n/a')}:\n{tr['result'][:100]}...", "system"))
            tui_app.call_after_refresh(tui_app.chat_history.scroll_end, animate=False)

        result_message = build_tool_result_message(tool_results)
        if not tui_app:
            console.print(f"[bold cyan]{prefix}Sending tool results back to '{agent_name}'...[/bold cyan]")

        messages.append({"role": "user", "content": result_message, "is_tool_result": True})
        compact_old_tool_results(messages)
        trim_messages_context(messages, _MAX_HISTORY)

    # Summarize result
    if tui_app:
        tui_app.call_from_thread(tui_app.chat_history.mount, ChatMessage(f"📝 Subagent '{agent_name}' summarizing results...", "system"))
        tui_app.call_after_refresh(tui_app.chat_history.scroll_end, animate=False)
    else:
        console.print(f"[bold magenta]{prefix}📝 Subagent '{agent_name}' summarizing results...[/bold magenta]")

    messages.append({"role": "user", "content": "Please provide a concise summary of what you accomplished and the final result for the main agent."})
    summary_response = ""
    try:
        if tui_app:
            stream_msg = StreamMessage(f"Subagent '{agent_name}' Summary")
            tui_app.call_from_thread(tui_app.chat_history.mount, stream_msg)

            def on_update_summary_tui(text, _stream_msg=stream_msg):
                tui_app.call_from_thread(_stream_msg.update_content, text)
                tui_app.call_from_thread(tui_app.chat_history.scroll_end, animate=False)

            summary_response, _ = stream_completion(client, model, messages, on_update_summary_tui)
        else:
            with Live(console=console, refresh_per_second=8) as live:
                def on_update_summary_cli(text):
                    live.update(Panel(Markdown(text), title=f"[bold green]Subagent '{agent_name}' Summary[/bold green]", border_style="magenta"))
                summary_response, _ = stream_completion(client, model, messages, on_update_summary_cli)
    except Exception as e:
        summary_response = f"Error generating summary: {e}"

    return f"Subagent '{agent_name}' completed. Summary:\n{summary_response}"

def run_agent_loop(client: OpenAI, model: str, target_dir: Path, messages: list[dict], max_iterations: int):
    """Executes the agentic reasoning & execution loop with live updates."""
    subagent_runner_callable = lambda n, task: run_subagent(client, model, target_dir, n, task, depth=1)

    for i in range(1, max_iterations + 1):
        console.print(f"\n[bold blue]🤖 Agent Thinking (Step {i}/{max_iterations}) ...[/bold blue]")

        assistant_response = ""
        assistant_tool_calls = []
        try:
            with Live(console=console, refresh_per_second=8) as live:
                def on_update(text):
                    live.update(Panel(Markdown(text), title=f"[bold green]Assistant (Step {i})[/bold green]", border_style="blue"))
                assistant_response, assistant_tool_calls = stream_completion(client, model, messages, on_update, tool_mode=_TOOL_MODE)
        except StreamInterrupted as e:
            console.print("\n[bold yellow]Generation interrupted by user.[/bold yellow]")
            assistant_response = e.partial_text
            assistant_tool_calls = e.tool_calls
            if not assistant_response and not assistant_tool_calls:
                break
        except Exception as e:
            console.print(f"[bold red]API call failed:[/bold red] {e}")
            break

        if not assistant_response and not assistant_tool_calls:
            console.print("[bold red]Received empty response from the model.[/bold red]")
            break

        assistant_message = {"role": "assistant", "content": assistant_response}
        if assistant_tool_calls:
            assistant_message["tool_calls"] = assistant_tool_calls
        messages.append(assistant_message)
        save_session(_SESSION_FILE, messages)

        if assistant_tool_calls:
            tool_results = execute_native_tools(target_dir, assistant_tool_calls, allowed_tools=_ALLOWED_TOOLS)
            for tr in tool_results:
                messages.append({"role": "tool", "tool_call_id": tr["id"], "content": tr["result"]})
        else:
            tool_results = parse_and_execute_tools(target_dir, assistant_response, allowed_tools=_ALLOWED_TOOLS, subagent_runner=subagent_runner_callable)

            if not tool_results:
                console.print("[bold green]✔ No tools triggered or task complete.[/bold green]")
                break

            result_message = build_tool_result_message(tool_results)
            console.print(f"[bold cyan]Sending tool results back to LLM...[/bold cyan]")
            messages.append({"role": "user", "content": result_message, "is_tool_result": True})

        compact_old_tool_results(messages)
        trim_messages_context(messages, _MAX_HISTORY)
        save_session(_SESSION_FILE, messages)

    return messages

def run_single_prompt(client: OpenAI, model: str, messages: list[dict]):
    console.print(f"[bold blue]Sending prompt to LLM...[/bold blue]")
    try:
        with Live(console=console, refresh_per_second=8) as live:
            def on_update(text):
                live.update(Panel(Markdown(text), title="Assistant Response", border_style="blue"))
            assistant_response, _ = stream_completion(client, model, messages, on_update)
        messages.append({"role": "assistant", "content": assistant_response})
        save_session(_SESSION_FILE, messages)
    except StreamInterrupted as e:
        console.print("\n[bold yellow]Generation interrupted by user.[/bold yellow]")
        if e.partial_text:
            messages.append({"role": "assistant", "content": e.partial_text})
    except Exception as e:
        console.print(f"[bold red]API call failed:[/bold red] {e}")
    return messages

def run_console_repl(client: OpenAI, model: str, target_dir: Path, messages: list[dict], agent_mode: bool, max_iterations: int):
    console.print(Panel(f"[bold green]Target Directory:[/bold green] {target_dir.resolve()}\n[bold green]Model:[/bold green] {model}\n[bold green]Agent Mode:[/bold green] {agent_mode}", title="Console REPL Started"))
    console.print("Type your instruction below. Use [bold]/help[/bold] for available commands.")

    while True:
        try:
            user_input = console.input("\n[bold cyan]You:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[bold yellow]Exiting...[/bold yellow]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd = user_input.lower().split()[0]
            if cmd in ["/exit", "/quit"]:
                console.print("[bold yellow]Exiting...[/bold yellow]")
                break
            elif cmd == "/clear":
                sys_prompt = messages[0]
                messages.clear()
                messages.append(sys_prompt)
                console.print("[bold green]History cleared. System prompt retained.[/bold green]")
            elif cmd == "/tools":
                console.print(Panel(Markdown(_tools_info_markdown()), title="Tools", border_style="blue"))
            elif cmd == "/agents":
                console.print(Panel(Markdown(_agents_info_markdown(target_dir)), title="Agents", border_style="blue"))
            elif cmd == "/help":
                help_text = (
                    "**Available Commands:**\n"
                    "- `/help`: Show this help message\n"
                    "- `/clear`: Clear message history (retains system prompt)\n"
                    "- `/tools`: List available tool tags\n"
                    "- `/agents`: List configured agent profiles\n"
                    "- `/exit` or `/quit`: Exit the REPL"
                )
                console.print(Panel(Markdown(help_text), title="Help", border_style="blue"))
            else:
                console.print(f"[bold red]Command {cmd} not supported in Console REPL. Supported: /help, /clear, /tools, /agents, /exit[/bold red]")
            continue

        messages.append({"role": "user", "content": user_input})
        if agent_mode:
            run_agent_loop(client, model, target_dir, messages, max_iterations)
        else:
            run_single_prompt(client, model, messages)

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

class ConfirmationScreen(ModalScreen[bool]):
    CSS = """
    ConfirmationScreen {
        align: center middle;
    }
    #confirm-dialog {
        padding: 1 2;
        width: 80%;
        height: auto;
        border: thick $background 80%;
        background: $surface;
    }
    #preview-scroll {
        height: 1fr;
        max-height: 20;
        border: solid $primary;
        margin: 1 0;
    }
    """

    BINDINGS = [
        ("y", "confirm(True)", "Yes (Approve)"),
        ("n", "confirm(False)", "No (Deny)"),
    ]

    def __init__(self, title: str, preview: str):
        super().__init__()
        self.dialog_title = title
        self.preview_content = preview

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(f"[bold yellow]Requires Permission:[/bold yellow] {self.dialog_title}")
            with VerticalScroll(id="preview-scroll"):
                yield Static(self.preview_content)
            yield Label("Press [bold green]'y'[/bold green] to allow, or [bold red]'n'[/bold red] to deny.")
            with Horizontal():
                yield Button("Yes (y)", variant="success", id="btn-yes")
                yield Button("No (n)", variant="error", id="btn-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-yes":
            self.dismiss(True)
        elif event.button.id == "btn-no":
            self.dismiss(False)

    def action_confirm(self, result: bool) -> None:
        self.dismiss(result)


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
            res = tool_list_dir(self.target_dir, ".", show_ignored=_SHOW_IGNORED)
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
        elif cmd == "/tools":
            await self.chat_history.mount(ChatMessage(_tools_info_markdown(), "system"))
            self.chat_history.scroll_end(animate=False)
        elif cmd == "/agents":
            await self.chat_history.mount(ChatMessage(_agents_info_markdown(self.target_dir), "system"))
            self.chat_history.scroll_end(animate=False)
        elif cmd == "/help":
            help_text = (
                "**Available Commands:**\n"
                "- `/help`: Show this help screen\n"
                "- `/models`: Manage (list & download) local GGUF models\n"
                "- `/serve`: Serve a downloaded model using llama-server\n"
                "- `/dir`: List files in the target directory\n"
                "- `/tools`: List available tool tags\n"
                "- `/agents`: List configured agent profiles\n"
                "- `/clear`: Clear history or screen\n"
                "- `/exit` or `/quit`: Exit the interactive session"
            )
            await self.chat_history.mount(ChatMessage(help_text, "system"))
            self.chat_history.scroll_end(animate=False)
        else:
            await self.chat_history.mount(ChatMessage(f"Command {cmd} not supported in TUI yet. Supported: /exit, /clear, /dir, /models, /serve, /tools, /agents, /help", "system"))
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
            assistant_response, _ = stream_completion(self.client, self.model, self.messages, on_update)
            self.call_from_thread(self._trim_chat_history)
            self.messages.append({"role": "assistant", "content": assistant_response})
            save_session(_SESSION_FILE, self.messages)

        except Exception as e:
            self.call_from_thread(self.chat_history.mount, ChatMessage(f"Error: {e}", "system"))
        finally:
            self.is_processing = False

    @work(thread=True)
    def run_agent_loop_async(self):
        subagent_runner_callable = lambda n, task: run_subagent(self.client, self.model, self.target_dir, n, task, depth=1, tui_app=self)
        try:
            for i in range(1, self.max_iterations + 1):
                self.call_from_thread(self.chat_history.mount, ChatMessage(f"🤖 Agent Thinking (Step {i}/{self.max_iterations}) ...", "system"))
                self.call_after_refresh(self.chat_history.scroll_end, animate=False)

                stream_msg = StreamMessage(f"Assistant (Step {i})")
                self.call_from_thread(self.chat_history.mount, stream_msg)

                def on_update(text, _stream_msg=stream_msg):
                    self.call_from_thread(_stream_msg.update_content, text)
                    self.call_from_thread(self.chat_history.scroll_end, animate=False)

                assistant_response, assistant_tool_calls = stream_completion(self.client, self.model, self.messages, on_update, tool_mode=_TOOL_MODE)

                assistant_message = {"role": "assistant", "content": assistant_response}
                if assistant_tool_calls:
                    assistant_message["tool_calls"] = assistant_tool_calls
                self.messages.append(assistant_message)
                save_session(_SESSION_FILE, self.messages)

                if assistant_tool_calls:
                    tool_results = execute_native_tools(self.target_dir, assistant_tool_calls, allowed_tools=_ALLOWED_TOOLS)
                    for tr in tool_results:
                        self.messages.append({"role": "tool", "tool_call_id": tr["id"], "content": tr["result"]})
                        self.call_from_thread(self.chat_history.mount, ChatMessage(f"Tool {tr['tool']} on {tr.get('path', '')}:\n{tr['result']}", "system"))
                else:
                    # Execute tools
                    tool_results = parse_and_execute_tools(self.target_dir, assistant_response, allowed_tools=_ALLOWED_TOOLS, subagent_runner=subagent_runner_callable)

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
                trim_messages_context(self.messages, _MAX_HISTORY)
                save_session(_SESSION_FILE, self.messages)
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
    parser.add_argument("-i", "--interactive", action="store_true", help="Force launch the interactive REPL shell (TUI)")
    parser.add_argument("--console", action="store_true", help="Force launch the interactive REPL in plain console mode instead of the TUI")
    parser.add_argument("--yolo", action="store_true", help="Auto-approve destructive operations (no prompts)")
    parser.add_argument("--auto-approve", action="store_true", help="Alias for --yolo")
    parser.add_argument("--show-ignored", action="store_true", help="Do not filter out .git and .gitignore-matched files in directory listings")
    parser.add_argument("--max-history", type=int, default=20, help="Maximum number of messages to keep in context (rolling window)")
    parser.add_argument("--resume", nargs="?", const="LATEST", help="Resume a previous session (provide filename or leave blank for most recent)")
    parser.add_argument("--list-agents", action="store_true", help="List all available agent profiles (built-in and project-local) and exit")
    parser.add_argument("--create-agent", type=str, help="Scaffold a new agent profile JSON in .local-coder/agents/ with the given name")
    parser.add_argument("--agent-profile", type=str, help="Launch the main agent with a specific profile (overrides --system-prompt, forces --agent)")
    parser.add_argument("--tool-mode", choices=["xml", "functions", "auto"], default="auto", help="Tool-calling protocol: xml (legacy tags), functions (native only), or auto (offer native, model may still use XML). Only affects --agent mode.")
    parser.add_argument("prompt", nargs="?", help="The programming task / instruction for the LLM")

    args = parser.parse_args()

    global _YOLO_MODE, _SHOW_IGNORED, _MAX_HISTORY, _TOOL_MODE
    if args.yolo or args.auto_approve:
        _YOLO_MODE = True
    if args.show_ignored:
        _SHOW_IGNORED = True
    _MAX_HISTORY = args.max_history
    _TOOL_MODE = args.tool_mode

    provider, api_url, api_key, model = resolve_provider_config(
        args.provider, args.api_url, args.api_key, args.model
    )

    target_dir = Path(args.target_dir).resolve()
    if not target_dir.exists():
        console.print(f"[bold yellow]Target directory '{target_dir}' does not exist. Creating it...[/bold yellow]")
        target_dir.mkdir(parents=True, exist_ok=True)

    if args.list_agents:
        print_list_agents(target_dir)
        sys.exit(0)

    if args.create_agent:
        scaffold_create_agent(target_dir, args.create_agent)
        sys.exit(0)

    global _ALLOWED_TOOLS
    if args.agent_profile:
        agents = get_all_agents(target_dir)
        if args.agent_profile not in agents:
            console.print(f"[bold red]Error: Agent profile '{args.agent_profile}' not found.[/bold red]")
            console.print("Use --list-agents to see available profiles.")
            sys.exit(1)
        profile = agents[args.agent_profile]
        _ALLOWED_TOOLS = profile.get("allowed_tools")
        args.agent = True

    system_prompt_content = ""
    if args.agent_profile:
        system_prompt_content = agents[args.agent_profile].get("system_prompt", "You are a helpful coding assistant.")
    elif args.system_prompt:
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

    global _SESSION_FILE
    if getattr(args, "resume", None):
        _SESSION_FILE = resolve_session_file(target_dir, args.resume)
        if _SESSION_FILE:
            console.print(f"[bold green]Resuming session from {_SESSION_FILE}[/bold green]")
            messages.extend(load_session(_SESSION_FILE))
        else:
            console.print(f"[bold yellow]Warning: Could not find session to resume for '{args.resume}'. Starting new session.[/bold yellow]")
            _SESSION_FILE = target_dir / ".local-coder" / "sessions" / f"{int(time.time())}.json"
    else:
        _SESSION_FILE = target_dir / ".local-coder" / "sessions" / f"{int(time.time())}.json"

    is_interactive = args.interactive or args.console or (not args.prompt)


    if is_interactive:
        if args.console:
            run_console_repl(client, model, target_dir, messages, args.agent, args.max_iterations)
        else:
            global TUI_MODE, _APP_INSTANCE
            TUI_MODE = True
            app = LocalCoderApp(client, model, target_dir, messages, args.agent, args.max_iterations)
            _APP_INSTANCE = app
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
