import pytest

from certiroute.risk import estimate_ambient_exposure


def test_exposure_uses_degree_hours_above_reference() -> None:
    estimate = estimate_ambient_exposure(
        temperature_c=33,
        duration_minutes=30,
        certainty=1,
        reference_temperature_c=27,
    )

    assert estimate.raw_exposure_units == 3
    assert estimate.certainty_adjusted_units == 3


def test_lower_certainty_adds_conservative_penalty() -> None:
    certain = estimate_ambient_exposure(
        temperature_c=35, duration_minutes=60, certainty=1
    )
    uncertain = estimate_ambient_exposure(
        temperature_c=35, duration_minutes=60, certainty=0.5
    )

    assert uncertain.raw_exposure_units == certain.raw_exposure_units
    assert uncertain.certainty_adjusted_units > certain.certainty_adjusted_units


def test_exposure_rejects_invalid_certainty() -> None:
    with pytest.raises(ValueError, match="between zero and one"):
        estimate_ambient_exposure(temperature_c=35, duration_minutes=60, certainty=1.1)
