"""Deterministic conditions used only for the offline product demonstration."""

from certiroute.optimization import ConditionPoint, TemperatureProfile

# Offsets relative to each sample job's 14:00 anchor temperature. They create a
# plausible shape for exercising scheduling logic, not a meteorological dataset.
_DIURNAL_OFFSETS_C = (
    (8 * 60, -7.0),
    (9 * 60, -5.6),
    (10 * 60, -4.1),
    (11 * 60, -2.6),
    (12 * 60, -1.3),
    (13 * 60, -0.4),
    (14 * 60, 0.0),
    (15 * 60, 0.3),
    (16 * 60, 0.1),
    (17 * 60, -0.6),
)


def build_demo_profile(
    *,
    job_id: str,
    anchor_temperature_c: float,
    certainty: float,
    diurnal_amplitude: float = 1.0,
) -> TemperatureProfile:
    """Create a clearly labeled synthetic profile for sample-mode testing."""

    if diurnal_amplitude <= 0:
        raise ValueError("diurnal_amplitude must be greater than zero")

    return TemperatureProfile(
        job_id=job_id,
        points=tuple(
            ConditionPoint(
                minute_of_day=minute,
                temperature_c=anchor_temperature_c + offset * diurnal_amplitude,
                certainty=certainty,
            )
            for minute, offset in _DIURNAL_OFFSETS_C
        ),
    )
