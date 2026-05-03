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

Required flow when the user wants to run a task:
  1. Ask whether the task is a SCRIPT or a BINARY.
  2. Collect the path (and the script body, if SCRIPT and they want to paste it inline).
  3. Ask whether the user wants to pick a specific worker or let the system auto-assign one.
  4. If they want to pick: call list_live_workers, present the list, ask which one.
     If they want auto-assign: skip directly to submission.
  5. Generate a fresh task_id (UUID) and call create_task. Pass worker_id only when the user picked one.
  6. Confirm acceptance to the user, including the task_id and the assigned worker.

When the user asks about the status of an existing task:
  - Call get_status with the task_id and report status/result in plain language.

Keep replies short and friendly. Do not invent task_ids or worker_ids.
Do not call create_task until you have task_type, path/script, and a clear
worker preference. Always confirm before submitting.
"""


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
        "description": "Look up the status of a previously-submitted task.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
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
                if block.name in ("create_task", "get_status"):
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
                    timeout=5,
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

        # "What's the status of <task_id>?" can be asked at any point.
        # Require the task_id to start with `task_` so the regex doesn't
        # capture the literal word "of" out of "status of ...".
        m = re.search(r"\b(task_[A-Za-z0-9]+)\b", msg, re.I)
        if m:
            tid = m.group(1)
            result = self._http_get(f"/getStatus/{tid}")
            reply = self._format_status(tid, result)
            action = {"type": "get_status", "input": {"task_id": tid}, "result": result}
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
            data["path"] = msg
            if data.get("task_type") == "SCRIPT" and (msg.startswith("#!") or "\n" in msg):
                data["script"] = msg
            else:
                data["script"] = data.get("script", "")
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
                state = {"step": "DONE", "data": {}}

        elif step == "ASK_WORKER_ID":
            chosen = msg.strip()
            reply, action = self._submit(data, chosen)
            state = {"step": "DONE", "data": {}}

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
            if r.status_code == 200 and body.get("message") == "Task accepted":
                reply = (
                    f"✓ Task accepted. ID: {body['task_id']}, "
                    f"assigned to {body['worker_id']} (by {body['assigned_by']})."
                )
            else:
                reply = f"Task rejected: {body.get('reason') or body}"
            return reply, action
        except Exception as exc:
            return f"Error submitting task: {exc}", None

    def _http_get(self, path: str) -> Dict[str, Any]:
        try:
            return requests.get(f"{self.base_url}{path}", timeout=5).json()
        except Exception as exc:
            return {"error": str(exc)}

    def _format_status(self, task_id: str, result: Dict[str, Any]) -> str:
        if isinstance(result, dict) and "status" in result:
            return f"Task {task_id}: status={result['status']}, result={result.get('result')}"
        return f"Couldn't fetch status for {task_id}: {result}"

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
