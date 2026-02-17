from tools.places import get_nearby_places

print("\nTEST 1 — correct format")
print(get_nearby_places(
    lat=48.8566,
    lon=2.3522,
    radius=1500,
    tag_filter="amenity=restaurant"
))

print("\nTEST 2 — agent wrong format (must still work)")
print(get_nearby_places(
    lat=48.8566,
    lon=2.3522,
    radius=1500,
    tag_filter="restaurant"
))

