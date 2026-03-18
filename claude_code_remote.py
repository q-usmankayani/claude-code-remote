#!/usr/bin/env python3
"""
Claude Code Remote - Slack-based remote interface for Claude Code CLI.

Uses YOUR personal Slack credentials (xoxc/xoxd tokens) to create a self-DM
thread. You message yourself, Claude replies as you with a robot emoji prefix.

Run this script on your local machine. It:
1. Messages yourself with "🤖 Claude Code Remote Session Started"
2. Polls that thread for new replies from you
3. Skips messages that start with 🤖 (those are Claude's replies)
4. Pipes your messages to `claude -p --resume <session-id>` locally
5. Posts Claude's response back prefixed with 🤖

This gives you "remote Claude Code" accessible from any device with Slack.

Usage:
    uv run python scripts/slack-workflow/claude_code_remote.py
    uv run python scripts/slack-workflow/claude_code_remote.py --model sonnet --interval 5
    uv run python scripts/slack-workflow/claude_code_remote.py --session-id <uuid>

Requires:
    - slack_sdk
    - claude CLI installed and authenticated
    - SLACK_MCP_XOXC_TOKEN and SLACK_MCP_XOXD_TOKEN env vars
"""

import argparse
import json
import logging
import os
import queue
import re
import signal
import ssl
import subprocess
import sys
import textwrap
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SLACK_MAX_MESSAGE_LENGTH = 3900
STATE_DIR = Path.home() / ".claude" / "remote-sessions"
FILES_DIR = Path.home() / ".claude" / "remote-files"
CLAUDE_CLI = "claude"

# Emoji prefix so Claude replies are visually distinct from your messages
BOT_PREFIX = "🤖"
BOT_DIVIDER = "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
PROCESSING_EMOJI = "hourglass_flowing_sand"
DONE_EMOJI = "white_check_mark"
ERROR_EMOJI = "x"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(debug: bool = False) -> logging.Logger:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(STATE_DIR / "claude_code_remote.log"),
        ],
    )
    return logging.getLogger("claude-remote")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def split_message(text: str, max_len: int = SLACK_MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a long message into chunks that fit Slack's limits.

    Tries to split on code block boundaries first, then on newlines.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Try to split at a code block boundary
        split_point = remaining.rfind("```\n", 0, max_len)
        if split_point > max_len // 2:
            split_point += 4
        else:
            split_point = remaining.rfind("\n", 0, max_len)
            if split_point < max_len // 2:
                split_point = max_len

        chunks.append(remaining[:split_point])
        remaining = remaining[split_point:].lstrip("\n")

    return chunks


def format_for_slack(text: str) -> str:
    """Convert Claude's markdown output to Slack mrkdwn format.

    Only transforms outside of code blocks.
    """
    parts = text.split("```")
    for i in range(0, len(parts), 2):
        if i < len(parts):
            p = parts[i]
            p = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", p, flags=re.MULTILINE)
            p = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", p)
            p = re.sub(r"\*\*(.+?)\*\*", r"*\1*", p)
            parts[i] = p
    return "```".join(parts)


class SessionState:
    """Persistent state for a remote session."""

    def __init__(self, session_id: str, state_dir: Path = STATE_DIR):
        self.session_id = session_id
        self.state_file = state_dir / f"{session_id}.json"
        self.claude_session_id: Optional[str] = None
        self.thread_ts: Optional[str] = None
        self.channel_id: Optional[str] = None
        self.processed_messages: set[str] = set()
        self.message_count: int = 0
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.working_dir: Optional[str] = None
        self.load()

    def load(self):
        if self.state_file.exists():
            data = json.loads(self.state_file.read_text())
            self.claude_session_id = data.get("claude_session_id")
            self.thread_ts = data.get("thread_ts")
            self.channel_id = data.get("channel_id")
            self.processed_messages = set(data.get("processed_messages", []))
            self.message_count = data.get("message_count", 0)
            self.created_at = data.get("created_at", self.created_at)
            self.working_dir = data.get("working_dir")

    def save(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "claude_session_id": self.claude_session_id,
                    "thread_ts": self.thread_ts,
                    "channel_id": self.channel_id,
                    "processed_messages": list(self.processed_messages),
                    "message_count": self.message_count,
                    "created_at": self.created_at,
                    "working_dir": self.working_dir,
                },
                indent=2,
            )
        )


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class ClaudeCodeRemote:
    """Slack-based remote interface for Claude Code CLI.

    Uses your personal xoxc/xoxd Slack tokens to send messages as yourself.
    Claude responses are prefixed with 🤖 so you can tell them apart.
    """

    def __init__(
        self,
        working_dir: str = ".",
        check_interval: int = 3,
        session_id: Optional[str] = None,
        claude_session_id: Optional[str] = None,
        xoxc_token: Optional[str] = None,
        xoxd_token: Optional[str] = None,
        debug: bool = False,
        permission_mode: str = "default",
        model: Optional[str] = None,
    ):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.logger = setup_logging(debug)
        self.debug = debug
        self.check_interval = check_interval
        self.working_dir = os.path.abspath(working_dir)
        self.permission_mode = permission_mode
        self.model = model
        self._running = True

        # Resolve Slack user tokens (xoxc + xoxd cookie)
        self.xoxc_token = xoxc_token or os.environ.get("SLACK_MCP_XOXC_TOKEN")
        self.xoxd_token = xoxd_token or os.environ.get("SLACK_MCP_XOXD_TOKEN")

        if not self.xoxc_token or not self.xoxd_token:
            raise ValueError(
                "Slack user tokens required. Set SLACK_MCP_XOXC_TOKEN and SLACK_MCP_XOXD_TOKEN env vars.\n"
                "These are the same tokens used by your MCP Slack server."
            )

        # SSL context — skip verification (avoids issues with corp proxies)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        # Create client with user token + cookie auth
        self.client = WebClient(
            token=self.xoxc_token,
            ssl=ssl_ctx,
            headers={"cookie": f"d={self.xoxd_token}"},
        )

        # Rate limiting
        self._last_api_call: float = 0.0

        # Session state
        remote_session_id = session_id or str(uuid.uuid4())
        self.state = SessionState(remote_session_id)

        if claude_session_id:
            self.state.claude_session_id = claude_session_id

        self.state.working_dir = self.working_dir

        # Our user ID (will be populated on identify)
        self.my_user_id: Optional[str] = None

        # Claude prompt queue — commands run inline, prompts are queued
        # so the poll loop never blocks waiting for Claude
        self._prompt_queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._processing_lock = threading.Lock()

        # caffeinate process — prevents macOS from sleeping while running
        self._caffeinate_proc: Optional[subprocess.Popen] = None

        # Graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, frame):
        self.logger.info("Shutdown signal received, saving state...")
        self._running = False

    def _start_caffeinate(self):
        """Prevent macOS from sleeping using the built-in caffeinate command.

        Uses -dims flags:
          -d  prevent display sleep
          -i  prevent idle sleep
          -m  prevent disk sleep
          -s  prevent system sleep (on AC power)
        Falls back silently on non-macOS systems.
        """
        try:
            self._caffeinate_proc = subprocess.Popen(
                ["caffeinate", "-dims"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.logger.info("☕ Sleep prevention active (caffeinate)")
        except FileNotFoundError:
            self.logger.debug("caffeinate not available (non-macOS) — skipping")

    def _stop_caffeinate(self):
        if self._caffeinate_proc:
            self._caffeinate_proc.terminate()
            self._caffeinate_proc.wait(timeout=5)
            self._caffeinate_proc = None
            self.logger.info("☕ Sleep prevention released")

    def _rate_limit(self):
        elapsed = time.time() - self._last_api_call
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self._last_api_call = time.time()

    # ----- Slack operations -----

    def _identify(self):
        """Get our own user ID via auth.test."""
        self._rate_limit()
        resp = self.client.auth_test()
        self.my_user_id = resp["user_id"]
        user_name = resp.get("user", "unknown")
        self.logger.info(f"Authenticated as: {user_name} ({self.my_user_id})")

    def _open_self_dm(self) -> str:
        """Open the self-DM channel (your 'note to self')."""
        self._rate_limit()
        resp = self.client.conversations_open(users=[self.my_user_id])
        channel_id = resp["channel"]["id"]
        self.logger.info(f"Self-DM channel: {channel_id}")
        return channel_id

    def _append_divider(self, text: str) -> str:
        """Append a visual divider to bot messages."""
        if text.startswith(BOT_PREFIX) or text.startswith(":robot_face:"):
            return text + BOT_DIVIDER
        return text

    def _post_message(
        self, channel: str, text: str, thread_ts: Optional[str] = None
    ) -> Optional[str]:
        """Post a message to Slack, splitting long messages. Returns ts of first."""
        text = self._append_divider(text)
        chunks = split_message(text)
        first_ts = None

        for i, chunk in enumerate(chunks):
            if len(chunks) > 1 and i > 0:
                chunk = f"_{BOT_PREFIX} ...continued ({i + 1}/{len(chunks)})_\n{chunk}"

            self._rate_limit()
            try:
                resp = self.client.chat_postMessage(
                    channel=channel,
                    text=chunk,
                    thread_ts=thread_ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                if first_ts is None:
                    first_ts = resp["ts"]
            except SlackApiError as e:
                self.logger.error(f"Failed to post message: {e.response['error']}")
                return None

        return first_ts

    def _update_message(self, channel: str, ts: str, text: str):
        """Update an existing Slack message in place (for live streaming)."""
        try:
            text = self._append_divider(text)
            self.client.chat_update(channel=channel, ts=ts, text=text)
        except SlackApiError as e:
            self.logger.debug(f"chat_update failed: {e.response.get('error')}")

    def _add_reaction(self, channel: str, ts: str, emoji: str):
        try:
            self._rate_limit()
            self.client.reactions_add(channel=channel, timestamp=ts, name=emoji)
        except SlackApiError:
            pass

    def _remove_reaction(self, channel: str, ts: str, emoji: str):
        try:
            self._rate_limit()
            self.client.reactions_remove(channel=channel, timestamp=ts, name=emoji)
        except SlackApiError:
            pass

    def _get_thread_replies(self, channel: str, thread_ts: str) -> list[dict]:
        self._rate_limit()
        try:
            resp = self.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=200
            )
            return resp.get("messages", [])
        except SlackApiError as e:
            self.logger.error(f"Failed to get replies: {e.response['error']}")
            return []

    def _download_slack_file(self, file_info: dict) -> Optional[Path]:
        """Download a file from Slack and return the local path."""
        url = file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            self.logger.warning(f"No download URL for file: {file_info.get('name')}")
            return None

        name = file_info.get("name", "unknown")
        FILES_DIR.mkdir(parents=True, exist_ok=True)

        # Prefix with timestamp to avoid collisions
        ts = int(time.time())
        local_path = FILES_DIR / f"{ts}_{name}"

        try:
            import urllib.request

            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.xoxc_token}",
                    "Cookie": f"d={self.xoxd_token}",
                },
            )
            # Use the same SSL context as the Slack client
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, context=ssl_ctx) as resp:
                local_path.write_bytes(resp.read())

            self.logger.info(f"Downloaded: {name} -> {local_path}")
            return local_path
        except Exception as e:
            self.logger.error(f"Failed to download {name}: {e}")
            return None

    def _extract_files(self, msg: dict) -> list[Path]:
        """Download all files attached to a Slack message."""
        files = msg.get("files", [])
        if not files:
            return []

        downloaded = []
        for f in files:
            path = self._download_slack_file(f)
            if path:
                downloaded.append(path)
        return downloaded

    # ----- Claude CLI operations -----

    def _run_claude_streaming(
        self,
        prompt: str,
        slack_msg_ts: str,
    ) -> str:
        """Run claude CLI, streaming output to terminal and Slack live.

        Posts an initial placeholder message, then updates it in-place
        as output arrives from Claude. Returns the final full output.
        """
        # Use --output-format stream-json for:
        # 1. Live streaming (assistant events arrive as text is generated)
        # 2. Session ID capture (init event has session_id immediately)
        # 3. Final result extraction (result event at end)
        cmd = [CLAUDE_CLI, "-p", "--verbose", "--output-format", "stream-json"]

        # Default to claude-opus-4-6 (1M context). Override with --model flag.
        model = self.model or "claude-opus-4-6"
        cmd.extend(["--model", model])

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        if self.state.claude_session_id:
            cmd.extend(["--resume", self.state.claude_session_id])

        cmd.append(prompt)

        self.logger.info(
            f"Running Claude (session: {self.state.claude_session_id or 'new'})..."
        )

        # Strip CLAUDECODE guard (prevents nested session error)
        clean_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        clean_env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.working_dir,
                env=clean_env,
                bufsize=1,  # Line buffered
            )
        except FileNotFoundError:
            return f"Claude CLI not found at `{CLAUDE_CLI}`. Is it installed?"
        except Exception as e:
            return f"Error starting Claude: {e}"

        accumulated_text = ""
        result_text = ""
        last_update_time = 0.0
        update_interval = 2.0  # Update Slack every 2 seconds
        channel = self.state.channel_id

        try:
            # Read stream-json lines from stdout
            for raw_line in proc.stdout:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue

                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")

                # Init event — capture session ID immediately
                if event_type == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if sid:
                        self.state.claude_session_id = sid
                        self.logger.info(f"Session: {sid}")
                        self.state.save()

                # Assistant event — extract streaming text
                elif event_type == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "text":
                            text_chunk = block.get("text", "")
                            accumulated_text += text_chunk

                            # Print to terminal live
                            sys.stdout.write(text_chunk)
                            sys.stdout.flush()

                            # Update Slack periodically
                            now = time.time()
                            if now - last_update_time >= update_interval:
                                slack_text = format_for_slack(accumulated_text)
                                if len(slack_text) > SLACK_MAX_MESSAGE_LENGTH:
                                    slack_text = (
                                        f"_streaming... "
                                        f"({len(accumulated_text)} chars)_\n\n"
                                        + slack_text[-3500:]
                                    )
                                self._update_message(
                                    channel,
                                    slack_msg_ts,
                                    f"{BOT_PREFIX} {slack_text}",
                                )
                                last_update_time = now

                # Result event — final output and session ID
                elif event_type == "result":
                    result_text = event.get("result", accumulated_text)
                    sid = event.get("session_id")
                    if sid:
                        self.state.claude_session_id = sid
                        self.state.save()
                    if event.get("is_error"):
                        return f"Error: {result_text}"

            proc.wait()
            print()  # Newline after streaming

        except Exception as e:
            self.logger.error(f"Streaming error: {e}")
            proc.kill()
            return f"Streaming error: {e}"

        # Drain stderr
        stderr = proc.stderr.read().strip() if proc.stderr else ""
        if self.debug and stderr:
            self.logger.debug(f"Claude stderr: {stderr[:500]}")

        # Use result_text from the result event, or fall back to accumulated
        final = result_text or accumulated_text
        if proc.returncode != 0 and not final:
            return (
                f"Error (exit {proc.returncode}):\n"
                f"```\n{stderr[:1500]}\n```"
            )

        return final.strip() if final else "(No output from Claude)"

    # ----- Message classification -----

    def _is_bot_message(self, msg: dict) -> bool:
        """Check if a message is from Claude (our bot replies).

        We identify bot messages by the 🤖 prefix since both user and bot
        messages come from the same Slack user.

        Slack API returns emoji as either unicode (🤖) or shortcode (:robot_face:)
        depending on context, so we check for both.
        """
        text = msg.get("text", "")
        return text.startswith(BOT_PREFIX) or text.startswith(":robot_face:")

    # ----- Command processing -----

    def _handle_command(self, text: str) -> Optional[str]:
        """Handle !commands. Returns response string or None.

        Uses ! prefix instead of / because Slack intercepts / as slash commands.
        """
        text_lower = text.strip().lower()

        if text_lower in ("!status", "!info"):
            return self._status_message()

        if text_lower == "!new":
            self.state.claude_session_id = None
            self.state.save()
            return f"{BOT_PREFIX} 🆕 New Claude session started. Context cleared."

        if text_lower == "!stop":
            self._running = False
            return f"{BOT_PREFIX} 👋 Stopping Claude Code Remote. Goodbye!"

        if text_lower.startswith("!session "):
            new_session = text.strip()[9:].strip()
            if not new_session:
                return f"{BOT_PREFIX} ❌ Usage: `!session <claude-session-id>`"
            old = self.state.claude_session_id or "none"
            self.state.claude_session_id = new_session
            self.state.save()
            return (
                f"{BOT_PREFIX} 🔄 Claude session switched.\n"
                f"*From:* `{old}`\n"
                f"*To:* `{new_session}`\n"
                f"Next message will resume that session."
            )

        if text_lower == "!spawn":
            return self._spawn_session()

        if text_lower.startswith("!cd "):
            new_dir = text.strip()[4:].strip()
            expanded = os.path.expanduser(new_dir)
            if os.path.isdir(expanded):
                self.working_dir = os.path.abspath(expanded)
                self.state.working_dir = self.working_dir
                self.state.save()
                return f"{BOT_PREFIX} 📂 Working directory: `{self.working_dir}`"
            return f"{BOT_PREFIX} ❌ Directory not found: `{expanded}`"

        if text_lower.startswith("!send "):
            file_path = text.strip()[6:].strip()
            return self._send_file(file_path)

        if text_lower.startswith("!clean"):
            parts = text_lower.split()
            keep = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
            return self._clean_thread(keep)

        if text_lower.startswith("!tree"):
            parts = text.strip().split(maxsplit=1)
            path = parts[1] if len(parts) > 1 else self.working_dir
            return self._tree(path)

        if text_lower == "!diff":
            return self._git_diff()

        if text_lower == "!git":
            return self._git_status()

        if text_lower == "!menu":
            return self._menu()

        if text_lower == "!help":
            return self._help_text()

        return None

    def _status_message(self) -> str:
        return textwrap.dedent(f"""\
            {BOT_PREFIX} *Session Status*

            📋 *Remote Session:* `{self.state.session_id}`
            🧠 *Claude Session:* `{self.state.claude_session_id or 'not started'}`
            📂 *Working Dir:* `{self.working_dir}`
            💬 *Messages:* {self.state.message_count}
            🔐 *Permissions:* `{self.permission_mode}`
            ⏱️ *Interval:* {self.check_interval}s
            🕐 *Started:* {self.state.created_at[:19]}
        """)

    def _send_file(self, file_path: str) -> str:
        """Upload a file from the local machine to the Slack thread."""
        expanded = os.path.expanduser(file_path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.working_dir, expanded)

        if not os.path.isfile(expanded):
            return f"{BOT_PREFIX} ❌ File not found: `{expanded}`"

        try:
            self._rate_limit()
            self.client.files_upload_v2(
                channel=self.state.channel_id,
                file=expanded,
                filename=os.path.basename(expanded),
                thread_ts=self.state.thread_ts,
                initial_comment=f"{BOT_PREFIX} 📎 `{os.path.basename(expanded)}`",
            )
            return ""  # Empty string = handled, don't post (files_upload_v2 posts its own)
        except SlackApiError as e:
            return f"{BOT_PREFIX} ❌ Upload failed: {e.response['error']}"

    def _clean_thread(self, keep: int = 10) -> str:
        """Delete older messages in the thread, keeping the last N."""
        try:
            self._rate_limit()
            resp = self.client.conversations_replies(
                channel=self.state.channel_id,
                ts=self.state.thread_ts,
                limit=200,
            )
            messages = resp.get("messages", [])
        except SlackApiError as e:
            return f"{BOT_PREFIX} ❌ Failed to read thread: {e.response['error']}"

        # Never delete the parent message (first in list)
        deletable = [m for m in messages if m.get("ts") != self.state.thread_ts]
        if len(deletable) <= keep:
            return f"{BOT_PREFIX} Thread only has {len(deletable)} messages — nothing to clean."

        to_delete = deletable[:-keep]
        deleted = 0
        for msg in to_delete:
            try:
                time.sleep(0.3)
                self.client.chat_delete(
                    channel=self.state.channel_id, ts=msg["ts"]
                )
                deleted += 1
            except SlackApiError:
                pass  # Can't delete Slackbot messages etc.

        return (
            f"{BOT_PREFIX} 🧹 Cleaned {deleted} messages, kept last {keep}.\n"
            f"Thread now has {len(deletable) - deleted + 1} messages."
        )

    def _tree(self, path: str, max_depth: int = 3) -> str:
        """Show directory tree — mobile-friendly view of the codebase."""
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            expanded = os.path.join(self.working_dir, expanded)

        if not os.path.isdir(expanded):
            return f"{BOT_PREFIX} ❌ Directory not found: `{expanded}`"

        try:
            result = subprocess.run(
                ["find", expanded, "-maxdepth", str(max_depth), "-type", "f"],
                capture_output=True, text=True, timeout=10,
            )
            files = sorted(result.stdout.strip().split("\n"))[:100]

            # Build a simple tree
            tree_lines = [f"📂 `{expanded}`\n```"]
            for f in files:
                if not f:
                    continue
                rel = os.path.relpath(f, expanded)
                depth = rel.count(os.sep)
                indent = "  " * depth
                name = os.path.basename(f)
                tree_lines.append(f"{indent}├── {name}")
            tree_lines.append("```")

            output = "\n".join(tree_lines)
            if len(output) > SLACK_MAX_MESSAGE_LENGTH - 100:
                output = output[:SLACK_MAX_MESSAGE_LENGTH - 150] + "\n... (truncated)```"

            return f"{BOT_PREFIX} {output}"
        except Exception as e:
            return f"{BOT_PREFIX} ❌ Tree failed: {e}"

    def _git_diff(self) -> str:
        """Show git diff for the working directory."""
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "HEAD"],
                capture_output=True, text=True, timeout=10,
                cwd=self.working_dir,
            )
            diff_stat = result.stdout.strip()
            if not diff_stat:
                # Try staged
                result = subprocess.run(
                    ["git", "diff", "--stat", "--staged"],
                    capture_output=True, text=True, timeout=10,
                    cwd=self.working_dir,
                )
                diff_stat = result.stdout.strip()
                if not diff_stat:
                    return f"{BOT_PREFIX} ✅ No changes — working tree clean."
                label = "Staged changes"
            else:
                label = "Unstaged changes"

            return f"{BOT_PREFIX} 📝 *{label}:*\n```\n{diff_stat[:3500]}\n```"
        except Exception as e:
            return f"{BOT_PREFIX} ❌ Git diff failed: {e}"

    def _git_status(self) -> str:
        """Show git status for the working directory."""
        try:
            result = subprocess.run(
                ["git", "status", "--short", "--branch"],
                capture_output=True, text=True, timeout=10,
                cwd=self.working_dir,
            )
            status = result.stdout.strip()
            if not status:
                return f"{BOT_PREFIX} ✅ Clean working tree."
            return f"{BOT_PREFIX} 📊 *Git Status:*\n```\n{status[:3500]}\n```"
        except Exception as e:
            return f"{BOT_PREFIX} ❌ Git status failed: {e}"

    def _menu(self) -> str:
        """Mobile-friendly action menu."""
        return textwrap.dedent(f"""\
            {BOT_PREFIX} *📱 Quick Actions*

            *Session:*
            `!status` — Info  •  `!new` — Reset  •  `!stop` — End
            `!session <id>` — Resume session  •  `!spawn` — New thread

            *Files & Code:*
            `!tree` — Browse files  •  `!diff` — See changes
            `!git` — Git status  •  `!send <path>` — Upload file
            `!cd <path>` — Change directory

            *Housekeeping:*
            `!clean` — Trim thread (keep 10)  •  `!clean 5` — Keep 5

            _Or just type a message to talk to Claude._
        """)

    def _help_text(self) -> str:
        """Full help text with all commands."""
        return textwrap.dedent(f"""\
            {BOT_PREFIX} *Claude Code Remote — Commands*

            *Session Management:*
            `!help` — Show this help
            `!menu` — Quick action menu (mobile-friendly)
            `!status` — Session info (IDs, working dir, message count)
            `!new` — Fresh Claude session (clear context)
            `!session <id>` — Resume a specific Claude CLI session
            `!spawn` — Start a new remote session (new thread)
            `!cd <path>` — Change working directory
            `!stop` — Stop the remote listener

            *Files & Code:*
            `!send <path>` — Upload a file to this thread
            `!tree [path]` — Browse directory structure
            `!diff` — Show git diff (changed files)
            `!git` — Show git status

            *Housekeeping:*
            `!clean [n]` — Delete old messages, keep last n (default 10)

            *How it works:*
            • Your messages → Claude Code CLI on your machine
            • {BOT_PREFIX} prefixed messages → Claude's responses
            • Upload images/files — they're downloaded and passed to Claude
            • Code blocks, long responses auto-split
            • Session persists across script restarts
        """)

    def _spawn_session(self) -> str:
        """Spawn a new remote session as a background process with its own Slack thread."""
        new_session_id = str(uuid.uuid4())
        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "-w", self.working_dir,
            "-i", str(self.check_interval),
            "--session-id", new_session_id,
            "--permission-mode", self.permission_mode,
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.debug:
            cmd.append("--debug")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # Detach from parent process
            )
            self.logger.info(
                f"Spawned new session {new_session_id[:8]}... (PID: {proc.pid})"
            )
            return (
                f"{BOT_PREFIX} 🚀 Spawned new remote session!\n"
                f"*Session:* `{new_session_id[:8]}...`\n"
                f"*PID:* `{proc.pid}`\n"
                f"*Working Dir:* `{self.working_dir}`\n"
                f"A new thread will appear in your DMs shortly."
            )
        except Exception as e:
            return f"{BOT_PREFIX} ❌ Failed to spawn: {e}"

    # ----- Main loop -----

    def start(self):
        """Start the remote session."""
        self.logger.info("=" * 60)
        self.logger.info("  🤖 Claude Code Remote")
        self.logger.info("=" * 60)

        self._start_caffeinate()
        self._identify()

        # Resume or create thread
        if self.state.channel_id and self.state.thread_ts:
            self.logger.info(f"Resuming session: {self.state.session_id}")
            self.logger.info(f"Thread: {self.state.thread_ts}")
            # Post a "back online" message in the thread
            self._post_message(
                self.state.channel_id,
                f"{BOT_PREFIX} 🔄 Claude Code Remote reconnected.\n"
                f"Working dir: `{self.working_dir}`",
                thread_ts=self.state.thread_ts,
            )
        else:
            channel_id = self._open_self_dm()
            self.state.channel_id = channel_id

            start_msg = self._build_start_message()
            ts = self._post_message(channel_id, start_msg)
            if not ts:
                self.logger.error("Failed to post start message")
                sys.exit(1)

            self.state.thread_ts = ts
            self.state.save()

            self.logger.info(f"Session started! Thread ts: {ts}")

        # Start the Claude worker thread (processes prompts from queue)
        self._worker_thread = threading.Thread(
            target=self._claude_worker, daemon=True
        )
        self._worker_thread.start()

        self.logger.info(
            f"Polling every {self.check_interval}s — Ctrl+C to stop"
        )

        while self._running:
            try:
                self._poll_and_process()
            except Exception as e:
                self.logger.error(f"Poll error: {e}", exc_info=self.debug)
                time.sleep(5)
            time.sleep(self.check_interval)

        # Shutdown — drain the queue
        self._prompt_queue.put(None)  # Sentinel to stop worker
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=10)
        self._stop_caffeinate()
        self.state.save()
        self._post_message(
            self.state.channel_id,
            f"{BOT_PREFIX} 🛑 Session ended. Run the script again to resume.",
            thread_ts=self.state.thread_ts,
        )
        self.logger.info("Session ended. State saved.")

    def _build_start_message(self) -> str:
        repo_name = os.path.basename(self.working_dir)
        return textwrap.dedent(f"""\
            {BOT_PREFIX} *Claude Code Remote Session Started*

            📂 *Working Directory:* `{self.working_dir}` ({repo_name})
            🆔 *Session:* `{self.state.session_id[:8]}...`
            🔐 *Permissions:* `{self.permission_mode}`
            🕐 *Started:* {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

            Reply in this thread to talk to Claude.
            Type `!help` for commands.
        """)

    def _claude_worker(self):
        """Background worker that processes Claude prompts from the queue.

        Runs in a daemon thread. Processes one prompt at a time (Claude CLI
        calls must be sequential for session continuity). Commands (!help etc.)
        are handled inline in the poll loop and never hit this queue.
        """
        while self._running:
            try:
                item = self._prompt_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is None:  # Shutdown sentinel
                break

            text, msg_ts = item

            try:
                # Post placeholder that will be updated live
                placeholder_ts = self._post_message(
                    self.state.channel_id,
                    f"{BOT_PREFIX} _thinking..._",
                    thread_ts=self.state.thread_ts,
                )

                response = self._run_claude_streaming(
                    text, slack_msg_ts=placeholder_ts
                )
                slack_response = format_for_slack(response)

                # Final update with complete response
                final_text = f"{BOT_PREFIX} {slack_response}"
                if len(final_text) <= SLACK_MAX_MESSAGE_LENGTH:
                    self._update_message(
                        self.state.channel_id, placeholder_ts, final_text
                    )
                else:
                    # Too long — delete placeholder and post as split messages
                    try:
                        self.client.chat_delete(
                            channel=self.state.channel_id, ts=placeholder_ts
                        )
                    except SlackApiError:
                        pass
                    self._post_message(
                        self.state.channel_id,
                        final_text,
                        thread_ts=self.state.thread_ts,
                    )

                self._remove_reaction(
                    self.state.channel_id, msg_ts, PROCESSING_EMOJI
                )
                self._add_reaction(
                    self.state.channel_id, msg_ts, DONE_EMOJI
                )
            except Exception as e:
                self._remove_reaction(
                    self.state.channel_id, msg_ts, PROCESSING_EMOJI
                )
                self._add_reaction(
                    self.state.channel_id, msg_ts, ERROR_EMOJI
                )
                self._post_message(
                    self.state.channel_id,
                    f"{BOT_PREFIX} ❌ Error: ```{e}```",
                    thread_ts=self.state.thread_ts,
                )

            self.state.message_count += 1
            self.state.save()
            self._prompt_queue.task_done()

    def _poll_and_process(self):
        """Poll for new thread replies and process user messages."""
        messages = self._get_thread_replies(
            self.state.channel_id, self.state.thread_ts
        )
        if not messages:
            return

        for msg in messages:
            ts = msg.get("ts")

            # Skip processed
            if ts in self.state.processed_messages:
                continue

            # Skip original start message
            if ts == self.state.thread_ts:
                self.state.processed_messages.add(ts)
                continue

            # Skip our own bot replies (identified by 🤖 prefix)
            if self._is_bot_message(msg):
                self.state.processed_messages.add(ts)
                continue

            # Skip non-message subtypes (except file_share)
            subtype = msg.get("subtype")
            if subtype and subtype != "file_share":
                self.state.processed_messages.add(ts)
                continue

            text = msg.get("text", "").strip()

            # Download any attached files (images, documents, etc.)
            file_paths = self._extract_files(msg)

            if not text and not file_paths:
                self.state.processed_messages.add(ts)
                continue

            # Prepend file references so Claude can read them
            if file_paths:
                file_lines = []
                for fp in file_paths:
                    suffix = fp.suffix.lower()
                    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
                        file_lines.append(f"[Attached image: {fp}]")
                    else:
                        file_lines.append(f"[Attached file: {fp}]")
                file_context = "\n".join(file_lines)
                if text:
                    text = f"{file_context}\n\n{text}"
                else:
                    text = f"{file_context}\n\nThe user attached the above file(s). Read and analyse them."

            self.logger.info(
                f"📩 New: {text[:80]}{'...' if len(text) > 80 else ''}"
            )

            # Mark as processing
            self.state.processed_messages.add(ts)
            self._add_reaction(self.state.channel_id, ts, PROCESSING_EMOJI)

            # Handle commands
            cmd_response = self._handle_command(text)
            if cmd_response is not None:
                self._remove_reaction(self.state.channel_id, ts, PROCESSING_EMOJI)
                self._add_reaction(self.state.channel_id, ts, DONE_EMOJI)
                if cmd_response:  # Empty string = handled silently (e.g. file upload)
                    self._post_message(
                        self.state.channel_id,
                        cmd_response,
                        thread_ts=self.state.thread_ts,
                    )
                self.state.message_count += 1
                self.state.save()
                continue

            # Queue the prompt for the Claude worker thread
            # This returns immediately so the poll loop keeps running
            queue_size = self._prompt_queue.qsize()
            if queue_size > 0:
                self._post_message(
                    self.state.channel_id,
                    f"{BOT_PREFIX} _queued ({queue_size} ahead)..._",
                    thread_ts=self.state.thread_ts,
                )
            self._prompt_queue.put((text, ts))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _get_session_files() -> list[Path]:
    """Get all session JSON files sorted by modification time (newest first)."""
    if not STATE_DIR.exists():
        return []
    files = sorted(STATE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [f for f in files if f.name != "claude_code_remote.log"]


def _make_slack_client():
    """Create a Slack client using xoxc/xoxd tokens."""
    xoxc = os.environ.get("SLACK_MCP_XOXC_TOKEN")
    xoxd = os.environ.get("SLACK_MCP_XOXD_TOKEN")
    if not xoxc or not xoxd:
        raise ValueError(
            "SLACK_MCP_XOXC_TOKEN and SLACK_MCP_XOXD_TOKEN env vars required.\n"
            "Run: source ~/.zshrc"
        )
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    return WebClient(token=xoxc, ssl=ssl_ctx, headers={"cookie": f"d={xoxd}"})


def list_sessions():
    """List saved remote sessions."""
    sessions = _get_session_files()
    if not sessions:
        print("No sessions found.")
        return

    print(f"{'#':>3}  {'Session ID':<40} {'Msgs':>5}  {'Created':<20}  {'Working Dir'}")
    print("-" * 110)
    for i, sf in enumerate(sessions[:20], 1):
        try:
            data = json.loads(sf.read_text())
            sid = data.get("session_id", "?")[:36]
            count = data.get("message_count", 0)
            created = data.get("created_at", "?")[:19]
            wd = data.get("working_dir", "?")
            if wd and len(wd) > 40:
                wd = "..." + wd[-37:]
            print(f"{i:>3}  {sid:<40} {count:>5}  {created:<20}  {wd}")
        except Exception:
            pass


def clean_session(session_id: str):
    """Delete all Slack messages for a session and remove its state file."""
    state_file = STATE_DIR / f"{session_id}.json"
    if not state_file.exists():
        print(f"Session not found: {session_id}")
        return False

    data = json.loads(state_file.read_text())
    channel_id = data.get("channel_id")
    thread_ts = data.get("thread_ts")

    if not channel_id or not thread_ts:
        print(f"Session {session_id[:8]}... has no Slack thread. Removing state file.")
        state_file.unlink()
        return True

    print(f"🧹 Cleaning session {session_id[:8]}...")
    print(f"   Channel: {channel_id}, Thread: {thread_ts}")

    try:
        client = _make_slack_client()
    except ValueError as e:
        print(f"❌ {e}")
        return False

    # Get all messages in the thread
    deleted = 0
    try:
        time.sleep(1)
        resp = client.conversations_replies(channel=channel_id, ts=thread_ts, limit=200)
        messages = resp.get("messages", [])
        print(f"   Found {len(messages)} messages in thread")

        # Delete thread replies first (newest to oldest), then the parent
        for msg in reversed(messages):
            msg_ts = msg.get("ts")
            try:
                time.sleep(0.5)  # Rate limit
                client.chat_delete(channel=channel_id, ts=msg_ts)
                deleted += 1
            except SlackApiError as e:
                err = e.response.get("error", "unknown")
                if err == "message_not_found":
                    continue
                print(f"   ⚠️  Could not delete {msg_ts}: {err}")
    except SlackApiError as e:
        print(f"   ⚠️  Could not read thread: {e.response.get('error', 'unknown')}")

    # Remove state file
    state_file.unlink()
    print(f"   ✅ Deleted {deleted} messages, removed state file")
    return True


def clean_all_sessions():
    """Delete all Slack messages and state files for all sessions."""
    sessions = _get_session_files()
    if not sessions:
        print("No sessions to clean.")
        return

    print(f"🧹 Cleaning {len(sessions)} session(s)...\n")
    cleaned = 0
    for sf in sessions:
        try:
            data = json.loads(sf.read_text())
            sid = data.get("session_id", sf.stem)
            if clean_session(sid):
                cleaned += 1
            print()
        except Exception as e:
            print(f"   ❌ Error cleaning {sf.name}: {e}")

    print(f"Done. Cleaned {cleaned}/{len(sessions)} sessions.")


def main():
    parser = argparse.ArgumentParser(
        description="🤖 Claude Code Remote — Slack self-DM interface for Claude Code CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              # Start a new session
              uv run python claude_code_remote.py

              # Use sonnet model, poll every 5s
              uv run python claude_code_remote.py --model sonnet -i 5

              # Resume a previous remote session
              uv run python claude_code_remote.py --session-id <uuid>

              # Auto-approve all tool use
              uv run python claude_code_remote.py --permission-mode bypassPermissions

              # List previous sessions
              uv run python claude_code_remote.py --list

              # Clean up a specific session (deletes Slack messages + state)
              uv run python claude_code_remote.py --clean <session-id>

              # Clean ALL sessions
              uv run python claude_code_remote.py --clean-all

            Environment variables:
              SLACK_MCP_XOXC_TOKEN  Your Slack xoxc token (same as MCP config)
              SLACK_MCP_XOXD_TOKEN  Your Slack xoxd cookie token
        """),
    )
    parser.add_argument(
        "--working-dir",
        "-w",
        default=".",
        help="Working directory for Claude CLI (default: current dir)",
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=int,
        default=3,
        help="Polling interval in seconds (default: 3)",
    )
    parser.add_argument(
        "--session-id",
        help="Resume a previous remote session by ID",
    )
    parser.add_argument(
        "--claude-session",
        help="Connect to a specific Claude Code session ID",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=None,
        help="Claude model (e.g. 'sonnet', 'opus'). Default: settings.json",
    )
    parser.add_argument(
        "--permission-mode",
        default="default",
        choices=["default", "plan", "auto", "bypassPermissions"],
        help="Claude CLI permission mode (default: default)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List previous remote sessions and exit",
    )
    parser.add_argument(
        "--clean",
        metavar="SESSION_ID",
        help="Delete all Slack messages for a session and remove its state",
    )
    parser.add_argument(
        "--clean-all",
        action="store_true",
        help="Delete all Slack messages and state for ALL sessions",
    )

    args = parser.parse_args()

    if args.list:
        list_sessions()
        return 0

    if args.clean_all:
        clean_all_sessions()
        return 0

    if args.clean:
        clean_session(args.clean)
        return 0

    try:
        remote = ClaudeCodeRemote(
            working_dir=args.working_dir,
            check_interval=args.interval,
            session_id=args.session_id,
            claude_session_id=args.claude_session,
            debug=args.debug,
            permission_mode=args.permission_mode,
            model=args.model,
        )
        remote.start()
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.debug:
            import traceback

            traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
