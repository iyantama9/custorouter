"""
OpenAI to Anthropic format converters for OpenChamber support
"""
import json
import time
import uuid
from typing import Dict, List, Any, Optional


def openai_to_anthropic_messages(openai_messages: List[Dict]) -> tuple[Optional[str], List[Dict]]:
    """
    Convert OpenAI chat completion messages to Anthropic format.
    Returns: (system_prompt, anthropic_messages)
    """
    system_prompt = None
    anthropic_messages = []
    pending_tool_results = []

    def flush_tool_results():
        """Emit adjacent OpenAI tool messages as one Anthropic user turn.

        OpenAI represents results for parallel calls as consecutive `tool`
        messages. Anthropic requires those results to be content blocks of
        one `user` message; emitting two user messages in a row causes a
        protocol error on the next agent turn.
        """
        nonlocal pending_tool_results
        if pending_tool_results:
            anthropic_messages.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for msg in openai_messages:
        role = msg.get("role")
        content = msg.get("content", "")

        if role == "system":
            flush_tool_results()
            # Anthropic uses separate system parameter
            if isinstance(content, str):
                system_prompt = content
            elif isinstance(content, list):
                system_prompt = " ".join([
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                ])
            continue

        if role == "assistant":
            flush_tool_results()
            # Handle tool calls
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                content_blocks = []
                # Add text content if exists
                if content:
                    content_blocks.append({"type": "text", "text": content})
                # Add tool use blocks
                for tc in tool_calls:
                    func = tc.get("function", {})
                    arguments = func.get("arguments", "{}")
                    try:
                        tool_input = json.loads(arguments) if isinstance(arguments, str) else arguments
                    except (TypeError, json.JSONDecodeError):
                        # Invalid historical calls must not turn a proxy
                        # request into an internal error. The upstream will
                        # still receive a structurally valid tool-use block.
                        tool_input = {}
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                        "name": func.get("name", ""),
                        "input": tool_input if isinstance(tool_input, dict) else {},
                    })
                anthropic_messages.append({
                    "role": "assistant",
                    "content": content_blocks
                })
            else:
                # Regular text response
                anthropic_messages.append({
                    "role": "assistant",
                    "content": content if content else ""
                })

        elif role == "tool":
            # Keep adjacent results together: one Anthropic user turn may
            # contain many tool_result blocks after a parallel tool call.
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content,
            })

        elif role == "user":
            flush_tool_results()
            # Handle user messages with possible images
            if isinstance(content, str):
                anthropic_messages.append({
                    "role": "user",
                    "content": content
                })
            elif isinstance(content, list):
                content_blocks = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            content_blocks.append({"type": "text", "text": block.get("text", "")})
                        elif block.get("type") == "image_url":
                            # Convert OpenAI image_url to Anthropic image format
                            image_url = block.get("image_url", {}).get("url", "")
                            if image_url.startswith("data:"):
                                # Extract base64 data
                                parts = image_url.split(",", 1)
                                if len(parts) == 2:
                                    media_type = parts[0].split(";")[0].replace("data:", "")
                                    content_blocks.append({
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": media_type,
                                            "data": parts[1]
                                        }
                                    })
                    elif isinstance(block, str):
                        content_blocks.append({"type": "text", "text": block})

                anthropic_messages.append({
                    "role": "user",
                    "content": content_blocks if content_blocks else ""
                })

    flush_tool_results()

    return system_prompt, anthropic_messages


def openai_tools_to_anthropic(openai_tools):
    """Convert an OpenAI-shaped `tools` array to Anthropic's `tools` shape.

    Without this, a request that arrives via the OpenAI-compatible
    /v1/chat/completions endpoint and gets dispatched to a custom provider
    (which internally speaks Anthropic shape) had its `tools` array dropped
    entirely on the floor -- the upstream model never even learned what
    functions were available, so it could only narrate intent in prose
    instead of emitting a real tool call. This is the inverse of
    convert_tools() in translator.py, which goes the other direction.
    """
    if not openai_tools:
        return []
    result = []
    for t in openai_tools:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        converted = {
            "name": name,
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {}),
        }
        if "strict" in fn:
            converted["strict"] = fn["strict"]
        result.append(converted)
    return result


def openai_tool_choice_to_anthropic(tool_choice):
    """Convert an OpenAI-shaped `tool_choice` to Anthropic's shape."""
    if not tool_choice:
        return None
    if tool_choice == "auto":
        return {"type": "auto"}
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice == "none":
        return {"type": "none"}
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        name = tool_choice.get("function", {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return None


def _created_timestamp() -> int:
    """Return a valid OpenAI-style Unix timestamp."""
    return int(time.time())


def anthropic_to_openai_response(anthropic_response: Dict, model: str) -> Dict:
    """
    Convert Anthropic response to OpenAI chat completion format.
    """
    content_blocks = anthropic_response.get("content", [])

    # Build message content
    message_content = ""
    tool_calls = []

    for block in content_blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                message_content += block.get("text", "")
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}))
                    }
                })

    # Build message object
    message = {
        "role": "assistant",
        "content": message_content or None
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    # Build usage
    usage_data = anthropic_response.get("usage", {})
    usage = {
        "prompt_tokens": usage_data.get("input_tokens", 0),
        "completion_tokens": usage_data.get("output_tokens", 0),
        "total_tokens": usage_data.get("input_tokens", 0) + usage_data.get("output_tokens", 0)
    }

    # Build full response
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
        "object": "chat.completion",
        "created": _created_timestamp(),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _map_stop_reason(anthropic_response.get("stop_reason"))
        }],
        "usage": usage
    }


def make_anthropic_to_openai_stream_converter(model: str):
    """
    Build a per-stream converter from Anthropic SSE chunks to OpenAI chunks.

    A factory rather than a plain function because tool_use blocks need
    state that spans chunks: Anthropic identifies a tool call by its
    content_block `index` (shared with text/thinking blocks in the same
    stream), but OpenAI's `tool_calls` delta needs its own zero-based index
    counting only tool calls. That mapping has to be built up as
    content_block_start events for tool_use blocks arrive and then reused
    for every input_json_delta that follows.

    Earlier this only forwarded text_delta -- content_block_start (where a
    tool call's id/name live) and input_json_delta (its arguments) had no
    handling at all, so a tool call was silently dropped from the stream:
    an agentic client calling through /v1/chat/completions against a
    custom provider got an empty response with no tool_calls and no error,
    as if the model produced nothing.
    """
    tool_index_by_block = {}

    def convert(chunk_data: Dict) -> Optional[str]:
        event_type = chunk_data.get("type")

        if event_type == "message_start":
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
                "object": "chat.completion.chunk",
                "created": _created_timestamp(),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None
                }]
            }
            return f"data: {json.dumps(chunk)}\n\n"

        elif event_type == "content_block_start":
            block = chunk_data.get("content_block", {})
            if block.get("type") == "tool_use":
                openai_idx = len(tool_index_by_block)
                tool_index_by_block[chunk_data.get("index")] = openai_idx
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
                    "object": "chat.completion.chunk",
                    "created": _created_timestamp(),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [{
                            "index": openai_idx,
                            "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                            "type": "function",
                            "function": {"name": block.get("name", ""), "arguments": ""},
                        }]},
                        "finish_reason": None
                    }]
                }
                return f"data: {json.dumps(chunk)}\n\n"
            return None

        elif event_type == "content_block_delta":
            delta = chunk_data.get("delta", {})
            delta_type = delta.get("type")

            if delta_type == "text_delta":
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
                    "object": "chat.completion.chunk",
                    "created": _created_timestamp(),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"content": delta.get("text", "")},
                        "finish_reason": None
                    }]
                }
                return f"data: {json.dumps(chunk)}\n\n"

            elif delta_type == "input_json_delta":
                openai_idx = tool_index_by_block.get(chunk_data.get("index"))
                if openai_idx is None:
                    return None
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
                    "object": "chat.completion.chunk",
                    "created": _created_timestamp(),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"tool_calls": [{
                            "index": openai_idx,
                            "function": {"arguments": delta.get("partial_json", "")},
                        }]},
                        "finish_reason": None
                    }]
                }
                return f"data: {json.dumps(chunk)}\n\n"

            elif delta_type == "thinking_delta":
                # chat.completions has no standard field for this; forward it
                # as `reasoning_content`, the same de-facto extension this
                # router already reads FROM upstream reasoning models, so any
                # client that looks for it can still show it, and one that
                # doesn't just ignores an unrecognized delta key.
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
                    "object": "chat.completion.chunk",
                    "created": _created_timestamp(),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {"reasoning_content": delta.get("thinking", "")},
                        "finish_reason": None
                    }]
                }
                return f"data: {json.dumps(chunk)}\n\n"

            return None

        elif event_type == "message_delta":
            delta = chunk_data.get("delta", {})
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                chunk = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:28]}",
                    "object": "chat.completion.chunk",
                    "created": _created_timestamp(),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "delta": {},
                        "finish_reason": _map_stop_reason(stop_reason)
                    }]
                }
                return f"data: {json.dumps(chunk)}\n\n"
            return None

        elif event_type == "message_stop":
            # The caller closes the stream with its own [DONE]; emitting one
            # here too put two of them on the wire.
            return None

        return None

    return convert

    return None


def _map_stop_reason(anthropic_stop_reason: Optional[str]) -> Optional[str]:
    """Map Anthropic stop_reason to OpenAI finish_reason."""
    if not anthropic_stop_reason:
        return None

    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls"
    }
    return mapping.get(anthropic_stop_reason, "stop")
