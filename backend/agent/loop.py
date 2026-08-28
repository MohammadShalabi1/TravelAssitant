"""
Agent loop for one travel-assistant turn.

The loop rebuilds owned session context, optionally serves a scoped cache hit,
lets Gemini request allowlisted tools, persists the turn, and returns the final
assistant response.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
from backend.core.metrics import record_blocked_tool_call, record_cache, record_tool_call
from backend.security.prompt_guard import sanitize_model_text
from backend.security.tool_gateway import (
    ToolExecutionContext,
    ToolPolicyError,
    authorize_and_execute_tool,
    sanitize_tool_error,
    validate_tool_arguments,
    wrap_untrusted_tool_data,
)

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
                        "category": types.Schema(
                            type=types.Type.STRING,
                            enum=["restaurant", "cafe", "museum", "hotel", "attraction"],
                        ),
                        "radius": types.Schema(type=types.Type.INTEGER),
                    },
                    required=["lat", "lon", "category"],
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


@dataclass
class TurnState:
    session_id: str
    user_id: str
    user_message: str
    security_restricted: bool
    turn_start: float = field(default_factory=time.time)
    raw_history: list[tuple[str, str]] = field(default_factory=list)
    model_version: str = ""
    cache_policy: Any = None
    cache_identity: Any = None
    cached_answer: str | None = None
    chat: Any = None
    tools_used: list[str] = field(default_factory=list)
    tool_failures: list[str] = field(default_factory=list)
    total_tool_calls: int = 0
    loop_count: int = 0


@dataclass(frozen=True)
class ToolCallResult:
    name: str
    result_text: str
    success: bool
    blocked: bool
    elapsed_s: float


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


def _call_tool_with_retry(
    name: str,
    args: dict,
    context: ToolExecutionContext,
) -> tuple[str, bool, bool]:
    try:
        validated_args = validate_tool_arguments(name, args)
    except ToolPolicyError as exc:
        record_blocked_tool_call(name, exc.reason)
        log.warning(f"[TOOL BLOCKED] tool={name} reason={exc.reason} session={context.session_id}")
        return sanitize_tool_error(exc), False, True

    cached_result = get_tool_cache(name, validated_args)
    if cached_result:
        record_cache(hit=True)
        log.info(f"[TOOL CACHE HIT] tool={name}")
        return wrap_untrusted_tool_data(name, cached_result), True, False

    record_cache(hit=False)
    delay = TOOL_RETRY_BACKOFF
    for attempt in range(1, TOOL_RETRY_ATTEMPTS + 1):
        try:
            result = authorize_and_execute_tool(name, validated_args, context, TOOL_FUNCTIONS)
            formatted = _format_tool_result(result)
            if "error" not in formatted.lower():
                set_tool_cache(name, validated_args, formatted)
            return wrap_untrusted_tool_data(name, formatted), True, False
        except ToolPolicyError as exc:
            record_blocked_tool_call(name, exc.reason)
            log.warning(f"[TOOL BLOCKED] tool={name} reason={exc.reason} session={context.session_id}")
            return sanitize_tool_error(exc), False, True
        except Exception as exc:
            log.warning(f"Tool '{name}' attempt {attempt} failed")
            if attempt < TOOL_RETRY_ATTEMPTS:
                time.sleep(delay)
                delay *= 2

    log.error(f"Tool '{name}' failed after {TOOL_RETRY_ATTEMPTS} attempts")
    return sanitize_tool_error(), False, False


def record_trace(state: TurnState, event: str, **fields: Any) -> None:
    extras = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {extras}" if extras else ""
    log.info(f"[{event}] session={state.session_id}{suffix}")


def load_context(state: TurnState) -> list[tuple[str, str]]:
    state.raw_history = load_history(state.session_id, state.user_id, limit=10)
    return state.raw_history


def select_model(state: TurnState) -> str:
    state.model_version = choose_model(state.user_message)
    return state.model_version


def apply_input_policy(state: TurnState) -> str | None:
    state.cache_policy = classify_cache_request(
        state.user_message,
        has_context=bool(state.raw_history),
    )
    state.cache_identity = build_cache_identity(
        state.user_message,
        cache_kind=state.cache_policy.kind,
        model_version=state.model_version,
        context_hash=hash_context(state.raw_history),
        user_id=state.user_id,
        session_id=state.session_id,
    )

    if state.cache_policy.action == CacheAction.ALLOW:
        state.cached_answer = get_cache(state.user_message, state.cache_identity)
        record_cache(hit=bool(state.cached_answer))
    else:
        log.debug(
            f"[CACHE BYPASS] reason={state.cache_policy.reason} "
            f"kind={state.cache_policy.kind.value}"
        )
    return state.cached_answer


def _to_gemini_history(raw_history: list[tuple[str, str]]) -> list[types.Content]:
    return [
        types.Content(
            role="user" if role == "user" else "model",
            parts=[types.Part(text=content)],
        )
        for role, content in raw_history
        if role != "tool"
    ]


def persist_turn(state: TurnState, role: str, content: str) -> bool:
    return save_message(state.session_id, state.user_id, role, content)


def call_model(state: TurnState, client) -> Any:
    state.chat = client.chats.create(
        model=state.model_version,
        config=types.GenerateContentConfig(
            tools=GEMINI_TOOLS,
            system_instruction=SYSTEM_PROMPT,
        ),
        history=_to_gemini_history(state.raw_history),
    )
    persist_turn(state, "user", state.user_message)
    return state.chat.send_message(state.user_message)


def validate_tool_call(call: Any) -> tuple[str, dict[str, Any]]:
    return call.name, dict(call.args)


def execute_tool(state: TurnState, name: str, args: dict[str, Any]) -> ToolCallResult:
    state.tools_used.append(name)
    state.total_tool_calls += 1
    log.info(f"[TOOL CALL] {name} session={state.session_id}")

    t0 = time.time()
    context = ToolExecutionContext(
        session_id=state.session_id,
        user_id=state.user_id,
        restricted=state.security_restricted,
        tool_calls_used=state.total_tool_calls - 1,
    )
    result_text, success, blocked = _call_tool_with_retry(name, args, context)
    return ToolCallResult(
        name=name,
        result_text=result_text,
        success=success,
        blocked=blocked,
        elapsed_s=time.time() - t0,
    )


def record_tool_result(state: TurnState, result: ToolCallResult) -> None:
    if result.success:
        record_tool_call(result.name, success=True)
        log.info(f"[TOOL OK] {result.name} elapsed={result.elapsed_s:.2f}s")
        return

    record_tool_call(result.name, success=False)
    state.tool_failures.append(result.name)
    status = "blocked" if result.blocked else "failed"
    log.warning(
        f"[TOOL {status.upper()}] {result.name} "
        f"session={state.session_id} elapsed={result.elapsed_s:.2f}s"
    )


def record_tool_turn(state: TurnState, tool_texts: list[str]) -> Any:
    tool_summary = "\n".join(tool_texts)
    persist_turn(state, "tool", tool_summary)
    return state.chat.send_message(
        f"Here are the tool results:\n{tool_summary}\n\nUse this information to answer the user."
    )


def finalize_response(state: TurnState, response: Any) -> dict:
    final_text = sanitize_model_text(response.text)
    if state.tool_failures and not final_text.strip():
        final_text = (
            "I'm sorry, I wasn't able to retrieve the requested information right now "
            "due to a temporary service issue. Please try again in a moment."
        )
        log.warning(
            f"[FALLBACK RESPONSE] session={state.session_id} "
            f"failed_tools={state.tool_failures}"
        )

    if (
        state.cache_policy.action == CacheAction.ALLOW
        and final_text.strip()
        and not state.tool_failures
    ):
        set_cache(
            state.user_message,
            final_text,
            state.cache_identity,
            state.cache_policy.ttl_seconds,
        )
    persist_turn(state, "assistant", final_text)
    return {"text": final_text, "tools_used": state.tools_used, "cached": False}


def run_single_turn(
    session_id: str,
    user_id: str,
    user_message: str,
    client,
    security_restricted: bool = False,
) -> dict:
    state = TurnState(session_id, user_id, user_message, security_restricted)
    record_trace(state, "TURN START", message_len=len(user_message))

    load_context(state)
    select_model(state)
    cached_answer = apply_input_policy(state)

    if cached_answer:
        persist_turn(state, "assistant", cached_answer)
        record_trace(
            state,
            "CACHE HIT",
            kind=state.cache_policy.kind.value,
            scope=state.cache_identity.user_scope,
            duration=f"{time.time()-state.turn_start:.2f}s",
        )
        return {"text": cached_answer, "tools_used": [], "cached": True}

    response = call_model(state, client)

    while response.function_calls:
        state.loop_count += 1
        if state.loop_count > MAX_TOOL_LOOPS:
            log.error(f"[MAX LOOPS] session={session_id} hit {MAX_TOOL_LOOPS} tool loops")
            break

        tool_texts: list[str] = []
        for call in response.function_calls:
            name, args = validate_tool_call(call)
            result = execute_tool(state, name, args)
            record_tool_result(state, result)
            tool_texts.append(result.result_text)

        response = record_tool_turn(state, tool_texts)

    result = finalize_response(state, response)

    record_trace(
        state,
        "TURN END",
        tools=state.tools_used,
        loops=state.loop_count,
        failures=state.tool_failures,
        duration=f"{time.time() - state.turn_start:.2f}s",
    )
    return result
