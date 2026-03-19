import re
from google.genai import types
from tools.schemas import FinalAnswer
from tools.weather import get_current_weather
from tools.geocoding import get_coordinates
from tools.places import get_nearby_places
from agent.router import choose_model
from core.cache import get_cache, set_cache,get_ttl
from agent.memory import init_db, create_session, save_message, load_history
from core.rate_limit import check_rate_limit, time_remaining

TOOL_FUNCTIONS = {
    "get_coordinates": get_coordinates,
    "get_current_weather": get_current_weather,
    "get_nearby_places": get_nearby_places,
}

def extract_json(text: str) -> str:
    pattern = r"```(?:json)?(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()

GEMINI_TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="get_coordinates",
            description="Convert a city or location name into latitude and longitude coordinates. Must be called befo" \
            "re weather or places tools.",
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


def run_agentic_loop(client):
    print("🤖 Agent ready (type quit)")

    # ----------------------------
    # Initialize DB + Session
    # ----------------------------
    init_db()
    session_id = create_session()

    system_prompt = """
You are a travel assistant with access to tools.

IMPORTANT RULES:
- You MUST use tools whenever the user asks about weather or nearby places.
- If a location is mentioned, first call get_coordinates.
- Then call the appropriate tool (weather or nearby places).
- Never ask the user for data that tools can provide.
- Always produce the final answer in natural language.
-Always call get_coordinates before any tool that needs lat/lon.
- get_coordinates returns lat and lon; pass them to weather or nearby_places tools.
- Never skip steps. If coordinates are missing, ask user to clarify location.

Never mention tools in the final answer.
Never return JSON.
"""

    chat = client.chats.create(
        model=choose_model("start"),
        config=types.GenerateContentConfig(
            tools=GEMINI_TOOLS,
            system_instruction=system_prompt,
        ),
    )
    
    while True:
        user_input = input("\nYou: ")
        if user_input.lower() == "quit":
            break
        if not check_rate_limit(session_id):
            wait = time_remaining(session_id)
            print(f"⏳ Please wait {wait} more second(s) before sending a new message.")
            continue

        # ----------------------------
        # SAVE USER MESSAGE
        # ----------------------------
        save_message(session_id, "user", user_input)

        cache_key = user_input.lower()
        cached_response = get_cache(cache_key)
        if cached_response:
            print("\n🤖 (cached) " + cached_response)

            # Save cached response as assistant message
            save_message(session_id, "assistant", cached_response)

            continue
        #here where we inject the user input with the past user input 
        history = load_history(session_id)
        history  = history[-10:]
        formatted_history = []
        for role,content in history:
            if role == "user":
                formatted_history.append(f"user:{content}")
            if role == "assistant":
                formatted_history.append(f"assistant:{content}")    

        context = "\n".join(formatted_history)        
        
        response = chat.send_message(f"{context}\nUser: {user_input}")

        # Process tool calls, convert them to plain text BEFORE sending back
        while response.function_calls:
            tool_texts = []

            for call in response.function_calls:
                # Run the tool
                result = TOOL_FUNCTIONS[call.name](**dict(call.args))

                # Convert dict/tool result into human-readable text
                if isinstance(result, dict):
                    plain_result = []

                    for k, v in result.items():

                        # CASE 1 → value is a list
                        if isinstance(v, list):
                            clean_list = []

                            for item in v:
                                if isinstance(item, dict):
                                    clean_list.append(
                                        item.get("name")
                                        or item.get("title")
                                        or item.get("value")
                                        or item.get("address")
                                        or str(item)
                                    )
                                else:
                                    clean_list.append(str(item))

                            plain_result.append(
                                f"{k.capitalize()}: {', '.join(clean_list)}"
                            )

                        # CASE 2 → value is normal value
                        else:
                            plain_result.append(f"{k.capitalize()}: {v}")

                    result_text = "; ".join(plain_result)

                else:
                    result_text = str(result)

                tool_texts.append(result_text)

            # ----------------------------
            # SAVE TOOL RESULT (OPTIONAL MEMORY)
            # ----------------------------
            save_message(session_id, "tool", "\n".join(tool_texts))

            # Send plain text summary of tool results back to AI
            tool_summary = "\n".join(tool_texts)

            response = chat.send_message(
                "Here are the tool results:\n"
                f"{tool_summary}\n\n"
                "Use this information to answer the user."
            )

        ttl = get_ttl(user_input)

        # Cache the final AI response
        set_cache(cache_key, response.text, ttl)

        # ----------------------------
        # SAVE AI RESPONSE
        # ----------------------------
        save_message(session_id, "assistant", response.text)

        # Print AI response directly
        print("\n🤖", response.text)