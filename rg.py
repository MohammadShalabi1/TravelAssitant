import requests
import re
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List, Optional
from dotenv import load_dotenv
import os
# =========================
# 🔑 API KEYS
# =========================
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# =========================
# 🧠 Pydantic Models
# =========================
class Coordinates(BaseModel):
    lat: float
    lon: float

class Weather(BaseModel):
    source: str
    location: str
    temp: float
    feels_like: float
    humidity: int
    description: str
    wind_speed: float
    clouds: int

class POI(BaseModel):
    name: str
    type: str

class NearbyPlaces(BaseModel):
    places: List[POI]

class GetCoordinatesInput(BaseModel):
    location: str

class GetWeatherInput(BaseModel):
    lat: float
    lon: float

class GetNearbyPlacesInput(BaseModel):
    lat: float
    lon: float
    tag_filter: Optional[str] = "amenity"
    radius: int = 2000

class FinalAnswer(BaseModel):
    summary: str
    suggested_places: Optional[List[str]] = None
    weather_summary: Optional[str] = None


# =========================
# 🧹 FIX JSON FROM LLM
# =========================
def extract_json(text: str) -> str:
    pattern = r"```(?:json)?(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


# =========================
# 🌍 TOOLS
# =========================
def get_coordinates(**kwargs):
    args = GetCoordinatesInput(**kwargs)
    url = "https://nominatim.openstreetmap.org/search"

    res = requests.get(
        url,
        params={"q": args.location, "format": "json", "limit": 1},
        headers={"User-Agent": "AI-Agent"},
        timeout=10,
    ).json()

    return Coordinates(lat=float(res[0]["lat"]), lon=float(res[0]["lon"])).model_dump()


def get_current_weather(**kwargs):
    args = GetWeatherInput(**kwargs)

    res = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={
            "lat": args.lat,
            "lon": args.lon,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
        },
        timeout=10,
    ).json()

    weather = Weather(
        source="openweather",
        location=res["name"],
        temp=res["main"]["temp"],
        feels_like=res["main"]["feels_like"],
        humidity=res["main"]["humidity"],
        description=res["weather"][0]["description"],
        wind_speed=res["wind"]["speed"],
        clouds=res["clouds"]["all"],
    )
    return weather.model_dump()


def get_nearby_places(**kwargs):
    args = GetNearbyPlacesInput(**kwargs)

    query = f"""
    [out:json];
    node(around:{args.radius},{args.lat},{args.lon})["{args.tag_filter}"];
    out;
    """

    try:
        res = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=15,
        ).json()
    except:
        return {"places": []}

    pois = []
    for e in res.get("elements", []):
        if "tags" not in e:
            continue
        pois.append(POI(
            name=e["tags"].get("name", "Unknown"),
            type=e["tags"].get(args.tag_filter, "Unknown")
        ))

    return NearbyPlaces(places=pois).model_dump()


TOOL_FUNCTIONS = {
    "get_coordinates": get_coordinates,
    "get_current_weather": get_current_weather,
    "get_nearby_places": get_nearby_places,
}


# =========================
# 🧠 GEMINI TOOLS SCHEMA
# =========================
GEMINI_TOOLS = [
    types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="get_coordinates",
            description="Get coordinates from location",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={"location": types.Schema(type=types.Type.STRING)},
                required=["location"],
            ),
        ),
        types.FunctionDeclaration(
            name="get_current_weather",
            description="Get weather from coordinates",
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
            description="Get nearby places",
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


# =========================
# 🤖 AGENT LOOP
# =========================
def run_agentic_loop(client: genai.Client):
    print("🤖 Agent ready (type quit)")

    system_prompt = """
You are a travel assistant.
ALWAYS use tools before answering.
"""

    chat = client.chats.create(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            tools=GEMINI_TOOLS,
            system_instruction=system_prompt,
        ),
    )

    while True:
        user = input("\nYou: ")
        if user == "quit":
            break

        response = chat.send_message(user)

        while response.function_calls:
            results = []
            for call in response.function_calls:
                result = TOOL_FUNCTIONS[call.name](**dict(call.args))
                results.append(types.Part.from_function_response(
                    name=call.name, response={"result": result}
                ))
            response = chat.send_message(results)

        # ✅ FINAL VALIDATION
        try:
            clean = extract_json(response.text)
            parsed = FinalAnswer.model_validate_json(clean)

            print("\n🤖", parsed.summary)
            if parsed.weather_summary:
                print("🌤", parsed.weather_summary)
            if parsed.suggested_places:
                print("📍", ", ".join(parsed.suggested_places))

        except Exception as e:
            print("❌ JSON Error:", response.text)


# =========================
# 🚀 RUN
# =========================
if __name__ == "__main__":
    client = genai.Client(api_key=GEMINI_API_KEY)
    run_agentic_loop(client)
