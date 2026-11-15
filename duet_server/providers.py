"""Provider-neutral streaming; no dependency on vendor SDKs."""
import codecs
import hashlib
import json
import re
from urllib.parse import urlparse

import aiohttp


def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("config is required")
    config = dict(protocol="openai", max_tokens=8192, system_prompt="", allow_tools=True, auto_approve=False,
                  prompt_cache_enabled=True, stream_usage_enabled=True, api_mode="chat_completions",
                  reasoning_effort=None, prompt_cache_mode=None, prompt_cache_ttl=None)
    config.update(raw)
    if config["protocol"] not in ("openai", "anthropic"):
        raise ValueError("protocol must be openai or anthropic")
    for field in ("base_url", "model"):
        if not isinstance(config.get(field), str) or not config[field].strip():
            raise ValueError(f"{field} is required")
    url = urlparse(config["base_url"])
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
        raise ValueError("base_url must be an HTTP(S) URL without embedded credentials")
    if url.query or url.fragment:
        raise ValueError("base_url must not contain query or fragment")
    if not isinstance(config.get("api_key", ""), str):
        raise ValueError("api_key must be text")
    config.setdefault("api_key", "")
    if type(config["max_tokens"]) is not int or not 1 <= config["max_tokens"] <= 131072:
        raise ValueError("max_tokens must be 1..131072")
    if not isinstance(config["system_prompt"], str):
        raise ValueError("system_prompt must be text")
    for key in ("allow_tools", "auto_approve", "prompt_cache_enabled", "stream_usage_enabled"):
        if type(config[key]) is not bool:
            raise ValueError(f"{key} must be boolean")
    if config["api_mode"] not in ("chat_completions", "responses"):
        raise ValueError("api_mode must be chat_completions or responses")
    if config["protocol"] != "openai" and config["api_mode"] != "chat_completions":
        raise ValueError("Responses mode requires the openai protocol")
    if config["reasoning_effort"] not in (None, "none", "minimal", "low", "medium", "high", "xhigh", "max"):
        raise ValueError("Unsupported reasoning_effort")
    if config["protocol"] == "anthropic" and config["reasoning_effort"] is not None:
        raise ValueError("reasoning_effort is an OpenAI setting")
    if config["prompt_cache_mode"] not in (None, "implicit", "explicit"):
        raise ValueError("prompt_cache_mode must be implicit or explicit")
    if config["prompt_cache_ttl"] not in (None, "30m", "12h"):
        raise ValueError("prompt_cache_ttl must be 30m or 12h")
    if config["api_mode"] != "responses" and any(config[key] is not None for key in ("prompt_cache_mode", "prompt_cache_ttl")):
        raise ValueError("Cache mode and TTL settings require Responses mode")
    if config["protocol"] == "openai" and config["model"] == "gpt-6-luna":
        if config["max_tokens"] > 128000:
            raise ValueError("gpt-6-luna max_tokens must not exceed 128000")
        if config["reasoning_effort"] == "minimal":
            raise ValueError("gpt-6-luna does not support minimal reasoning effort")
        if config["api_mode"] == "chat_completions" and config["allow_tools"]:
            if config["reasoning_effort"] not in (None, "none"):
                raise ValueError("gpt-6-luna tool calling with reasoning requires Responses mode")
            config["reasoning_effort"] = "none"
    endpoint(config)
    return config


async def bounded_response_body(content, limit=8 * 1024 * 1024):
    """Read through EOF without assuming a socket read contains the full JSON."""
    body = bytearray()
    async for chunk in content.iter_chunked(64 * 1024):
        if len(body) + len(chunk) > limit:
            raise RuntimeError("provider response exceeds limit")
        body.extend(chunk)
    return bytes(body)


async def sse_events(content):
    """Decode arbitrary byte boundaries, CR/LF, comments and multiline data."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    data = []
    async for chunk in content.iter_any():
        buffer += decoder.decode(chunk)
        if len(buffer) > 8 * 1024 * 1024:
            raise RuntimeError("provider SSE event exceeds limit")
        while True:
            match = re.search(r"\r\n|\r|\n", buffer)
            if match is None or (match.group() == "\r" and match.end() == len(buffer)):
                break
            line, buffer = buffer[:match.start()], buffer[match.end():]
            if not line:
                if data:
                    yield "\n".join(data)
                    data = []
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
                if sum(map(len, data)) > 8 * 1024 * 1024:
                    raise RuntimeError("provider SSE event exceeds limit")
    buffer += decoder.decode(b"", final=True)
    if buffer.startswith("data:"):
        data.append(buffer[5:].removeprefix(" ").rstrip("\r"))
    if data:
        yield "\n".join(data)


def endpoint(config):
    base = config["base_url"].rstrip("/")
    if config["protocol"] == "openai":
        suffix = "/responses" if config.get("api_mode") == "responses" else "/chat/completions"
        if base.endswith(suffix):
            return base
        if base.endswith(("/responses", "/chat/completions")):
            raise ValueError("base_url endpoint does not match api_mode")
        return base + suffix
    if base.endswith("/messages"):
        return base
    return base + ("/messages" if base.endswith("/v1") else "/v1/messages")


USAGE_FIELDS = {
    "openai": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
               "prompt_tokens_details": {"cached_tokens": None, "cache_write_tokens": None, "audio_tokens": None, "image_tokens": None, "text_tokens": None,
                                         "cache_write_tokens_details": {"ttl_30m_tokens": None, "ttl_12h_tokens": None}},
               "completion_tokens_details": {"reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens", "text_tokens"}},
    "anthropic": {"input_tokens": None, "output_tokens": None, "cache_creation_input_tokens": None,
                  "cache_read_input_tokens": None,
                  "cache_creation": {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"},
                  "server_tool_use": {"web_search_requests", "web_fetch_requests"}},
}


def clean_usage(protocol, value):
    """Retain documented counters only, never arbitrary provider metadata/errors."""
    if not isinstance(value, dict):
        return {}
    def counter(number):
        return type(number) is int and 0 <= number <= 2 ** 53 - 1
    def select(source, schema):
        result = {}
        for field, children in schema.items():
            item = source.get(field)
            if children is None:
                if counter(item):
                    result[field] = item
            elif isinstance(item, dict):
                child_schema = children if isinstance(children, dict) else dict.fromkeys(sorted(children))
                selected = select(item, child_schema)
                if selected:
                    result[field] = selected
        return result
    return select(value, USAGE_FIELDS[protocol])


def responses_usage(value):
    """Normalize actual Responses counters into the existing OpenAI usage view."""
    if not isinstance(value, dict):
        return {}
    return clean_usage("openai", {"prompt_tokens": value.get("input_tokens"),
        "completion_tokens": value.get("output_tokens"), "total_tokens": value.get("total_tokens"),
        "prompt_tokens_details": value.get("input_tokens_details"),
        "completion_tokens_details": value.get("output_tokens_details")})


def merge_usage(protocol, previous, update):
    """Streaming counters are cumulative snapshots, not additive deltas."""
    result = clean_usage(protocol, previous)
    for field, value in clean_usage(protocol, update).items():
        if isinstance(value, dict):
            result[field] = dict(result.get(field, {}), **value)
        else:
            result[field] = value
    return result


def prompt_cache_key(config, session_id):
    """Opaque stable routing hint; no API key, username, prompt or random salt."""
    identity = json.dumps([session_id, config["protocol"], config["model"], endpoint(config)], separators=(",", ":"))
    return "duet_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:56]


def openai_messages(messages, system):
    result = [{"role": "system", "content": system}] if system else []
    observations = []
    for msg in messages:
        role = msg["role"]
        if role != "tool" and observations:
            result.append({"role": "user", "content": observations})
            observations = []
        if role == "tool":
            result.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": msg["text"]})
            # Tool messages cannot contain image blocks in Chat Completions. A separate
            # user observation carries the actual pixels back to visual models.
            if msg.get("_images"):
                observations.extend([
                    {"type": "text", "text": "Tool screenshot observation"},
                    *[{"type": "image_url", "image_url": {"url": "data:image/png;base64," + im}} for im in msg["_images"]]])
            continue
        content = msg["text"]
        if msg.get("_images"):
            content = [{"type": "text", "text": content or "Attached image"},
                       *[{"type": "image_url", "image_url": {"url": "data:" + im["mime"] + ";base64," + im["data"]}}
                         for im in msg["_images"]]]
        item = {"role": role, "content": content}
        if msg.get("tool_calls"):
            item["tool_calls"] = [{"id": t["id"], "type": "function", "function": {
                "name": t["name"], "arguments": json.dumps(t["arguments"])}} for t in msg["tool_calls"]]
        result.append(item)
    if observations:
        result.append({"role": "user", "content": observations})
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def responses_identity(config, tools):
    return _digest([config["protocol"], config.get("api_mode"), endpoint(config), config["model"],
                    config.get("_model_profile_id"), _digest(config["api_key"]), config["system_prompt"],
                    config.get("reasoning_effort"), sorted(tools, key=lambda item: item["name"])])


def responses_output(value):
    """Keep only replayable output fields; never persist an entire provider object."""
    if not isinstance(value, list) or len(value) > 128 or len(json.dumps(value)) > 4 * 1024 * 1024:
        raise RuntimeError("invalid Responses output")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError("invalid Responses output item")
        kind = item.get("type")
        if item.get("status") not in (None, "completed"):
            raise RuntimeError("Responses output item did not complete")
        fields = {"id", "type", "status"}
        if kind == "reasoning":
            fields |= {"encrypted_content", "summary"}
            summary = item.get("summary", [])
            if "encrypted_content" in item and not isinstance(item["encrypted_content"], str):
                raise RuntimeError("invalid encrypted reasoning")
            if not isinstance(summary, list) or any(not isinstance(x, dict) or x.get("type") != "summary_text" or not isinstance(x.get("text"), str) for x in summary):
                raise RuntimeError("invalid reasoning summary")
        elif kind == "message":
            fields |= {"role", "phase", "content"}
            if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
                raise RuntimeError("invalid Responses message")
            for block in item["content"]:
                if not isinstance(block, dict) or block.get("type") not in ("output_text", "refusal"):
                    raise RuntimeError("unsupported Responses content")
                if not isinstance(block.get("text" if block["type"] == "output_text" else "refusal"), str):
                    raise RuntimeError("invalid Responses content text")
        elif kind == "function_call":
            fields |= {"call_id", "name", "arguments"}
            if any(not isinstance(item.get(key), str) or not item[key] for key in ("call_id", "name", "arguments")):
                raise RuntimeError("invalid Responses function call")
        else:
            raise RuntimeError("unsupported Responses output item")
        clean = {key: item[key] for key in fields if key in item}
        if kind == "message":
            clean["content"] = [{key: block[key] for key in ("type", "text", "refusal", "annotations", "logprobs") if key in block}
                                for block in item["content"]]
        elif kind == "reasoning":
            clean["summary"] = [{"type": x["type"], "text": x["text"]} for x in item.get("summary", [])]
        result.append(clean)
    return result


def responses_input(config, messages, tools):
    identity = responses_identity(config, tools)
    result = []
    if config["system_prompt"]:
        block = {"type": "input_text", "text": config["system_prompt"]}
        if config.get("prompt_cache_enabled", True) and config.get("prompt_cache_mode") == "explicit":
            block["prompt_cache_breakpoint"] = {"mode": "explicit"}
        result.append({"role": "developer", "content": [block]})
    for msg in messages:
        role = msg["role"]
        state = msg.get("_responses")
        if role == "assistant" and isinstance(state, dict) and state.get("identity") == identity and state.get("prefix") == _digest(result):
            result.extend(responses_output(state.get("output")))
            continue
        if role == "tool":
            result.append({"type": "function_call_output", "call_id": msg["tool_call_id"], "output": msg["text"]})
            if msg.get("_images"):
                result.append({"role": "user", "content": [{"type": "input_text", "text": "Tool screenshot observation"},
                    *[{"type": "input_image", "image_url": "data:image/png;base64," + im} for im in msg["_images"]]]})
            continue
        content = []
        if msg.get("text"):
            content.append({"type": "output_text" if role == "assistant" else "input_text", "text": msg["text"]})
        for im in msg.get("_images", []):
            content.append({"type": "input_image", "image_url": "data:" + im["mime"] + ";base64," + im["data"]})
        if content:
            result.append({"role": role, "content": content})
        for call in msg.get("tool_calls", []):
            result.append({"type": "function_call", "call_id": call["id"], "name": call["name"],
                           "arguments": json.dumps(call["arguments"], ensure_ascii=False)})
    return result


async def stream_responses(http, config, messages, tools, *, session_id=None):
    tools = sorted(tools, key=lambda item: item["name"])
    inputs = responses_input(config, messages, tools)
    body = {"model": config["model"], "input": inputs, "stream": True, "store": False,
            "max_output_tokens": config["max_tokens"]}
    if config.get("reasoning_effort") is not None:
        body["reasoning"] = {"effort": config["reasoning_effort"]}
    if tools:
        body["tools"] = [dict(type="function", strict=False, **tool) for tool in tools]
    enabled = config.get("prompt_cache_enabled", True)
    mode = config.get("prompt_cache_mode")
    ttl = config.get("prompt_cache_ttl")
    if not enabled:
        body["prompt_cache_options"] = {"mode": "explicit"}
    elif mode is not None or ttl is not None:
        body["prompt_cache_options"] = {"mode": mode or "implicit"}
        if ttl is not None:
            body["prompt_cache_options"]["ttl"] = ttl
    if enabled and mode == "explicit" and not config["system_prompt"]:
        raise ValueError("Explicit caching requires a stable system prompt")
    if enabled and session_id is not None:
        body["prompt_cache_key"] = prompt_cache_key(config, session_id)
    yield {"type": "request", "request": {"protocol": "openai", "api_mode": "responses", "model": config["model"],
        "endpoint_id": hashlib.sha256(endpoint(config).encode()).hexdigest()[:16],
        "prompt_cache_enabled": enabled, "stream_usage_enabled": True,
        "cache_mode": body.get("prompt_cache_options", {}).get("mode"), "cache_ttl": ttl,
        "cache_breakpoints": int(enabled and mode == "explicit"), "cache_key": body.get("prompt_cache_key")}}
    text = ""
    terminal = None
    async with http.post(endpoint(config), json=body, headers={"Content-Type": "application/json", "Authorization": "Bearer " + config["api_key"]},
                         allow_redirects=False, timeout=aiohttp.ClientTimeout(total=600, sock_read=120)) as response:
        if 300 <= response.status < 400:
            raise RuntimeError("provider redirects are not permitted")
        if response.status >= 400:
            await response.content.read(4096)
            raise RuntimeError(f"provider HTTP {response.status}")
        if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
            raw = await bounded_response_body(response.content)
            try:
                terminal = json.loads(raw)
            except ValueError:
                raise RuntimeError("invalid Responses JSON") from None
        else:
            async for data in sse_events(response.content):
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    raise RuntimeError("invalid Responses SSE JSON") from None
                if not isinstance(obj, dict):
                    raise RuntimeError("invalid Responses event")
                kind = obj.get("type")
                if kind == "error" or obj.get("error"):
                    raise RuntimeError("provider stream reported an error")
                if kind == "response.output_text.delta":
                    delta = obj.get("delta")
                    if not isinstance(delta, str):
                        raise RuntimeError("invalid Responses text delta")
                    text += delta
                    if len(text) > 1024 * 1024:
                        raise RuntimeError("assistant output exceeds limit")
                    yield {"type": "text", "text": delta}
                elif kind in ("response.completed", "response.incomplete", "response.failed"):
                    terminal = obj.get("response")
                    break
        if not isinstance(terminal, dict):
            raise RuntimeError("provider stream interrupted before completion")
        usage = responses_usage(terminal.get("usage"))
        if usage:
            yield {"type": "usage", "usage": usage}
        if terminal.get("status") != "completed" or terminal.get("error"):
            raise RuntimeError("Responses request did not complete")
        output = responses_output(terminal.get("output"))
        final_text = "".join(block.get("text", block.get("refusal", "")) for item in output if item["type"] == "message" for block in item["content"])
        if not final_text.startswith(text):
            raise RuntimeError("Responses final text does not match stream")
        if len(final_text) > 1024 * 1024:
            raise RuntimeError("assistant output exceeds limit")
        if final_text[len(text):]:
            yield {"type": "text", "text": final_text[len(text):]}
        calls = []
        for item in output:
            if item["type"] != "function_call":
                continue
            try:
                args = json.loads(item["arguments"])
            except ValueError:
                raise RuntimeError("invalid Responses tool arguments") from None
            if not isinstance(args, dict) or len(item["arguments"]) > 1024 * 1024 or item["call_id"] in {call["id"] for call in calls}:
                raise RuntimeError("invalid Responses tool call")
            calls.append({"id": item["call_id"], "name": item["name"], "arguments": args})
        if len(calls) > 16:
            raise RuntimeError("too many Responses tool calls")
        yield {"type": "provider_state", "state": {"identity": responses_identity(config, tools), "prefix": _digest(inputs), "output": output}}
        if calls:
            yield {"type": "tools", "tools": calls}


def anthropic_messages(messages):
    result = []
    for msg in messages:
        role = "user" if msg["role"] == "tool" else msg["role"]
        content = []
        if msg["role"] == "tool":
            tool_content = [{"type": "text", "text": msg["text"] or "(empty result)"}]
            tool_content += [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": im}}
                             for im in msg.get("_images", [])]
            content.append({"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": tool_content})
        else:
            if msg["text"]:
                content.append({"type": "text", "text": msg["text"]})
            content += [{"type": "image", "source": {"type": "base64", "media_type": im["mime"], "data": im["data"]}}
                        for im in msg.get("_images", [])]
            content += [{"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["arguments"]}
                        for t in msg.get("tool_calls", [])]
        if not content:
            content = [{"type": "text", "text": "(empty)"}]
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(content)
        else:
            result.append({"role": role, "content": content})
    return result


async def stream(http, config, messages, tools, *, session_id=None):
    """Yield {type:text|tools|usage, ...}. Tool calls are emitted when complete."""
    if config.get("api_mode") == "responses":
        async for event in stream_responses(http, config, messages, tools, session_id=session_id):
            yield event
        return
    anthropic = config["protocol"] == "anthropic"
    body = {"model": config["model"], "max_tokens": config["max_tokens"], "stream": True}
    headers = {"Content-Type": "application/json"}
    tools = sorted(tools, key=lambda tool: tool["name"])
    cache_enabled = config.get("prompt_cache_enabled", True)
    request_info = {"protocol": config["protocol"], "model": config["model"],
                    "endpoint_id": hashlib.sha256(endpoint(config).encode()).hexdigest()[:16],
                    "prompt_cache_enabled": cache_enabled,
                    "stream_usage_enabled": config.get("stream_usage_enabled", True),
                    "cache_breakpoints": 0, "cache_key": None}
    if anthropic:
        headers.update({"x-api-key": config["api_key"], "anthropic-version": "2023-06-01"})
        body["messages"] = anthropic_messages(messages)
        if config["system_prompt"]:
            if cache_enabled:
                body["system"] = [{"type": "text", "text": config["system_prompt"], "cache_control": {"type": "ephemeral"}}]
                request_info["cache_breakpoints"] += 1
            else:
                body["system"] = config["system_prompt"]
        if tools:
            body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
        if cache_enabled:
            # The final block marks the whole tools/system/conversation prefix.
            # Do not insert a message between tool calls and their results.
            for item in reversed(body["messages"]):
                if item["content"]:
                    item["content"][-1]["cache_control"] = {"type": "ephemeral"}
                    request_info["cache_breakpoints"] += 1
                    break
    else:
        headers["Authorization"] = "Bearer " + config["api_key"]
        body["messages"] = openai_messages(messages, config["system_prompt"])
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
        if cache_enabled and session_id is not None:
            body["prompt_cache_key"] = prompt_cache_key(config, session_id)
            request_info["cache_key"] = body["prompt_cache_key"]
        if config.get("stream_usage_enabled", True):
            body["stream_options"] = {"include_usage": True}
        if config.get("reasoning_effort") is not None:
            body["reasoning_effort"] = config["reasoning_effort"]
    yield {"type": "request", "request": request_info}
    calls = {}
    async with http.post(endpoint(config), json=body, headers=headers,
                         allow_redirects=False,
                         timeout=aiohttp.ClientTimeout(total=600, sock_read=120)) as response:
        if 300 <= response.status < 400:
            raise RuntimeError("provider redirects are not permitted")
        if response.status >= 400:
            # Never relay provider error bodies: gateways sometimes echo credentials.
            await response.content.read(4096)
            raise RuntimeError(f"provider HTTP {response.status}")
        is_sse = "text/event-stream" in response.headers.get("Content-Type", "").lower()
        if not is_sse:
            raw = await bounded_response_body(response.content)
            try:
                obj = json.loads(raw)
                if anthropic:
                    for block in obj.get("content", []):
                        if block.get("type") == "text":
                            yield {"type": "text", "text": block.get("text", "")}
                        elif block.get("type") == "tool_use":
                            calls[len(calls)] = dict(id=block["id"], name=block["name"], arguments=block["input"])
                else:
                    msg = obj["choices"][0]["message"]
                    if msg.get("content"):
                        yield {"type": "text", "text": msg["content"]}
                    for tool in msg.get("tool_calls", []):
                        calls[len(calls)] = dict(id=tool["id"], name=tool["function"]["name"], arguments=json.loads(tool["function"]["arguments"]))
                usage = clean_usage(config["protocol"], obj.get("usage"))
                if usage:
                    yield {"type": "usage", "usage": usage}
            except (KeyError, ValueError, TypeError) as error:
                raise RuntimeError("invalid provider JSON response") from error
        else:
            finished = False
            async for data in sse_events(response.content):
                if data == "[DONE]":
                    finished = True
                    break
                try:
                    obj = json.loads(data)
                except ValueError as error:
                    raise RuntimeError("invalid provider SSE JSON") from error
                if obj.get("error") or obj.get("type") == "error":
                    raise RuntimeError("provider stream reported an error")
                if anthropic:
                    kind = obj.get("type")
                    index = obj.get("index", 0)
                    if kind == "message_start":
                        usage = clean_usage("anthropic", obj.get("message", {}).get("usage"))
                        if usage:
                            yield {"type": "usage", "usage": usage}
                    elif kind == "content_block_start":
                        block = obj.get("content_block", {})
                        if block.get("type") == "tool_use":
                            calls[index] = dict(id=block["id"], name=block["name"], arguments="", initial=block.get("input", {}))
                        elif block.get("type") == "text" and block.get("text"):
                            yield {"type": "text", "text": block["text"]}
                    elif kind == "content_block_delta":
                        delta = obj.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield {"type": "text", "text": delta.get("text", "")}
                        elif delta.get("type") == "input_json_delta":
                            if index not in calls:
                                raise RuntimeError("tool delta without a tool start")
                            calls[index]["arguments"] += delta.get("partial_json", "")
                    elif kind == "message_stop":
                        finished = True
                    usage = clean_usage("anthropic", obj.get("usage"))
                    if usage:
                        yield {"type": "usage", "usage": usage}
                else:
                    for choice in obj.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta", {})
                        if delta.get("content"):
                            yield {"type": "text", "text": delta["content"]}
                        for tool in delta.get("tool_calls", []):
                            entry = calls.setdefault(tool["index"], dict(id="", name="", arguments=""))
                            entry["id"] += tool.get("id", "")
                            entry["name"] += tool.get("function", {}).get("name", "")
                            entry["arguments"] += tool.get("function", {}).get("arguments", "")
                        if choice.get("finish_reason"):
                            finished = True
                    usage = clean_usage("openai", obj.get("usage"))
                    if usage:
                        yield {"type": "usage", "usage": usage}
                if sum(len(str(t)) for t in calls.values()) > 1024 * 1024:
                    raise RuntimeError("tool arguments exceed limit")
            if not finished:
                raise RuntimeError("provider stream interrupted before completion")
        if calls:
            output = []
            for call in calls.values():
                args = call["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else call.get("initial", {})
                    except ValueError as error:
                        raise RuntimeError("invalid tool arguments") from error
                if not isinstance(args, dict) or not call["id"] or not call["name"]:
                    raise RuntimeError("invalid tool call")
                output.append(dict(id=call["id"], name=call["name"], arguments=args))
            yield {"type": "tools", "tools": output}
