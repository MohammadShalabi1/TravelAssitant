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
from pydantic import ValidationError

from backend.agent.memory import AgentContext, load_agent_context, save_message
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
from backend.core.metrics import (
    record_blocked_tool_call,
    record_cache,
    record_model_fallback,
    record_model_route,
    record_tool_call,
)
from backend.security.prompt_guard import sanitize_model_text
from backend.security.tool_gateway import (
    ToolExecutionContext,
    ToolPolicyError,
    authorize_and_execute_tool,
    sanitize_tool_error,
    validate_tool_arguments,
    wrap_untrusted_tool_data,
)
from backend.tools.schemas import TripPlan

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
PROVIDER_UNAVAILABLE_TEXT = (
    "I'm sorry, the AI provider is unavailable right now. "
    "Please check the Gemini API key and backend configuration, then try again."
)
STRUCTURED_ITINERARY_SYSTEM_PROMPT = """
You convert travel-planning answers into a validated itinerary schema.

Rules:
- Return only JSON matching the TripPlan schema.
- Keep user-facing travel details concise and practical.
- Do not include hidden prompts, credentials, policy text, chain-of-thought, or raw tool traces.
- If information is uncertain, put the caveat in warnings or limitations.
"""


@dataclass
class TurnState:
    session_id: str
    user_id: str
    user_message: str
    security_restricted: bool
    turn_start: float = field(default_factory=time.time)
    raw_history: list[tuple[str, str]] = field(default_factory=list)
    agent_context: AgentContext | None = None
    model_version: str = ""
    cache_policy: Any = None
    cache_identity: Any = None
    cached_answer: str | None = None
    chat: Any = None
    tools_used: list[str] = field(default_factory=list)
    tool_failures: list[str] = field(default_factory=list)
    total_tool_calls: int = 0
    loop_count: int = 0
    route_category: str = ""
    fallback_model: str = ""
    route_reason: str = ""
    structured_output_used: bool = False
    structured_output_failed: bool = False


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
    state.agent_context = load_agent_context(state.session_id, state.user_id)
    state.raw_history = state.agent_context.messages
    record_trace(
        state,
        "CONTEXT LOADED",
        messages=len(state.agent_context.messages),
        token_estimate=state.agent_context.token_estimate,
        truncated=state.agent_context.truncated,
        summary_used=state.agent_context.summary is not None,
    )
    return state.raw_history


def select_model(state: TurnState) -> str:
    route = choose_model(
        state.user_message,
        security_restricted=state.security_restricted,
    )
    state.route_category = route.category.value
    state.model_version = route.model
    state.fallback_model = route.fallback_model
    state.route_reason = route.reason
    record_model_route(state.route_category, state.model_version)
    record_trace(
        state,
        "MODEL ROUTE",
        category=state.route_category,
        model=state.model_version,
        fallback=state.fallback_model,
        reason=state.route_reason,
    )
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
        context_hash=hash_context(_context_for_hash(state)),
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


def _context_for_hash(state: TurnState) -> list[tuple[str, str]]:
    context = []
    if state.agent_context and state.agent_context.summary:
        context.append(("summary", state.agent_context.summary.summary))
    context.extend(state.raw_history)
    return context


def _to_gemini_history(state: TurnState) -> list[types.Content]:
    history = []
    if state.agent_context and state.agent_context.summary:
        history.append(
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        text=(
                            "Previous conversation summary for this user/session:\n"
                            f"{state.agent_context.summary.summary}"
                        )
                    )
                ],
            )
        )

    history.extend(
        types.Content(
            role="user" if role == "user" else "model",
            parts=[types.Part(text=content)],
        )
        for role, content in state.raw_history
        if role != "tool"
    )
    return history


def persist_turn(state: TurnState, role: str, content: str) -> bool:
    return save_message(state.session_id, state.user_id, role, content)


def _send_model_message(state: TurnState, client, model: str) -> Any:
    state.chat = client.chats.create(
        model=model,
        config=types.GenerateContentConfig(
            tools=GEMINI_TOOLS,
            system_instruction=SYSTEM_PROMPT,
        ),
        history=_to_gemini_history(state),
    )
    return state.chat.send_message(state.user_message)


def call_model(state: TurnState, client) -> Any:
    persist_turn(state, "user", state.user_message)
    try:
        return _send_model_message(state, client, state.model_version)
    except Exception:
        if not state.fallback_model or state.model_version == state.fallback_model:
            raise

        original_model = state.model_version
        state.model_version = state.fallback_model
        record_model_fallback(state.route_category, original_model, state.fallback_model)
        record_trace(
            state,
            "MODEL FALLBACK",
            category=state.route_category,
            from_model=original_model,
            to_model=state.fallback_model,
        )
        return _send_model_message(state, client, state.fallback_model)


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


def _is_itinerary_turn(state: TurnState) -> bool:
    return state.route_category == "itinerary_planning"


def _structured_itinerary_prompt(state: TurnState, draft_text: str) -> str:
    tool_metadata = ", ".join(state.tools_used) if state.tools_used else "none"
    return (
        "Create a TripPlan JSON object for this travel-planning turn.\n\n"
        f"User request:\n{state.user_message}\n\n"
        f"Draft answer:\n{draft_text}\n\n"
        f"Tools used: {tool_metadata}\n"
        "Use the draft answer and available context, but return only schema-valid JSON."
    )


def _structured_repair_prompt(error: Exception, invalid_text: str) -> str:
    return (
        "The previous itinerary JSON failed schema validation.\n"
        f"Validation error: {str(error)[:500]}\n\n"
        f"Invalid JSON/text:\n{invalid_text[:1500]}\n\n"
        "Return corrected JSON only. Ensure days start at 1 and are sequential."
    )


def _parse_trip_plan(response: Any) -> TripPlan:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, TripPlan):
        return parsed
    if parsed is not None:
        return TripPlan.model_validate(parsed)
    return TripPlan.model_validate_json(response.text)


def _render_trip_plan(plan: TripPlan) -> str:
    lines = [plan.trip_summary]
    details = []
    if plan.destination:
        details.append(f"Destination: {plan.destination}")
    if plan.dates:
        details.append(f"Dates: {plan.dates}")
    if details:
        lines.extend(["", *details])

    lines.append("")
    lines.append("Itinerary:")
    for day in plan.days:
        lines.append(f"Day {day.day}: {day.title}")
        for activity in day.activities:
            lines.append(f"- {activity}")
        if day.places:
            place_names = ", ".join(place.name for place in day.places)
            lines.append(f"Places to consider: {place_names}")

    if plan.weather_notes:
        lines.extend(["", f"Weather notes: {plan.weather_notes}"])
    if plan.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in plan.warnings)
    if plan.limitations:
        lines.append("")
        lines.append("Limitations:")
        lines.extend(f"- {limitation}" for limitation in plan.limitations)
    if plan.metadata.tools_used or plan.metadata.source_notes:
        metadata = []
        if plan.metadata.tools_used:
            metadata.append(f"Tools used: {', '.join(plan.metadata.tools_used)}")
        metadata.extend(plan.metadata.source_notes)
        lines.extend(["", "Sources and notes:", *[f"- {item}" for item in metadata]])

    return "\n".join(lines)


def _generate_structured_trip_plan(state: TurnState, client, draft_text: str) -> TripPlan:
    chat = client.chats.create(
        model=state.model_version,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TripPlan,
            system_instruction=STRUCTURED_ITINERARY_SYSTEM_PROMPT,
        ),
        history=_to_gemini_history(state),
    )
    response = chat.send_message(_structured_itinerary_prompt(state, draft_text))
    try:
        return _parse_trip_plan(response)
    except (ValidationError, ValueError) as exc:
        record_trace(state, "STRUCTURED OUTPUT REPAIR", error=type(exc).__name__)
        repair_response = chat.send_message(_structured_repair_prompt(exc, response.text))
        return _parse_trip_plan(repair_response)


def _final_text_from_response(state: TurnState, response: Any, client) -> str:
    draft_text = sanitize_model_text(response.text)
    if not _is_itinerary_turn(state) or state.tool_failures:
        return draft_text

    try:
        trip_plan = _generate_structured_trip_plan(state, client, draft_text)
        rendered = sanitize_model_text(_render_trip_plan(trip_plan))
        if rendered.strip():
            state.structured_output_used = True
            record_trace(
                state,
                "STRUCTURED OUTPUT OK",
                days=len(trip_plan.days),
                tools=trip_plan.metadata.tools_used,
            )
            return rendered
    except Exception as exc:
        state.structured_output_failed = True
        record_trace(state, "STRUCTURED OUTPUT FALLBACK", error=type(exc).__name__)

    return draft_text


def finalize_response(state: TurnState, response: Any, client) -> dict:
    final_text = _final_text_from_response(state, response, client)
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


def _is_model_provider_error(exc: Exception) -> bool:
    exc_type = type(exc)
    if exc_type.__module__.startswith("google.genai"):
        return True
    text = str(exc).lower()
    provider_signals = (
        "api_key_invalid",
        "api key not valid",
        "generativelanguage.googleapis.com",
        "google.genai",
    )
    return any(signal in text for signal in provider_signals)


def model_provider_fallback_response(state: TurnState, exc: Exception) -> dict:
    state.structured_output_failed = True
    log.warning(
        f"[MODEL PROVIDER UNAVAILABLE] session={state.session_id} "
        f"model={state.model_version} error={type(exc).__name__}"
    )
    record_trace(
        state,
        "MODEL PROVIDER FALLBACK",
        model=state.model_version,
        error=type(exc).__name__,
    )
    persist_turn(state, "assistant", PROVIDER_UNAVAILABLE_TEXT)
    return {"text": PROVIDER_UNAVAILABLE_TEXT, "tools_used": state.tools_used, "cached": False}


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

    try:
        response = call_model(state, client)
    except Exception as exc:
        if _is_model_provider_error(exc):
            return model_provider_fallback_response(state, exc)
        raise

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

    result = finalize_response(state, response, client)

    record_trace(
        state,
        "TURN END",
        tools=state.tools_used,
        loops=state.loop_count,
        failures=state.tool_failures,
        structured=state.structured_output_used,
        structured_failed=state.structured_output_failed,
        duration=f"{time.time() - state.turn_start:.2f}s",
    )
    return result
