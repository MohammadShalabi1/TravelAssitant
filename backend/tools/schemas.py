from enum import Enum
from typing import List

from pydantic import BaseModel, Field, field_validator, model_validator


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


class TripPlace(BaseModel):
    name: str = Field(..., min_length=1)
    category: str | None = None
    notes: str | None = None

    @field_validator("name", "category", "notes")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("text fields cannot be blank")
        return stripped


class ItineraryDay(BaseModel):
    day: int = Field(..., ge=1, le=14)
    title: str = Field(..., min_length=1)
    activities: List[str] = Field(..., min_length=1)
    places: List[TripPlace] | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title cannot be blank")
        return stripped

    @field_validator("activities")
    @classmethod
    def validate_activities(cls, values: List[str]) -> List[str]:
        cleaned = [value.strip() for value in values if value and value.strip()]
        if not cleaned:
            raise ValueError("at least one activity is required")
        return cleaned


class TripPlanMetadata(BaseModel):
    tools_used: List[str] = Field(default_factory=list)
    source_notes: List[str] = Field(default_factory=list)

    @field_validator("tools_used", "source_notes")
    @classmethod
    def strip_lists(cls, values: List[str]) -> List[str]:
        return [value.strip() for value in values if value and value.strip()]


class TripPlan(BaseModel):
    trip_summary: str = Field(..., min_length=1)
    destination: str | None = None
    dates: str | None = None
    days: List[ItineraryDay] = Field(..., min_length=1, max_length=14)
    weather_notes: str | None = None
    warnings: List[str] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    metadata: TripPlanMetadata = Field(default_factory=TripPlanMetadata)

    @field_validator("trip_summary", "destination", "dates", "weather_notes")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("text fields cannot be blank")
        return stripped

    @field_validator("warnings", "limitations")
    @classmethod
    def strip_optional_lists(cls, values: List[str]) -> List[str]:
        return [value.strip() for value in values if value and value.strip()]

    @model_validator(mode="after")
    def validate_day_order(self) -> "TripPlan":
        expected = list(range(1, len(self.days) + 1))
        actual = [day.day for day in self.days]
        if actual != expected:
            raise ValueError("itinerary days must start at 1 and be sequential")
        return self
