"""
Agent loop for one travel-assistant turn.

The loop rebuilds owned session context, optionally serves a scoped cache hit,
lets Gemini request allowlisted tools, persists the turn, and returns the final
assistant response.
"""

from __future__ import annotations

import time
from typing import Any

from google.genai import types

from backend.agent.memory import load_history, save_message
from backend.agent.router import choose_model
from backend.core.cache import (
    CacheAction,
    build_cache_identity,
    classify_cache_request,
    get_cache,
    get_tool_cache,
    hash_context,
    set_cache,
    set_tool_cache,
)
from backend.core.logger import get_logger
from backend.core.metrics import record_cache

log = get_logger(__name__)

try:
    from backend.tools.geocoding import get_coordinates
    from backend.tools.places import get_nearby_places
    from backend.tools.weather import get_current_weather
except ImportError:
    get_current_weather = get_coordinates = get_nearby_places = None  # type: ignore

TOOL_FUNCTIONS: dict[str, Any] = {
    "get_coordinates": get_coordinates,
    "get_current_weather": get_current_weather,
    "get_nearby_places": get_nearby_places,
}

GEMINI_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="get_coordinates",
                description=(
                    "Convert a city or location name into latitude and longitude. "
                    "Must be called before weather or places tools."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"location": types.Schema(type=types.Type.STRING)},
                    required=["location"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_current_weather",
                description="Get the current weather for a location using latitude and longitude.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "lat": types.Schema(type=types.Type.NUMBER),
                        "lon": types.Schema(type=types.Type.NUMBER),
                    },
                    required=["lat", "lon"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_nearby_places",
                description="Find nearby places such as restaurants, cafes, or attractions.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "lat": types.Schema(type=types.Type.NUMBER),
                        "lon": types.Schema(type=types.Type.NUMBER),
                        "tag_filter": types.Schema(type=types.Type.STRING),
                    },
                    required=["lat", "lon"],
                ),
            ),
        ]
    )
]

SYSTEM_PROMPT = """
You are a travel assistant with access to tools.

IMPORTANT RULES:
- You MUST use tools whenever the user asks about weather or nearby places.
- If a location is mentioned, first call get_coordinates.
- Then call the appropriate tool (weather or nearby places).
- Never ask the user for data that tools can provide.
- Always produce the final answer in natural language.
- Always call get_coordinates before any tool that needs lat/lon.
- get_coordinates returns lat and lon; pass them to weather or nearby_places tools.
- Never skip steps. If coordinates are missing, ask user to clarify location.

Never mention tools in the final answer.
Never return JSON.
"""

MAX_TOOL_LOOPS = 8
TOOL_RETRY_ATTEMPTS = 2
TOOL_RETRY_BACKOFF = 1.0


def _format_tool_result(result: dict | str) -> str:
    if not isinstance(result, dict):
        return str(result)

    parts = []
    for k, v in result.items():
        if isinstance(v, list):
            items = []
            for item in v:
                if isinstance(item, dict):
                    items.append(
                        item.get("name")
                        or item.get("title")
                        or item.get("value")
                        or item.get("address")
                        or str(item)
                    )
                else:
                    items.append(str(item))
            parts.append(f"{k.capitalize()}: {', '.join(items)}")
        else:
            parts.append(f"{k.capitalize()}: {v}")
    return "; ".join(parts)


def _call_tool_with_retry(name: str, args: dict) -> tuple[str, bool]:
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        log.error(f"Unknown tool requested: {name}")
        return f"Tool '{name}' is not available.", False

    cached_result = get_tool_cache(name, args)
    if cached_result:
        record_cache(hit=True)
        log.info(f"[TOOL CACHE HIT] tool={name}")
        return cached_result, True

    record_cache(hit=False)
    delay = TOOL_RETRY_BACKOFF
    for attempt in range(1, TOOL_RETRY_ATTEMPTS + 1):
        try:
            result = fn(**args)
            formatted = _format_tool_result(result)
            if "error" not in formatted.lower():
                set_tool_cache(name, args, formatted)
            return formatted, True
        except Exception as exc:
            log.warning(f"Tool '{name}' attempt {attempt} failed: {exc}")
            if attempt < TOOL_RETRY_ATTEMPTS:
                time.sleep(delay)
                delay *= 2

    log.error(f"Tool '{name}' failed after {TOOL_RETRY_ATTEMPTS} attempts")
    return f"The '{name}' tool is temporarily unavailable. Please try again later.", False


def run_single_turn(session_id: str, user_id: str, user_message: str, client) -> dict:
    turn_start = time.time()
    log.info(f"[TURN START] session={session_id} message_len={len(user_message)}")

    raw_history = load_history(session_id, user_id, limit=10)
    model_version = choose_model(user_message)
    cache_policy = classify_cache_request(user_message, has_context=bool(raw_history))
    cache_identity = build_cache_identity(
        user_message,
        cache_kind=cache_policy.kind,
        model_version=model_version,
        context_hash=hash_context(raw_history),
        user_id=user_id,
        session_id=session_id,
    )

    cached_answer = None
    if cache_policy.action == CacheAction.ALLOW:
        cached_answer = get_cache(user_message, cache_identity)
        record_cache(hit=bool(cached_answer))
    else:
        log.debug(f"[CACHE BYPASS] reason={cache_policy.reason} kind={cache_policy.kind.value}")

    if cached_answer:
        save_message(session_id, user_id, "assistant", cached_answer)
        log.info(
            f"[CACHE HIT] session={session_id} kind={cache_policy.kind.value} "
            f"scope={cache_identity.user_scope} duration={time.time()-turn_start:.2f}s"
        )
        return {"text": cached_answer, "tools_used": [], "cached": True}

    gemini_history = [
        types.Content(
            role="user" if role == "user" else "model",
            parts=[types.Part(text=content)],
        )
        for role, content in raw_history
        if role != "tool"
    ]

    chat = client.chats.create(
        model=model_version,
        config=types.GenerateContentConfig(
            tools=GEMINI_TOOLS,
            system_instruction=SYSTEM_PROMPT,
        ),
        history=gemini_history,
    )

    save_message(session_id, user_id, "user", user_message)
    response = chat.send_message(user_message)

    tools_used: list[str] = []
    tool_failures: list[str] = []
    loop_count = 0

    while response.function_calls:
        loop_count += 1
        if loop_count > MAX_TOOL_LOOPS:
            log.error(f"[MAX LOOPS] session={session_id} hit {MAX_TOOL_LOOPS} tool loops")
            break

        tool_texts: list[str] = []
        for call in response.function_calls:
            tools_used.append(call.name)
            log.info(f"[TOOL CALL] {call.name} args={dict(call.args)} session={session_id}")

            t0 = time.time()
            result_text, success = _call_tool_with_retry(call.name, dict(call.args))
            elapsed = time.time() - t0

            if success:
                log.info(f"[TOOL OK] {call.name} elapsed={elapsed:.2f}s")
            else:
                tool_failures.append(call.name)
                log.warning(f"[TOOL FAIL] {call.name} session={session_id} elapsed={elapsed:.2f}s")

            tool_texts.append(result_text)

        tool_summary = "\n".join(tool_texts)
        save_message(session_id, user_id, "tool", tool_summary)
        response = chat.send_message(
            f"Here are the tool results:\n{tool_summary}\n\nUse this information to answer the user."
        )

    final_text = response.text
    if tool_failures and not final_text.strip():
        final_text = (
            "I'm sorry, I wasn't able to retrieve the requested information right now "
            "due to a temporary service issue. Please try again in a moment."
        )
        log.warning(f"[FALLBACK RESPONSE] session={session_id} failed_tools={tool_failures}")

    if cache_policy.action == CacheAction.ALLOW and final_text.strip() and not tool_failures:
        set_cache(user_message, final_text, cache_identity, cache_policy.ttl_seconds)
    save_message(session_id, user_id, "assistant", final_text)

    duration = time.time() - turn_start
    log.info(
        f"[TURN END] session={session_id} tools={tools_used} "
        f"loops={loop_count} failures={tool_failures} duration={duration:.2f}s"
    )

    return {"text": final_text, "tools_used": tools_used, "cached": False}
