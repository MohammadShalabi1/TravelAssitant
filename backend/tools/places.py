import requests
from tools.schemas import NearbyPlaces, POI, GetNearbyPlacesInput

def get_nearby_places(**kwargs):
    args = GetNearbyPlacesInput(**kwargs)
    
    if "=" in args.tag_filter:
      key, value = args.tag_filter.split("=")
    else:
     key = "amenity"
     value = args.tag_filter

    query = f"""
     [out:json];
     node(around:{args.radius},{args.lat},{args.lon})["{key}"="{value}"];
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
