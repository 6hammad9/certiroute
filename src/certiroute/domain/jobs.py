"""Core job and coordinate models."""

from datetime import time

from pydantic import BaseModel, ConfigDict, Field


class GeoPoint(BaseModel):
    """A WGS84 coordinate used by jobs and API request builders."""

    model_config = ConfigDict(frozen=True)

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)

    @property
    def geojson_position(self) -> tuple[float, float]:
        """Return GeoJSON's required longitude, latitude ordering."""

        return (self.longitude, self.latitude)


class Job(BaseModel):
    """One field job that must be assigned a time during a crew shift."""

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    location: GeoPoint
    duration_minutes: int = Field(gt=0, le=24 * 60)
    priority: int = Field(default=3, ge=1, le=5)
    earliest_start: time | None = None
    latest_finish: time | None = None
