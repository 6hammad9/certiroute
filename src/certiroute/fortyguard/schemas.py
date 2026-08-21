"""Validated request schemas for the FortyGuard Temperature API."""

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


class PolygonGeometry(BaseModel):
    """A GeoJSON Polygon geometry."""

    model_config = ConfigDict(frozen=True)

    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]]

    @field_validator("coordinates")
    @classmethod
    def validate_linear_rings(
        cls, coordinates: list[list[tuple[float, float]]]
    ) -> list[list[tuple[float, float]]]:
        if not coordinates:
            raise ValueError("a polygon needs at least one linear ring")
        for ring in coordinates:
            if len(ring) < 4:
                raise ValueError("a polygon ring needs at least four positions")
            if ring[0] != ring[-1]:
                raise ValueError("a polygon ring must be closed")
        return coordinates


class PolygonFeature(BaseModel):
    """A GeoJSON Feature containing a polygon."""

    model_config = ConfigDict(frozen=True)

    type: Literal["Feature"] = "Feature"
    properties: dict[str, object] = Field(default_factory=dict)
    geometry: PolygonGeometry


class PolygonFeatureCollection(BaseModel):
    """The FeatureCollection wrapper required by FortyGuard."""

    model_config = ConfigDict(frozen=True)

    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[PolygonFeature] = Field(min_length=1)


class SingleHourDateTime(BaseModel):
    """FortyGuard filter type 1: one hour beginning at the supplied time."""

    model_config = ConfigDict(frozen=True)

    start_date: date
    start_time: time
    filter_type: Literal[1] = 1

    @field_serializer("start_date")
    def serialize_date(self, value: date) -> str:
        return value.isoformat()

    @field_serializer("start_time")
    def serialize_time(self, value: time) -> str:
        return value.strftime("%H:%M")


class HeatmapRequest(BaseModel):
    """Initial supported subset of the FortyGuard Create Heatmap contract."""

    model_config = ConfigDict(frozen=True)

    polygon_aoi: PolygonFeatureCollection
    date_time: SingleHourDateTime
    granularity: Literal[60, 80, 100] = 100
    analytic_type: Literal["tcm"] = "tcm"
