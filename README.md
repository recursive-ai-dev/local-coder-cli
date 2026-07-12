# Local Coder CLI

An autonomous local coding agent CLI that uses llama.cpp or LM Studio OpenAI-compatible APIs to write, modify, and manage code files directly on your local system with safe target directory constraints.

## Features
- **OpenAI-Compatible Local API Integration**: Compatible with LM Studio (`http://localhost:1234/v1`) and llama.cpp (`http://localhost:8080/v1`).
- **Filesystem Access Tools**: The LLM agent can autonomously inspect directories, read, write, and patch files in the designated target directory.
- **Custom System Prompt**: Provide a custom system prompt to tailor the coding assistant's instructions.
- **Agent Mode Loop**: Runs an agentic loop where the LLM can iteratively explore directories, check files, apply edits, and loop until the task is complete.

## Setup

1. **Clone/Copy the CLI**: Ensure `local_coder.py` and `system_prompt.txt` are in the directory.
2. **Install Dependencies**:
   Create a virtual environment and install the dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install openai rich
   ```
3. **Start Your Local LLM Provider**:
   - **LM Studio**: Turn on the "Local Server" option. By default, it runs at `http://localhost:1234/v1`.
   - **llama.cpp**: Run the llama.cpp server:
     ```bash
     ./llama-server -m your-model.gguf -c 4096 --port 8080
     ```

## Usage

### Simple Single Prompt
Queries the LLM for a response without executing filesystem tools:
```bash
./local_coder.py --api-url http://localhost:1234/v1 "Write a python function to compute fibonacci numbers"
```

### Agent Mode (Autonomous Filesystem Access)
Instructs the agent to perform edits directly in a specified workspace directory:
```bash
./local_coder.py --agent --target-dir ./test_workspace "Create a python script hello.py that prints hello world, then add a function to calculate factorial."
```

### CLI Arguments Reference

| Argument | Description | Default |
|----------|-------------|---------|
| `--api-url` | Base URL of the OpenAI-compatible local API | `http://localhost:1234/v1` |
| `--api-key` | API key (if required) | `lm-studio` |
| `--model` | Model identifier to send in the request | `local-model` |
| `--target-dir` | Path to target folder for file operations | `.` |
| `--system-prompt` | Path to custom system prompt txt file | None (uses default `system_prompt.txt`) |
| `--agent` | Enable autonomous agent loop | Disabled |
| `--max-iterations` | Maximum steps the agent can perform in a single run | `10` |
| `prompt` | The instruction or query for the coding assistant | (Required) |

## Safety
All path arguments are resolved against the target directory, preventing path traversal outside the target workspace.
