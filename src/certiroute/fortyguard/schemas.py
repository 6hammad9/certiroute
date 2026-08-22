"""Validated request schemas for the FortyGuard Temperature API."""

from datetime import date, time
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _cross_product(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
) -> float:
    return (second[0] - first[0]) * (third[1] - first[1]) - (second[1] - first[1]) * (
        third[0] - first[0]
    )


def _point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> bool:
    tolerance = 1e-12
    return (
        abs(_cross_product(start, end, point)) <= tolerance
        and min(start[0], end[0]) - tolerance
        <= point[0]
        <= max(start[0], end[0]) + tolerance
        and min(start[1], end[1]) - tolerance
        <= point[1]
        <= max(start[1], end[1]) + tolerance
    )


def _segments_intersect(
    first_start: tuple[float, float],
    first_end: tuple[float, float],
    second_start: tuple[float, float],
    second_end: tuple[float, float],
) -> bool:
    tolerance = 1e-12
    products = (
        _cross_product(first_start, first_end, second_start),
        _cross_product(first_start, first_end, second_end),
        _cross_product(second_start, second_end, first_start),
        _cross_product(second_start, second_end, first_end),
    )
    if products[0] * products[1] < 0 and products[2] * products[3] < 0:
        return True
    return (
        (
            abs(products[0]) <= tolerance
            and _point_on_segment(second_start, first_start, first_end)
        )
        or (
            abs(products[1]) <= tolerance
            and _point_on_segment(second_end, first_start, first_end)
        )
        or (
            abs(products[2]) <= tolerance
            and _point_on_segment(first_start, second_start, second_end)
        )
        or (
            abs(products[3]) <= tolerance
            and _point_on_segment(first_end, second_start, second_end)
        )
    )


def _validate_simple_ring(ring: list[tuple[float, float]]) -> None:
    for longitude, latitude in ring:
        if (
            not isfinite(longitude)
            or not isfinite(latitude)
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            raise ValueError("polygon positions must be finite WGS84 coordinates")
    if len(set(ring[:-1])) < 3:
        raise ValueError("a polygon ring needs at least three distinct positions")
    if any(start == end for start, end in zip(ring, ring[1:], strict=False)):
        raise ValueError("a polygon ring cannot contain zero-length segments")

    segments = list(zip(ring, ring[1:], strict=False))
    for first_index, first in enumerate(segments):
        for second_index in range(first_index + 1, len(segments)):
            if second_index == first_index + 1:
                continue
            if first_index == 0 and second_index == len(segments) - 1:
                continue
            if _segments_intersect(*first, *segments[second_index]):
                raise ValueError("a polygon ring cannot self-intersect")


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
            _validate_simple_ring(ring)
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


class DailyAggregateDateTime(BaseModel):
    """FortyGuard filter type 3: a whole-day aggregate for one date.

    Verified against the live API on 2026-08-22. Unlike filter type 1 this
    ignores ``start_time`` entirely - 06:00, 14:00 and 20:00 for the same date
    all returned an identical mean - and it is the only mode that returns
    tiles for the current date. ``start_time`` is still sent because the
    endpoint requires the field.
    """

    model_config = ConfigDict(frozen=True)

    start_date: date
    start_time: time = time(12, 0)
    filter_type: Literal[3] = 3

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
    date_time: SingleHourDateTime | DailyAggregateDateTime
    granularity: Literal[60, 80, 100] = 100
    analytic_type: Literal["tcm"] = "tcm"
