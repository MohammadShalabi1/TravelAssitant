from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class PlaceCategory(str, Enum):
    RESTAURANT = "restaurant"
    CAFE = "cafe"
    MUSEUM = "museum"
    HOTEL = "hotel"
    ATTRACTION = "attraction"

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
    location: str = Field(..., min_length=1, max_length=120)

class GetWeatherInput(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)

class GetNearbyPlacesInput(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    category: PlaceCategory = PlaceCategory.RESTAURANT
    radius: int = Field(default=2000, ge=1, le=5000)

class FinalAnswer(BaseModel):
    summary: str
    suggested_places: List[str] | None = None
    weather_summary: str | None = None
