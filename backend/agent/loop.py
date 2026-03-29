from google.genai import types
from tools.schemas import FinalAnswer
from tools.weather import get_current_weather
from tools.geocoding import get_coordinates
from tools.places import get_nearby_places
from agent.router import choose_model
from core.cache import get_cache, set_cache, get_ttl
from agent.memory import save_message, load_history

TOOL_FUNCTIONS = {
    "get_coordinates": get_coordinates,
    "get_current_weather": get_current_weather,
    "get_nearby_places": get_nearby_places,
}

GEMINI_TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="get_coordinates",
            description="Convert a city or location name into latitude and longitude coordinates. Must be called before weather or places tools.",
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
            description="Find nearby places such as restaurants, cafes, or attractions using latitude and longitude.",
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


def _format_tool_result(result: dict | str) -> str:
    if not isinstance(result, dict):
        return str(result)

    parts = []
    for k, v in result.items():
        if isinstance(v, list):
            items = []
            for item in v:
                if isinstance(item, dict):        # BUG FIX: was isinstance(item, list)
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


def run_single_turn(session_id: str, user_message: str, client) -> dict:
    """
    Run one full agentic turn for a given session and user message.

    Returns a dict:
        {
            "text":       str,        # final assistant response
            "tools_used": list[str],  # names of tools called this turn
            "cached":     bool        # whether the answer came from cache
        }
    """
    # ── 1. Cache check ──────────────────────────────────────────────────────
    cache_key = user_message.lower().strip()
    cached = get_cache(cache_key)
    if cached:                              # BUG FIX: was `if cache_key`
        save_message(session_id, "assistant", cached)
        return {"text": cached, "tools_used": [], "cached": True}

    # ── 2. Rebuild Gemini chat from last 10 DB messages ─────────────────────
    raw_history = load_history(session_id)
    recent = raw_history[-10:]

    gemini_history = []
    for role, content in recent:
        if role == "tool":
            continue
        gemini_role = "user" if role == "user" else "model"
        gemini_history.append(
            types.Content(
                role=gemini_role,
                parts=[types.Part(text=content)],
            )
        )

    # BUG FIX: chat created AFTER the loop, not inside it
    chat = client.chats.create(
        model=choose_model("start"),
        config=types.GenerateContentConfig(
            tools=GEMINI_TOOLS,
            system_instruction=SYSTEM_PROMPT,
        ),
        history=gemini_history,
    )

    # ── 3. Send the new user message ─────────────────────────────────────────
    save_message(session_id, "user", user_message)
    response = chat.send_message(user_message)

    # ── 4. Agentic tool loop ─────────────────────────────────────────────────
    tools_used = []

    while response.function_calls:
        tool_texts = []

        for call in response.function_calls:
            tools_used.append(call.name)
            result = TOOL_FUNCTIONS[call.name](**dict(call.args))
            tool_texts.append(_format_tool_result(result))

        tool_summary = "\n".join(tool_texts)
        save_message(session_id, "tool", tool_summary)

        response = chat.send_message(
            f"Here are the tool results:\n{tool_summary}\n\nUse this information to answer the user."
        )

    # ── 5. Cache + persist final answer ──────────────────────────────────────
    final_text = response.text
    ttl = get_ttl(user_message)
    set_cache(cache_key, final_text, ttl)
    save_message(session_id, "assistant", final_text)

    return {"text": final_text, "tools_used": tools_used, "cached": False}