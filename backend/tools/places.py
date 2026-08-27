import requests
from backend.tools.schemas import GetNearbyPlacesInput, NearbyPlaces, POI, PlaceCategory


OVERPASS_CATEGORY_FILTERS = {
    PlaceCategory.RESTAURANT: ("amenity", "restaurant"),
    PlaceCategory.CAFE: ("amenity", "cafe"),
    PlaceCategory.MUSEUM: ("tourism", "museum"),
    PlaceCategory.HOTEL: ("tourism", "hotel"),
    PlaceCategory.ATTRACTION: ("tourism", "attraction"),
}


def get_nearby_places(**kwargs):
    args = GetNearbyPlacesInput(**kwargs)
    key, value = OVERPASS_CATEGORY_FILTERS[args.category]

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
            type=e["tags"].get(value, args.category.value)
        ))

    return NearbyPlaces(places=pois).model_dump()
