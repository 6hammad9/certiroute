"""Transparent planning scores for the first CertiRoute vertical slice."""

from pydantic import BaseModel, ConfigDict, Field


class ExposureEstimate(BaseModel):
    """A non-medical ambient-temperature exposure estimate."""

    model_config = ConfigDict(frozen=True)

    temperature_c: float
    duration_minutes: int = Field(gt=0)
    certainty: float = Field(ge=0, le=1)
    raw_exposure_units: float = Field(ge=0)
    certainty_adjusted_units: float = Field(ge=0)


def estimate_ambient_exposure(
    *,
    temperature_c: float,
    duration_minutes: int,
    certainty: float,
    reference_temperature_c: float = 27.0,
    uncertainty_penalty: float = 0.5,
) -> ExposureEstimate:
    """Calculate a simple, explainable score for planning comparisons.

    One raw unit is one degree-Celsius hour above the configured reference.
    Uncertainty adds a conservative penalty. This is deliberately not presented
    as a physiological or regulatory heat-stress index.
    """

    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be greater than zero")
    if not 0 <= certainty <= 1:
        raise ValueError("certainty must be between zero and one")
    if uncertainty_penalty < 0:
        raise ValueError("uncertainty_penalty cannot be negative")

    degree_hours = max(temperature_c - reference_temperature_c, 0.0) * (
        duration_minutes / 60
    )
    adjusted = degree_hours * (1 + uncertainty_penalty * (1 - certainty))
    return ExposureEstimate(
        temperature_c=temperature_c,
        duration_minutes=duration_minutes,
        certainty=certainty,
        raw_exposure_units=round(degree_hours, 3),
        certainty_adjusted_units=round(adjusted, 3),
    )
