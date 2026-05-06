"""
Phase 2: Conversational AI agent.

A separate, user-facing agent (NOT the placement agent in agent_loop.py).

Walks a user through:
  1. Choose Script or Binary
  2. Provide path / script content
  3. Pick a specific worker, or let the system auto-assign
  4. Submit -> /createTask
  5. Optionally check /getStatus/<task_id>

Stateless: the caller passes the full conversation history on every call.
The agent reaches /workers/live, /createTask, and /getStatus over HTTP so
the layers stay cleanly separated.
"""

import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Task Executioner conversational agent.

Walk the user through submitting a task and (optionally) checking its status.

═══════════════ SUBMITTING A TASK ═══════════════
Required flow when the user wants to run a task:
  1. Ask whether the task is a SCRIPT or a BINARY.
  2. Collect the path (and the script body, if SCRIPT and they want to paste it inline).
  3. Ask whether the user wants to pick a specific worker or let the system auto-assign one.
  4. If they want to pick: call list_live_workers, present the list, ask which one.
     If they want auto-assign: skip directly to submission.
  5. Generate a fresh task_id (UUID) and call create_task. Pass worker_id only when the user picked one.
  6. Confirm acceptance to the user, including the task_id and the assigned worker.

═══════════════ CHECKING STATUS ═══════════════
Two tools, used in different situations:

  get_status(task_id)
      Use for one-shot questions: "status of t1", "is t1 done?", "did it work?"

  wait_for_status(task_id, max_wait_seconds=120)
      Use when the user explicitly asks to wait or be notified:
      "wait for t1", "let me know when it's done", "tell me when it finishes".
      The server blocks up to max_wait_seconds and returns as soon as the
      task leaves PENDING.

INTENT EXAMPLES:
  "status of t1"                 → get_status(t1)
  "is task_xyz done yet?"        → get_status(task_xyz)
  "did it work?"                 → get_status(<most recent task_id>)
  "wait for t1 to finish"        → wait_for_status(t1)
  "let me know when it's done"   → wait_for_status(<most recent>)
  "check on that task"           → get_status(<most recent>)
  "see if it's done"             → get_status(<most recent>)
  "show me the full output"      → get_status(<most recent>) and quote result
                                   without truncating

REMEMBERING task_ids:
If the user says "my task", "it", "that task", or doesn't specify an id
at all, look back through the conversation history for the most recent
create_task tool result and use its task_id. If no task_id can be found
in history, ASK the user "Which task ID would you like me to check?"
— do NOT guess or invent one.

═══════════════ REPORTING RESULTS ═══════════════
Always lead the status reply with a single-glyph prefix:

  ✓   SUCCESS
  ✗   FAILURE (non-zero exit, not a timeout)
  ⏱   FAILURE with timed_out=true
  ⏳   PENDING (still running)
  ❓   error_code = TASK_NOT_FOUND

Always include the task_id in the reply.

Result formatting rules:
  - SUCCESS: show the task_id, a brief OK note, and stdout. Truncate
    stdout to 500 chars and append "(truncated, ask to see more)" if cut.
  - FAILURE: include exit_code and the first 500 chars of stderr (or
    stdout if stderr is empty).
  - PENDING: say "still running" and offer "Want me to wait for it?"
  - TASK_NOT_FOUND: "I don't have a record of task <id>. Did you mean a
    different ID?" — never guess.
  - Never invent fields not present in the response.

After every reply, suggest a clear next step:
  - PENDING  → "Want me to wait for it to finish?"
  - SUCCESS  → "Anything else I can help with?"
  - FAILURE  → "Want me to show the full error output?"

═══════════════ GENERAL ═══════════════
Keep replies short and friendly. Do not invent task_ids or worker_ids.
Do not call create_task until you have task_type, path/script, and a
clear worker preference. Always confirm before submitting.
"""

# Phase 5 constants
STDOUT_TRUNCATE_CHARS = 500
WAIT_FOR_STATUS_DEFAULT_S = 120
WAIT_FOR_STATUS_MAX_S = 120


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "list_live_workers",
        "description": "Return the list of currently-alive worker IDs.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_task",
        "description": (
            "Submit a new task. Provide task_type (SCRIPT or BINARY), path, and "
            "optionally script body. If the user picked a worker, pass worker_id; "
            "otherwise omit it for auto-assignment. The agent must generate a "
            "fresh task_id (UUID-like) and pass it in."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "task_type": {"type": "string", "enum": ["SCRIPT", "BINARY"]},
                "path": {"type": "string"},
                "script": {"type": "string"},
                "worker_id": {"type": "string"},
            },
            "required": ["task_id", "task_type", "path"],
        },
    },
    {
        "name": "get_status",
        "description": (
            "One-shot status check for a previously-submitted task. Returns "
            "immediately with the current state. Use for questions like "
            "'status of X', 'is X done?', 'did it work?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "wait_for_status",
        "description": (
            "Block until the task completes (status leaves PENDING) or the "
            "max_wait_seconds elapses. Use when the user explicitly asks to "
            "wait, be notified, or be told when the task finishes. Capped "
            "server-side at 120 seconds; pass an integer 1-120."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "max_wait_seconds": {
                    "type": "integer", "minimum": 1, "maximum": 120, "default": 120,
                },
            },
            "required": ["task_id"],
        },
    },
]


class ConversationAgent:
    """
    Anthropic-backed conversational agent. Uses tool calling to reach the
    frontend HTTP API.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "http://127.0.0.1:5001",
        model: str = "claude-haiku-4-5-20251001",
        max_tool_iterations: int = 6,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tool_iterations = max_tool_iterations
        self._client = None  # lazy-init

    def _client_or_raise(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not configured — set the env var or pass "
                "--agent-key when starting the node."
            )
        import anthropic
        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    # Public API

    def chat(
        self,
        history: List[Dict[str, Any]],
        message: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run one turn of the conversation.

        history: prior turns in Anthropic message format. The caller MUST pass
                 back exactly what we returned in `history` on the previous turn.
        message: the new user message.
        Returns: { "reply": str, "history": [...], "action": {...} | None }
        """
        client = self._client_or_raise()

        messages = list(history) + [{"role": "user", "content": message}]
        last_action: Optional[Dict[str, Any]] = None

        for iteration in range(self.max_tool_iterations):
            response = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )

            # Append the assistant turn (serialized to plain dicts so the
            # history can round-trip through JSON between requests).
            messages.append({
                "role": "assistant",
                "content": [_block_to_dict(b) for b in response.content],
            })

            if response.stop_reason != "tool_use":
                break

            # Execute every tool_use block from this turn, then feed results back.
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = self._execute_tool(block.name, block.input or {})
                if block.name in ("create_task", "get_status", "wait_for_status"):
                    last_action = {
                        "type": block.name,
                        "input": block.input,
                        "result": result,
                    }
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning(
                "[ConversationAgent] tool-use loop hit max iterations "
                "(%d); session=%s", self.max_tool_iterations, session_id,
            )

        reply_text = _extract_text(messages[-1]["content"]) if messages[-1]["role"] == "assistant" else ""

        return {
            "reply": reply_text,
            "history": messages,
            "action": last_action,
        }

    # Tools

    def _execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if name == "list_live_workers":
                r = requests.get(f"{self.base_url}/workers/live", timeout=5)
                return r.json()

            if name == "create_task":
                task_id = args.get("task_id") or _new_task_id()
                payload = {
                    "task_id": task_id,
                    "task_details": {
                        "task_type": args.get("task_type"),
                        "path": args.get("path", ""),
                        "script": args.get("script", ""),
                    },
                }
                if args.get("worker_id"):
                    payload["worker_id"] = args["worker_id"]
                r = requests.post(
                    f"{self.base_url}/createTask",
                    json=payload,
                    timeout=5,
                )
                return r.json()

            if name == "get_status":
                task_id = args.get("task_id")
                if not task_id:
                    return {"error": "task_id required"}
                r = requests.get(
                    f"{self.base_url}/getStatus/{task_id}",
                    timeout=10,
                )
                return r.json()

            if name == "wait_for_status":
                task_id = args.get("task_id")
                if not task_id:
                    return {"error": "task_id required"}
                wait_s = int(args.get("max_wait_seconds") or WAIT_FOR_STATUS_DEFAULT_S)
                wait_s = max(1, min(WAIT_FOR_STATUS_MAX_S, wait_s))
                r = requests.get(
                    f"{self.base_url}/getStatus/{task_id}",
                    params={"wait": wait_s},
                    # Allow a small grace beyond the server-side wait so we
                    # don't socket-timeout right before the server replies.
                    timeout=wait_s + 10,
                )
                return r.json()

            return {"error": f"unknown tool: {name}"}
        except Exception as exc:
            logger.warning("[ConversationAgent] tool %s failed: %s", name, exc)
            return {"error": str(exc)}


class ScriptedAgent:
    """
    Deterministic, no-API-key fallback that walks the same conversational
    flow via a tiny state machine.  State is embedded in the conversation
    history as a hidden assistant content block (type='scripted_state'),
    so the agent itself remains stateless across requests.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:5001"):
        self.base_url = base_url.rstrip("/")

    def chat(
        self,
        history: List[Dict[str, Any]],
        message: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        msg = (message or "").strip()
        state = self._read_state(history) or {"step": "INIT", "data": {}}
        action: Optional[Dict[str, Any]] = None

        # Phase 5: status / wait / show-full-output intents can be asked at
        # any point in the conversation. Try them first before falling into
        # the create_task state machine.
        intent_result = self._try_status_intent(msg, state)
        if intent_result is not None:
            reply, action = intent_result
            return self._return(history, message, reply, state, action)

        step = state.get("step", "INIT")
        data = dict(state.get("data") or {})

        if step == "INIT":
            low = msg.lower()
            if low in ("2", "binary") or "binary" in low:
                data["task_type"] = "BINARY"
                state = {"step": "ASK_PATH", "data": data}
                reply = "Got it — BINARY. What is the binary path?"
            elif low in ("1", "script") or "script" in low:
                data["task_type"] = "SCRIPT"
                state = {"step": "ASK_PATH", "data": data}
                reply = "Got it — SCRIPT. Paste the script body, or provide a path."
            else:
                reply = "Hi! Would you like to execute a [1] Script or [2] Binary?"

        elif step == "ASK_PATH":
            if data.get("task_type") == "SCRIPT":
                # SCRIPT inputs: prefer storing as `script` body (the common case).
                # Only treat as a `path` to an existing script file when the input
                # looks unambiguously like a filesystem path (starts with / or ./
                # AND has no whitespace).
                looks_like_path = (
                    (msg.startswith("/") or msg.startswith("./"))
                    and " " not in msg and "\n" not in msg
                )
                if looks_like_path:
                    data["path"] = msg
                    data["script"] = ""
                else:
                    data["path"] = ""
                    data["script"] = msg
            else:  # BINARY
                data["path"] = msg
                data["script"] = ""
            state = {"step": "ASK_WORKER", "data": data}
            reply = "Would you like to [1] Pick a specific worker, or [2] Let the system auto-assign one?"

        elif step == "ASK_WORKER":
            low = msg.lower()
            if low in ("1", "pick") or "pick" in low:
                live = self._http_get("/workers/live").get("live_workers", []) or []
                if not live:
                    reply = (
                        "There are no live workers right now. Reply 'auto' to use "
                        "auto-assignment, or seed a worker via /workers/heartbeat first."
                    )
                else:
                    state = {"step": "ASK_WORKER_ID", "data": data}
                    reply = f"Live workers: {', '.join(live)}. Which one?"
            else:
                reply, action = self._submit(data, None)
                state = self._post_submit_state(action)

        elif step == "ASK_WORKER_ID":
            chosen = msg.strip()
            reply, action = self._submit(data, chosen)
            state = self._post_submit_state(action)

        elif step == "DONE":
            reply = (
                "Anything else? Say 'I want to run a task' to start over, "
                "or ask 'status of <task_id>'."
            )
            if "run a task" in msg.lower() or "new task" in msg.lower():
                state = {"step": "INIT", "data": {}}
                reply = "Sure! Would you like to execute a [1] Script or [2] Binary?"

        else:
            state = {"step": "INIT", "data": {}}
            reply = "Hi! Would you like to execute a [1] Script or [2] Binary?"
        
        return self._return(history, message, reply, state, action)

    def _submit(self, data: Dict[str, Any], worker_id: Optional[str]):
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        payload = {
            "task_id": task_id,
            "task_details": {
                "task_type": data.get("task_type"),
                "path": data.get("path", ""),
                "script": data.get("script", ""),
            },
        }
        if worker_id:
            payload["worker_id"] = worker_id
        try:
            r = requests.post(f"{self.base_url}/createTask", json=payload, timeout=5)
            body = r.json()
            action = {"type": "create_task", "input": payload, "result": body}
            # Phase 3: /createTask returns 201 for new tasks, 200 for idempotent
            # duplicates ("Task already exists"). Treat both as success.
            if r.status_code in (200, 201) and body.get("message") in (
                "Task accepted", "Task already exists",
            ):
                verb = "Task accepted" if body["message"] == "Task accepted" else "Task already existed"
                reply = (
                    f"✓ {verb}. ID: {body['task_id']}, "
                    f"assigned to {body['worker_id']} (by {body['assigned_by']})."
                )
            else:
                reply = f"Task rejected: {body.get('reason') or body}"
            return reply, action
        except Exception as exc:
            return f"Error submitting task: {exc}", None

    def _http_get(self, path: str, params: Optional[Dict[str, Any]] = None,
                  timeout: float = 5.0) -> Dict[str, Any]:
        try:
            return requests.get(
                f"{self.base_url}{path}",
                params=params, timeout=timeout,
            ).json()
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Phase 5 — intent detection + status formatting
    # ------------------------------------------------------------------

    # Wait/notify-me intent — prefer LONG forms ("when it is done") over
    # bare "wait" so casual mentions aren't false positives.
    _RE_WAIT = re.compile(
        r"\b("
        r"wait\s+(for|until|till|on)|"
        r"let\s+me\s+know|"
        r"tell\s+me\s+when|"
        r"notify\s+me|"
        r"when\s+(it|the\s+task|that\s+task)(\s+is|\s*'s)?\s+(done|finished|complete|completes|ready)|"
        r"when\s+(it|the\s+task|that)(\s+is|\s*'s)?\s+done"
        r")\b",
        re.I,
    )
    # One-shot status intent
    _RE_STATUS = re.compile(
        r"\b("
        r"status|"
        r"is\s+(it|that|the\s+task|task_\w+)\s+(done|finished|ready|complete)|"
        r"did\s+(it|task_\w+)\s+(finish|complete|work|run)|"
        r"check\s+on\s+(it|that|the\s+task)|"
        r"see\s+if\s+(it|that)\s+(is\s+)?done"
        r")\b",
        re.I,
    )
    # "show me the full output" — return without truncation
    _RE_SHOW_FULL = re.compile(
        r"\bshow\s+(me\s+)?(the\s+)?full\s+(output|stdout|stderr|result)",
        re.I,
    )
    # Allow underscores so multi-segment ids like task_wait_p5_abc1234 are captured whole.
    _RE_TASK_ID = re.compile(r"\b(task_\w+)\b", re.I)

    def _try_status_intent(self, msg: str, state: Dict[str, Any]):
        """
        Detect if msg is a status-related query. Returns (reply, action) on
        match, or None if no status intent (caller continues with the
        create_task state machine).
        """
        explicit_tid_m = self._RE_TASK_ID.search(msg)
        explicit_tid = explicit_tid_m.group(1) if explicit_tid_m else None

        is_show_full = bool(self._RE_SHOW_FULL.search(msg))
        is_wait      = bool(self._RE_WAIT.search(msg))
        is_status    = bool(self._RE_STATUS.search(msg))

        # No status-flavoured intent and no explicit task_id → not a status query
        if not (is_show_full or is_wait or is_status or explicit_tid):
            return None

        tid, ask_for_id = self._resolve_task_id(explicit_tid, state)
        if ask_for_id:
            return ask_for_id, None

        if is_wait:
            wait_s = WAIT_FOR_STATUS_DEFAULT_S
            result = self._http_get(f"/getStatus/{tid}", params={"wait": wait_s},
                                    timeout=wait_s + 10)
            reply = self._format_status_reply(tid, result, full=False, after_wait=True)
            return reply, {"type": "wait_for_status",
                           "input": {"task_id": tid, "max_wait_seconds": wait_s},
                           "result": result}

        # one-shot or show-full — both call /getStatus, formatting differs
        result = self._http_get(f"/getStatus/{tid}")
        reply = self._format_status_reply(tid, result, full=is_show_full)
        return reply, {"type": "get_status",
                       "input": {"task_id": tid}, "result": result}

    def _resolve_task_id(self, explicit_tid: Optional[str], state: Dict[str, Any]):
        """
        Return (task_id, None) if resolvable. Otherwise (None, ask_message)
        — caller should send the ask_message back to the user.
        """
        if explicit_tid:
            return explicit_tid, None
        last_tid = state.get("last_task_id")
        if last_tid:
            return last_tid, None
        return None, "Which task ID would you like me to check?"

    def _post_submit_state(self, action: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """After a successful submit, persist the new task_id into state."""
        last_tid = None
        if action and isinstance(action, dict):
            inp = action.get("input") or {}
            res = action.get("result") or {}
            last_tid = inp.get("task_id") or res.get("task_id")
        return {"step": "DONE", "data": {}, "last_task_id": last_tid}

    def _format_status_reply(self, task_id: str, body: Dict[str, Any],
                              *, full: bool = False, after_wait: bool = False) -> str:
        """
        Translate /getStatus response into natural language with a glyph
        prefix, truncating long output unless full=True. after_wait adjusts
        the message for the wait-for-completion path.
        """
        if not isinstance(body, dict):
            return f"❓ Couldn't read status for {task_id} — server returned {body!r}."

        # Standardised error shapes from Phase 5
        if body.get("error_code") == "TASK_NOT_FOUND":
            return (
                f"❓ I don't have a record of task `{task_id}`. "
                f"Did you mean a different ID?"
            )
        if body.get("error_code"):
            return f"⚠️ Server error checking `{task_id}`: {body.get('message') or body}"

        status = body.get("status")
        result = body.get("result") or {}

        if status == "PENDING":
            base = (f"⏳ Task `{task_id}` is still running"
                    f"{' (no change after the wait window)' if after_wait else ''}.")
            return f"{base}\n   Want me to wait for it to finish?"

        if status == "SUCCESS":
            stdout = result.get("stdout", "") or ""
            duration = result.get("duration_ms")
            stdout_str = stdout if full else self._truncate(stdout, STDOUT_TRUNCATE_CHARS)
            note = ""
            if not full and len(stdout) > STDOUT_TRUNCATE_CHARS:
                note = "  (output truncated, ask 'show me the full output' for the rest)"
            tail = "  Anything else I can help with?"
            duration_s = f" in {duration}ms" if duration is not None else ""
            return (f"✓ Task `{task_id}` completed successfully{duration_s}.\n"
                    f"Output:\n{stdout_str}{note}\n{tail}")

        if status == "FAILURE":
            timed_out = bool(result.get("timed_out"))
            exit_code = result.get("exit_code")
            stderr = (result.get("stderr") or result.get("stdout") or "")
            stderr_str = stderr if full else self._truncate(stderr, STDOUT_TRUNCATE_CHARS)
            note = ""
            if not full and len(stderr) > STDOUT_TRUNCATE_CHARS:
                note = "  (truncated, ask 'show me the full output' for the rest)"
            tail = "  Want me to show the full error output?"
            if timed_out:
                return (f"⏱ Task `{task_id}` ran longer than the allowed time and "
                        f"was stopped.\nstderr:\n{stderr_str}{note}\n{tail}")
            return (f"✗ Task `{task_id}` failed (exit code {exit_code}).\n"
                    f"Error:\n{stderr_str}{note}\n{tail}")

        # Unknown status — defensive
        return f"❓ Task `{task_id}` has an unrecognised status: {status!r}"

    @staticmethod
    def _truncate(text: str, n: int) -> str:
        if not text:
            return "(empty)"
        return text if len(text) <= n else text[:n] + "…"

    # Kept for backward-compat; prefer _format_status_reply
    def _format_status(self, task_id: str, result: Dict[str, Any]) -> str:
        return self._format_status_reply(task_id, result, full=False)

    def _read_state(self, history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for msg in reversed(history or []):
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "scripted_state":
                    return block.get("state")
        return None

    def _return(
        self,
        history: List[Dict[str, Any]],
        user_msg: str,
        reply: str,
        state: Dict[str, Any],
        action: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        new_history = list(history) + [
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": [
                {"type": "text", "text": reply},
                {"type": "scripted_state", "state": state},
            ]},
        ]
        return {"reply": reply, "history": new_history, "action": action}


def _block_to_dict(block) -> Dict[str, Any]:
    """Convert an Anthropic content block to a plain dict for JSON round-tripping."""
    btype = getattr(block, "type", None)
    if btype == "text":
        return {"type": "text", "text": block.text}
    if btype == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    # Fallback: try the SDK's model_dump if available
    if hasattr(block, "model_dump"):
        return block.model_dump()
    return {"type": btype or "unknown"}


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts).strip()
