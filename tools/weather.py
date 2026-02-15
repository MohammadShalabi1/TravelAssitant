import requests, os
from dotenv import load_dotenv
from tools.schemas import Weather, GetWeatherInput

load_dotenv()
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

def get_current_weather(**kwargs):
    args = GetWeatherInput(**kwargs)

    try:
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
    except Exception as e:
        return {"error": f"Weather API request failed: {str(e)}"}

    # ✅ API returned error (very important)
    if "main" not in res:
        return {
            "error": f"Weather API error: {res.get('message', 'unknown error')}"
        }

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

