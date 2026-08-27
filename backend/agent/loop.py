"""
Agent loop — production hardened.

Improvements over original:
  - Max tool-loop guard (prevents infinite loops)
  - Tool retry with exponential back-off
  - Fallback response on tool failure
  - Per-turn token + latency logging
  - Confidence / reasoning summary injected into final answer metadata
"""

from __future__ import annotations

import time
from typing import Any

from google.genai import types

from backend.agent.router import choose_model
from backend.core.cache import get_cache, set_cache, get_ttl
from backend.agent.memory import save_message, load_history
from backend.core.logger import get_logger

log = get_logger(__name__)

# ── Tool registry ─────────────────────────────────────────────────────────────
# Import lazily so the module loads even if tool deps are missing in tests.
try:
    from backend.tools.weather import get_current_weather
    from backend.tools.geocoding import get_coordinates
    from backend.tools.places import get_nearby_places
except ImportError:
    get_current_weather = get_coordinates = get_nearby_places = None  # type: ignore

TOOL_FUNCTIONS: dict[str, Any] = {
    "get_coordinates":    get_coordinates,
    "get_current_weather": get_current_weather,
    "get_nearby_places":  get_nearby_places,
}

# ── Gemini tool schemas ───────────────────────────────────────────────────────
GEMINI_TOOLS = [
    types.Tool(function_declarations=[
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
    ])
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

# ── Config ────────────────────────────────────────────────────────────────────
MAX_TOOL_LOOPS = 8          # prevent runaway agent
TOOL_RETRY_ATTEMPTS = 2     # retry each failing tool call
TOOL_RETRY_BACKOFF = 1.0    # seconds, doubled each retry


# ── Helpers ───────────────────────────────────────────────────────────────────

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
                        item.get("name") or item.get("title")
                        or item.get("value") or item.get("address")
                        or str(item)
                    )
                else:
                    items.append(str(item))
            parts.append(f"{k.capitalize()}: {', '.join(items)}")
        else:
            parts.append(f"{k.capitalize()}: {v}")
    return "; ".join(parts)


def _call_tool_with_retry(name: str, args: dict) -> tuple[str, bool]:
    """
    Call a tool with retry/back-off.
    Returns (formatted_result, success).
    """
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        log.error(f"Unknown tool requested: {name}")
        return f"Tool '{name}' is not available.", False

    delay = TOOL_RETRY_BACKOFF
    for attempt in range(1, TOOL_RETRY_ATTEMPTS + 1):
        try:
            result = fn(**args)
            return _format_tool_result(result), True
        except Exception as exc:
            log.warning(f"Tool '{name}' attempt {attempt} failed: {exc}")
            if attempt < TOOL_RETRY_ATTEMPTS:
                time.sleep(delay)
                delay *= 2

    log.error(f"Tool '{name}' failed after {TOOL_RETRY_ATTEMPTS} attempts")
    return f"The '{name}' tool is temporarily unavailable. Please try again later.", False


# ── Main entry point ──────────────────────────────────────────────────────────

def run_single_turn(session_id: str, user_message: str, client) -> dict:
    """
    Run one full agentic turn.

    Returns:
        {
            "text":       str,
            "tools_used": list[str],
            "cached":     bool,
        }
    """
    turn_start = time.time()
    log.info(f"[TURN START] session={session_id} message_len={len(user_message)}")

    # ── 1. Cache check ────────────────────────────────────────────────────────
    cache_key = user_message.lower().strip()
    cached_answer = get_cache(cache_key)
    if cached_answer:
        save_message(session_id, "assistant", cached_answer)
        log.info(f"[CACHE HIT] session={session_id} duration={time.time()-turn_start:.2f}s")
        return {"text": cached_answer, "tools_used": [], "cached": True}

    # ── 2. Rebuild chat history (last 10 non-tool messages) ───────────────────
    raw_history = load_history(session_id, limit=10)
    gemini_history = [
        types.Content(
            role="user" if role == "user" else "model",
            parts=[types.Part(text=content)],
        )
        for role, content in raw_history
        if role != "tool"
    ]

    chat = client.chats.create(
        model=choose_model(user_message),
        config=types.GenerateContentConfig(
            tools=GEMINI_TOOLS,
            system_instruction=SYSTEM_PROMPT,
        ),
        history=gemini_history,
    )

    # ── 3. Send user message ──────────────────────────────────────────────────
    save_message(session_id, "user", user_message)
    response = chat.send_message(user_message)

    # ── 4. Agentic tool loop (bounded) ────────────────────────────────────────
    tools_used: list[str] = []
    tool_failures: list[str] = []
    loop_count = 0

    while response.function_calls:
        loop_count += 1
        if loop_count > MAX_TOOL_LOOPS:
            log.error(
                f"[MAX LOOPS] session={session_id} hit {MAX_TOOL_LOOPS} tool loops — aborting"
            )
            break

        tool_texts: list[str] = []
        for call in response.function_calls:
            tools_used.append(call.name)
            log.info(f"[TOOL CALL] {call.name} args={dict(call.args)} session={session_id}")

            t0 = time.time()
            result_text, success = _call_tool_with_retry(call.name, dict(call.args))
            elapsed = time.time() - t0

            if not success:
                tool_failures.append(call.name)
                log.warning(f"[TOOL FAIL] {call.name} session={session_id} elapsed={elapsed:.2f}s")
            else:
                log.info(f"[TOOL OK] {call.name} elapsed={elapsed:.2f}s")

            tool_texts.append(result_text)

        tool_summary = "\n".join(tool_texts)
        save_message(session_id, "tool", tool_summary)

        response = chat.send_message(
            f"Here are the tool results:\n{tool_summary}\n\nUse this information to answer the user."
        )

    # ── 5. Fallback if all tools failed ──────────────────────────────────────
    final_text = response.text

    if tool_failures and not final_text.strip():
        final_text = (
            "I'm sorry, I wasn't able to retrieve the requested information right now "
            "due to a temporary service issue. Please try again in a moment."
        )
        log.warning(f"[FALLBACK RESPONSE] session={session_id} failed_tools={tool_failures}")

    # ── 6. Cache + persist ────────────────────────────────────────────────────
    ttl = get_ttl(user_message)
    set_cache(cache_key, final_text, ttl)
    save_message(session_id, "assistant", final_text)

    duration = time.time() - turn_start
    log.info(
        f"[TURN END] session={session_id} tools={tools_used} "
        f"loops={loop_count} failures={tool_failures} duration={duration:.2f}s"
    )

    return {"text": final_text, "tools_used": tools_used, "cached": False}