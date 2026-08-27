import requests
from backend.tools.schemas import Coordinates, GetCoordinatesInput

def get_coordinates(**kwargs):
    args = GetCoordinatesInput(**kwargs)

    url = "https://nominatim.openstreetmap.org/search"

    res = requests.get(
        url,
        params={"q": args.location, "format": "json", "limit": 1},
        headers={"User-Agent": "AI-Agent"},
        timeout=10,
    ).json()

    # ✅ Fix: use args.location
    if not res:
        return {
            "error": f"Location '{args.location}' not found"
        }

    return Coordinates(
        lat=float(res[0]["lat"]),
        lon=float(res[0]["lon"])
    ).model_dump()
