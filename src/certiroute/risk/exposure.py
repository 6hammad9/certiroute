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


def relative_exposure_reduction(
    baseline_units: float,
    candidate_units: float,
) -> float | None:
    """Return relative reduction, or ``None`` when the baseline has no exposure."""

    if baseline_units < 0 or candidate_units < 0:
        raise ValueError("exposure units cannot be negative")
    if baseline_units == 0:
        return None
    return 1 - candidate_units / baseline_units


def apply_certainty_penalty(
    raw_units: float,
    *,
    certainty: float,
    uncertainty_penalty: float = 0.5,
) -> float:
    """Scale raw exposure units by the transparent conservative penalty."""

    if raw_units < 0:
        raise ValueError("raw_units cannot be negative")
    if not 0 <= certainty <= 1:
        raise ValueError("certainty must be between zero and one")
    if uncertainty_penalty < 0:
        raise ValueError("uncertainty_penalty cannot be negative")
    return raw_units * (1 + uncertainty_penalty * (1 - certainty))


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

    degree_hours = max(temperature_c - reference_temperature_c, 0.0) * (
        duration_minutes / 60
    )
    adjusted = apply_certainty_penalty(
        degree_hours,
        certainty=certainty,
        uncertainty_penalty=uncertainty_penalty,
    )
    return ExposureEstimate(
        temperature_c=temperature_c,
        duration_minutes=duration_minutes,
        certainty=certainty,
        raw_exposure_units=round(degree_hours, 3),
        certainty_adjusted_units=round(adjusted, 3),
    )
