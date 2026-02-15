from pydantic import BaseModel
from typing import List, Optional

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
