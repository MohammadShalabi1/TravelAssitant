from tools.places import get_nearby_places

result = get_nearby_places(
    lat=48.8566,
    lon=2.3522,
    radius=1000,
    tag_filter="restaurant"
)
print(result)
