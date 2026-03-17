# Claude Code Remote

Use Claude Code from your phone, tablet, or any device with Slack.

![Claude Code Remote](https://img.shields.io/badge/Claude_Code-Remote-blue?style=flat-square) ![Python 3.10+](https://img.shields.io/badge/Python-3.10+-green?style=flat-square) ![License MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)

## Quick Start with Claude Code

If you have Claude Code and a Slack MCP server already configured, paste this prompt into Claude Code and it will set everything up for you:

> Clone https://github.com/q-usmankayani/claude-code-remote to ~/claude-code-remote and set it up. Find my Slack MCP xoxc and xoxd tokens from my existing MCP config (check ~/.claude/settings.json, ~/.claude.json, .mcp.json, or any mcp config files for a slack MCP server entry — the tokens will be in the env or args, or sourced from ~/.zshrc via a wrapper script). Add SLACK_MCP_XOXC_TOKEN and SLACK_MCP_XOXD_TOKEN exports to my ~/.zshrc if they're not already there. Install requirements with uv pip install -r requirements.txt. Add a shell function to ~/.zshrc so I can type claude-remote from any directory to launch it (it should pass the current working directory as -w and forward all arguments). Then source ~/.zshrc.

After setup, start a session from any project directory:

```bash
claude-remote                          # use current directory
claude-remote --model sonnet           # use a different model
claude-remote --permission-mode bypassPermissions  # auto-approve tools
```

## How It Works

A small Python script runs on your laptop and creates a self-DM thread in Slack. You type messages in the thread, and the script pipes them to the Claude Code CLI running locally on your machine, then posts Claude's response back with a robot emoji prefix.

```
Your Phone/Tablet (Slack)          Your Laptop
┌─────────────────────┐           ┌──────────────────────┐
│  Slack Self-DM       │           │  claude_code_remote.py│
│                      │  polls    │          │            │
│  You: "fix the bug"  │◄─────────│  picks up message     │
│                      │           │          │            │
│  🤖 Done. Fixed the  │  posts    │  claude -p "fix bug"  │
│  null check in...    │◄─────────│          │            │
│                      │           │  ← Claude CLI output  │
└─────────────────────┘           └──────────────────────┘
```

Because it runs against your local CLI, Claude has full access to your codebase, MCP servers, plugins, and all the tools you'd normally use — you just interact via Slack instead of the terminal.

## Features

- **Live streaming** — watch Claude thinking in real-time as responses stream to Slack
- **Session persistence** — sessions survive script restarts, pick up where you left off
- **Non-blocking** — send commands while Claude is still processing
- **Message splitting** — long responses automatically split at code block boundaries
- **File & image support** — upload images or files in the thread, Claude reads them automatically
- **Built-in commands** — `!help`, `!status`, `!new`, `!cd`, `!stop`
- **Sleep prevention** — automatically prevents macOS from sleeping while running (via `caffeinate`)
- **Cleanup tools** — `--clean` and `--clean-all` to delete old session threads

## Platform Support

- **macOS** — fully tested, includes automatic sleep prevention via `caffeinate`
- **Linux** — should work out of the box (caffeinate silently skipped)
- **Windows (WSL)** — should work under WSL with Claude Code installed

## Prerequisites

1. **Claude Code CLI** installed and authenticated (`claude` command available)
2. **Slack user tokens** — your personal `xoxc` and `xoxd` tokens (the same ones your Slack MCP server uses)
3. **Python 3.10+**

## Installation

```bash
# Clone the repo
git clone https://github.com/q-usmankayani/claude-code-remote.git
cd claude-code-remote

# Install dependencies
pip install -r requirements.txt
# or with uv
uv pip install -r requirements.txt
```

### Shell Function (optional)

Add this to your `~/.zshrc` or `~/.bashrc` so you can run `claude-remote` from any directory:

```bash
claude-remote() {
  python ~/claude-code-remote/claude_code_remote.py -w "$(pwd)" "$@"
}
```

## Setup

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SLACK_MCP_XOXC_TOKEN` | **Yes** | Your Slack user token (`xoxc-...`). Same token used by Slack MCP servers. |
| `SLACK_MCP_XOXD_TOKEN` | **Yes** | Your Slack cookie token (`xoxd-...`). Used alongside the xoxc token for authentication. |

```bash
# Add to your ~/.zshrc or ~/.bashrc
export SLACK_MCP_XOXC_TOKEN="xoxc-..."
export SLACK_MCP_XOXD_TOKEN="xoxd-..."
```

> **Important:** These tokens grant access to your Slack account. Never commit them to version control or share them.

### Getting Your Slack Tokens

If you already use a Slack MCP server, you have these tokens. If not:

1. Open Slack in your **browser** (not the desktop app)
2. Open Developer Tools (F12) → Network tab
3. Look for any API request to `api.slack.com`
4. Find the `token` parameter (`xoxc-...`) in the request body
5. Find the `d` cookie (`xoxd-...`) in the request headers

## Usage

```bash
# Start a new session (uses current directory)
python claude_code_remote.py

# Specify a working directory
python claude_code_remote.py -w ~/projects/my-app

# Use a different model (default: claude-opus-4-6)
python claude_code_remote.py --model sonnet

# Poll every 5 seconds instead of 3
python claude_code_remote.py -i 5

# Auto-approve all tool use (use with caution)
python claude_code_remote.py --permission-mode bypassPermissions

# Resume a previous session
python claude_code_remote.py --session-id <uuid>

# List previous sessions
python claude_code_remote.py --list

# Clean up a specific session (deletes Slack messages + state)
python claude_code_remote.py --clean <session-id>

# Clean ALL sessions
python claude_code_remote.py --clean-all
```

## File & Image Uploads

Upload files directly in the Slack thread — the script downloads them via Slack's API and passes the local paths to Claude Code. Claude can then read and analyse them using its built-in tools.

**Supported:**
- **Images** (`.png`, `.jpg`, `.gif`, `.webp`, `.svg`) — Claude reads them natively (multimodal)
- **Code files** — `.py`, `.js`, `.ts`, `.scala`, `.json`, `.yaml`, etc.
- **Documents** — `.csv`, `.txt`, `.md`, `.log`, etc.
- **Any file** — saved locally and path passed to Claude

Upload with a message for context, or upload without text and Claude will analyse automatically.

Files are saved to `~/.claude/remote-files/` with timestamp prefixes to avoid collisions.

## Commands

Type these in the Slack thread:

| Command | Description |
|---------|-------------|
| `!help` | Show available commands |
| `!status` | Session info (IDs, working dir, message count) |
| `!new` | Fresh Claude session (clears context) |
| `!session <id>` | Resume a specific Claude CLI session by ID |
| `!spawn` | Start a new remote session (new thread, background process) |
| `!cd <path>` | Change working directory |
| `!stop` | Stop the remote listener |

> **Note:** Uses `!` prefix instead of `/` because Slack intercepts `/` as slash commands.

## Architecture

```
Main Thread (poll loop)              Worker Thread
────────────────────                 ─────────────
poll Slack every 3s                  wait on queue
        │                                 │
├─ !help, !status, !cd → respond    │
│  immediately (never blocks)        │
│                                    │
├─ Claude prompt → queue.put()  ──> queue.get()
│  (returns instantly)               │
│                                    ├─ run claude -p (streaming)
├─ next poll picks up new msgs       │  ├─ terminal: live stdout
│  while Claude is still running     │  ├─ Slack: chat_update every 2s
│                                    │  └─ final response posted
│                                    │
└─ ...                               └─ next item from queue
```

- **Commands** (`!help`, `!status`, etc.) are handled inline and never block
- **Claude prompts** are queued — you can send multiple while one is processing
- **Session continuity** — uses `--resume <session-id>` so Claude remembers context
- **Stream JSON** — parses Claude's `stream-json` output format for live updates

## Session State

Sessions are stored in `~/.claude/remote-sessions/` as JSON files. Each session tracks:

- Remote session ID and Claude session ID
- Slack channel and thread timestamp
- Processed message timestamps (prevents re-processing)
- Working directory and message count

## Security — "Doesn't this let Slack control your machine?"

This is a fair question, but the security boundary here is **no different from using the Slack MCP inside Claude Code**.

If you already have a Slack MCP server configured, Claude Code can already:
1. Poll a Slack thread for messages (`conversations_history`)
2. Process the message as a prompt within the current session
3. Post the response back (`conversations_add_message`)

That's functionally identical to what this script does — just implemented as a standalone process rather than a plugin loop. The Slack MCP isn't sandboxed from Claude Code's other capabilities; it's another tool Claude can use alongside Bash, file editing, etc. **The security boundary is the same in both cases: whoever has access to your Slack thread can influence what Claude Code does on your machine.**

### Why a standalone script instead of a plugin?

| | Plugin/Skill approach | This script |
|---|---|---|
| **Reliability** | Depends on Claude maintaining a polling loop — can break on context compaction or session loss | Independent process with its own event loop — runs until you stop it |
| **Session persistence** | Lost when the Claude Code session ends | Survives script restarts, resumes where it left off |
| **Streaming** | No live streaming to Slack | Real-time streaming with periodic Slack updates |
| **Sleep prevention** | Not possible from within Claude Code | Built-in `caffeinate` support on macOS |
| **Concurrency** | Blocks the Claude Code session | Non-blocking — send commands while Claude is processing |
| **Setup** | Simpler (no extra process) | Requires running a script on your machine |

In short: the plugin approach is simpler but fragile. This script is the robust version of the same idea.

### Why not use Claude Code's built-in Remote Control?

Claude Code already has a built-in [Remote Control](https://code.claude.com/docs/en/remote-control) feature that does exactly this — continue local sessions from your phone, tablet, or any browser via `claude.ai/code` or the Claude mobile app.

However, **Remote Control requires a direct Anthropic subscription**. If your organisation uses Claude Code via **Vertex AI** (Google Cloud) or **AWS Bedrock** rather than paying Anthropic directly, the Remote Control feature is not available to you. This project exists as a workaround that gives Vertex AI and Bedrock users the same remote capability via Slack.

If you have a direct Anthropic subscription, you should probably just use the built-in Remote Control instead.

## License

MIT
