"""
LLM provider abstraction.

Adding a new backend:
  1. Subclass LLMProvider and implement complete / append_response / append_tool_result
  2. Register the name in create_provider()
  3. Set LLM_PROVIDER=<name> and LLM_MODEL=<model-id> in .env
"""

import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Normalized response types ─────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]   # pre-parsed; never a raw JSON string


@dataclass
class LLMResponse:
    content: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)
    _raw: Any = field(default=None, repr=False)   # provider-native object for re-appending


# ── Abstract base ─────────────────────────────────────────────────────────────

class LLMProvider(ABC):
    """Minimal contract every LLM backend must satisfy."""

    @abstractmethod
    def complete(self, messages: List[dict], tools: List[dict]) -> LLMResponse:
        """Send messages + tools to the model; return a normalized response."""

    @abstractmethod
    def append_response(self, messages: List[dict], response: LLMResponse) -> None:
        """Append the assistant turn (including tool-call blocks) to the message list."""

    @abstractmethod
    def append_tool_result(self, messages: List[dict], tool_call_id: str, content: str) -> None:
        """Append one tool-execution result to the message list."""


# ── OpenAI ────────────────────────────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    """
    OpenAI GPT provider (default).
    Env vars: OPENAI_API_KEY, LLM_PROVIDER=openai, LLM_MODEL=gpt-4.1-mini
    """

    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI, RateLimitError as _RLE
        self._RateLimitError = _RLE
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def complete(self, messages: List[dict], tools: List[dict]) -> LLMResponse:
        for attempt in range(4):
            try:
                raw = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                )
                break
            except self._RateLimitError as exc:
                if attempt == 3:
                    raise
                wait = 15 * (2 ** attempt)   # 15s, 30s, 60s
                print(f"⏳ Rate limit hit, retrying in {wait}s… ({exc})")
                time.sleep(wait)

        msg = raw.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=json.loads(tc.function.arguments))
            for tc in (msg.tool_calls or [])
        ]
        return LLMResponse(content=msg.content, tool_calls=tool_calls, _raw=msg)

    def append_response(self, messages: List[dict], response: LLMResponse) -> None:
        # Store the raw OpenAI message object; OpenAI SDK accepts it back as-is.
        messages.append(response._raw)

    def append_tool_result(self, messages: List[dict], tool_call_id: str, content: str) -> None:
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})


# ── Anthropic ─────────────────────────────────────────────────────────────────

class AnthropicProvider(LLMProvider):
    """
    Anthropic Claude provider.
    Requires:  pip install anthropic
    Env vars:  ANTHROPIC_API_KEY, LLM_PROVIDER=anthropic, LLM_MODEL=claude-opus-4-7

    Internally the message list uses the same OpenAI-style dicts as the rest of
    the codebase; _to_anthropic_messages() converts them per-call so no other
    module needs to know about Anthropic's format.
    """

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    # -- format helpers -------------------------------------------------------

    @staticmethod
    def _openai_tools_to_anthropic(tools: List[dict]) -> List[dict]:
        return [
            {
                "name": t["function"]["name"],
                "description": t["function"].get("description", ""),
                "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]

    @staticmethod
    def _split_system(messages: List[dict]):
        """Separate system messages (Anthropic takes them as a top-level param)."""
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system") or None
        conv = [m for m in messages if m["role"] != "system"]
        return system, conv

    @staticmethod
    def _to_anthropic_messages(messages: List[dict]) -> List[dict]:
        """
        Convert the internal OpenAI-style history → Anthropic messages array.
        Consecutive tool-result entries are grouped into a single user turn
        containing tool_result content blocks, as Anthropic requires.
        """
        out: List[dict] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            role = m["role"]

            if role == "assistant":
                # append_response stores content as a list of dicts; pass through.
                out.append({"role": "assistant", "content": m["content"]})

            elif role == "tool":
                # Collect all consecutive tool results into one user turn.
                blocks = []
                while i < len(messages) and messages[i]["role"] == "tool":
                    tr = messages[i]
                    blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tr["tool_call_id"],
                        "content": tr["content"],
                    })
                    i += 1
                out.append({"role": "user", "content": blocks})
                continue   # i already advanced inside the inner loop

            else:   # user
                out.append({"role": "user", "content": m["content"]})

            i += 1
        return out

    # -- LLMProvider interface ------------------------------------------------

    def complete(self, messages: List[dict], tools: List[dict]) -> LLMResponse:
        system, conv = self._split_system(messages)
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": self._to_anthropic_messages(conv),
            "tools": self._openai_tools_to_anthropic(tools),
        }
        if system:
            kwargs["system"] = system

        raw = self.client.messages.create(**kwargs)

        text_content: Optional[str] = None
        tool_calls: List[ToolCall] = []
        for block in raw.content:
            if block.type == "text":
                text_content = block.text
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=block.input))

        # Convert SDK objects → plain dicts so _to_anthropic_messages can re-emit them.
        content_dicts = [b.model_dump() for b in raw.content]
        return LLMResponse(content=text_content, tool_calls=tool_calls, _raw=content_dicts)

    def append_response(self, messages: List[dict], response: LLMResponse) -> None:
        messages.append({"role": "assistant", "content": response._raw})

    def append_tool_result(self, messages: List[dict], tool_call_id: str, content: str) -> None:
        # Store in the same OpenAI-style dict; _to_anthropic_messages groups them at call time.
        messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})


# ── Factory ───────────────────────────────────────────────────────────────────

def create_provider(name: str, model: str) -> LLMProvider:
    """
    Instantiate the LLM provider by name.  API keys are read from the environment.

    Supported LLM_PROVIDER values:
      openai / gpt      →  OpenAIProvider    (needs OPENAI_API_KEY)
      anthropic / claude →  AnthropicProvider (needs ANTHROPIC_API_KEY)
    """
    n = (name or "openai").strip().lower()

    if n in {"openai", "gpt"}:
        return OpenAIProvider(api_key=os.getenv("OPENAI_API_KEY", ""), model=model)

    if n in {"anthropic", "claude"}:
        return AnthropicProvider(api_key=os.getenv("ANTHROPIC_API_KEY", ""), model=model)

    raise ValueError(
        f"Unknown LLM_PROVIDER={name!r}. Supported: 'openai', 'anthropic'."
    )
