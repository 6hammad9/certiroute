"""A trained, shippable diurnal model for one operating area.

The runtime cannot afford to learn a shape on demand: doing so would mean
fetching a fortnight of hourly heatmaps before the first plan could be shown.
The shape is also not a property of a particular day - it is how an area's
heat is distributed across the hours, which changes slowly with season and
climate, not overnight.

So the shape is trained offline from cached snapshots, evaluated on days it
never saw, and saved as a versioned artifact. At plan time the only network
call is the one whole-day aggregate that sets today's level.

Every artifact carries the dates it learned from, the dates it was scored on,
and the error it actually achieved, so a plan can always state what evidence
stands behind it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import asin, ceil, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any

from certiroute.forecasting import DailyLevelShape, InsufficientHistoryError
from certiroute.optimization import ConditionPoint, TemperatureProfile

ARTIFACT_SCHEMA_VERSION = 1

# Predicted points carry no hidden certainty penalty; uncertainty is stated
# explicitly through the calibrated radius instead.
NEUTRAL_CERTAINTY = 1.0

DEFAULT_ARTIFACT_ROOT = Path("data/climatology")


class ClimatologyUnavailableError(LookupError):
    """No trained model covers the requested area."""


class OutsideTrainedAreaError(ValueError):
    """Work sites lie too far from the ground the model was trained on."""


# How far from the trained sites the offsets are still treated as valid. The
# diurnal shape is a regional property - how a metro's heat moves through a
# day - not a per-tile one, which is why one model can serve points a user
# places anywhere in that metro. It is emphatically not a national constant,
# so this radius is deliberately metro-sized rather than generous.
DEFAULT_TRAINED_RADIUS_KM = 60.0


@dataclass(frozen=True)
class TrainedArea:
    """The ground a model actually learned from."""

    min_latitude: float
    max_latitude: float
    min_longitude: float
    max_longitude: float

    @property
    def centre(self) -> tuple[float, float]:
        return (
            (self.min_latitude + self.max_latitude) / 2,
            (self.min_longitude + self.max_longitude) / 2,
        )

    def distance_km(self, latitude: float, longitude: float) -> float:
        """Great-circle distance from this area's centre."""

        centre_lat, centre_lon = self.centre
        earth_radius_km = 6371.0088
        lat1, lat2 = radians(centre_lat), radians(latitude)
        delta_lat = lat2 - lat1
        delta_lon = radians(longitude - centre_lon)
        value = (
            sin(delta_lat / 2) ** 2
            + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
        )
        return 2 * earth_radius_km * asin(sqrt(value))

    @classmethod
    def covering(
        cls, points: Iterable[tuple[float, float]]
    ) -> TrainedArea:
        latitudes = []
        longitudes = []
        for latitude, longitude in points:
            latitudes.append(latitude)
            longitudes.append(longitude)
        if not latitudes:
            raise ValueError("at least one training location is required")
        return cls(
            min_latitude=min(latitudes),
            max_latitude=max(latitudes),
            min_longitude=min(longitudes),
            max_longitude=max(longitudes),
        )


@dataclass(frozen=True)
class ClimatologyEvaluation:
    """What the model scored on days it was not trained on."""

    holdout_dates: tuple[date, ...]
    mean_absolute_error_c: float
    worst_absolute_error_c: float
    reading_count: int
    # One conformity score per held-out day: that day's largest absolute
    # error. Days, not site-hours, are the exchangeable unit, because a day
    # runs hot or cool as a whole.
    day_scores_c: tuple[float, ...]
    # Error at sites the offsets were never learned from. The product applies
    # one area model to whatever points a user drops on the map, so this is
    # the number that says whether that is honest, and it is recorded even
    # when it is unflattering.
    unseen_site_mae_c: float | None = None

    @property
    def supported_miscoverage(self) -> float:
        """The tightest miscoverage this many days can honestly support.

        A split-conformal radius needs ceil((n+1)(1-alpha)) <= n. With few
        held-out days the usual 10% target is not reachable, and claiming it
        anyway would be the kind of number that looks rigorous and is not.
        """

        count = len(self.day_scores_c)
        if count < 1:
            raise InsufficientHistoryError("no held-out day produced a score")
        for candidate in (0.05, 0.1, 0.2, 0.25, 0.5):
            if ceil((count + 1) * (1 - candidate)) <= count:
                return candidate
        raise InsufficientHistoryError(
            f"{count} held-out day(s) cannot support any usable interval"
        )


@dataclass(frozen=True)
class DiurnalClimatology:
    """Learned hour offsets for an area, plus the evidence behind them."""

    area_id: str
    label: str
    granularity_m: int
    shape: DailyLevelShape
    training_dates: tuple[date, ...]
    evaluation: ClimatologyEvaluation
    trained_at_utc: datetime
    trained_area: TrainedArea | None = None

    def sites_outside_trained_area(
        self,
        locations: Mapping[str, tuple[float, float]],
        *,
        radius_km: float = DEFAULT_TRAINED_RADIUS_KM,
    ) -> dict[str, float]:
        """Sites too far from the trained ground, with how far off they are.

        The map lets a dispatcher pan anywhere, so nothing stops a Phoenix
        model being pointed at Tucson. The offsets would still produce a
        confident-looking curve, and it would be meaningless. This is the
        check that stops that silently happening.
        """

        if self.trained_area is None:
            return {}
        return {
            job_id: distance
            for job_id, (latitude, longitude) in locations.items()
            if (distance := self.trained_area.distance_km(latitude, longitude))
            > radius_km
        }

    @property
    def covered_minutes(self) -> tuple[int, ...]:
        return self.shape.covered_minutes

    def covers(self, minutes: Sequence[int]) -> bool:
        return set(minutes).issubset(self.shape.offsets_by_minute)

    def predict_profiles(
        self, level_by_job: Mapping[str, float]
    ) -> dict[str, TemperatureProfile]:
        """Predict each site's curve from that site's own whole-day level.

        Anchoring per site rather than on an area mean keeps the spatial
        detail FortyGuard actually provides: the offsets say how the day is
        shaped, the site's own aggregate says how hot that site runs.
        """

        if not level_by_job:
            raise ValueError("at least one site level is required")
        return {
            job_id: TemperatureProfile(
                job_id=job_id,
                points=tuple(
                    ConditionPoint(
                        minute_of_day=minute,
                        temperature_c=value,
                        certainty=NEUTRAL_CERTAINTY,
                    )
                    for minute, value in sorted(self.shape.predict(level).items())
                ),
            )
            for job_id, level in level_by_job.items()
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "area_id": self.area_id,
            "label": self.label,
            "granularity_m": self.granularity_m,
            "trained_at_utc": self.trained_at_utc.isoformat(),
            "training_dates": [d.isoformat() for d in self.training_dates],
            "offsets_by_minute": {
                str(k): v for k, v in sorted(self.shape.offsets_by_minute.items())
            },
            "sample_counts": {
                str(k): v for k, v in sorted(self.shape.sample_counts.items())
            },
            "training_day_count": self.shape.day_count,
            "trained_area": (
                None
                if self.trained_area is None
                else {
                    "min_latitude": self.trained_area.min_latitude,
                    "max_latitude": self.trained_area.max_latitude,
                    "min_longitude": self.trained_area.min_longitude,
                    "max_longitude": self.trained_area.max_longitude,
                }
            ),
            "evaluation": {
                "holdout_dates": [
                    d.isoformat() for d in self.evaluation.holdout_dates
                ],
                "mean_absolute_error_c": self.evaluation.mean_absolute_error_c,
                "worst_absolute_error_c": self.evaluation.worst_absolute_error_c,
                "reading_count": self.evaluation.reading_count,
                "day_scores_c": list(self.evaluation.day_scores_c),
                "unseen_site_mae_c": self.evaluation.unseen_site_mae_c,
            },
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> DiurnalClimatology:
        version = payload.get("schema_version")
        if version != ARTIFACT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported climatology artifact version {version!r}"
            )
        evaluation = payload["evaluation"]
        return cls(
            area_id=str(payload["area_id"]),
            label=str(payload["label"]),
            granularity_m=int(payload["granularity_m"]),
            shape=DailyLevelShape(
                offsets_by_minute={
                    int(k): float(v)
                    for k, v in payload["offsets_by_minute"].items()
                },
                sample_counts={
                    int(k): int(v) for k, v in payload["sample_counts"].items()
                },
                day_count=int(payload["training_day_count"]),
            ),
            training_dates=tuple(
                date.fromisoformat(d) for d in payload["training_dates"]
            ),
            evaluation=ClimatologyEvaluation(
                holdout_dates=tuple(
                    date.fromisoformat(d) for d in evaluation["holdout_dates"]
                ),
                mean_absolute_error_c=float(evaluation["mean_absolute_error_c"]),
                worst_absolute_error_c=float(evaluation["worst_absolute_error_c"]),
                reading_count=int(evaluation["reading_count"]),
                day_scores_c=tuple(float(v) for v in evaluation["day_scores_c"]),
                unseen_site_mae_c=(
                    None
                    if evaluation.get("unseen_site_mae_c") is None
                    else float(evaluation["unseen_site_mae_c"])
                ),
            ),
            trained_at_utc=datetime.fromisoformat(payload["trained_at_utc"]),
            trained_area=(
                None
                if payload.get("trained_area") is None
                else TrainedArea(
                    min_latitude=float(payload["trained_area"]["min_latitude"]),
                    max_latitude=float(payload["trained_area"]["max_latitude"]),
                    min_longitude=float(payload["trained_area"]["min_longitude"]),
                    max_longitude=float(payload["trained_area"]["max_longitude"]),
                )
            ),
        )


# One training day: its date, each site's whole-day aggregate, and each
# site's measured hourly profile. Levels are per site rather than area-wide
# because that is exactly how they are supplied at plan time. Learning against
# an area mean and serving against a site level would be a train/serve skew,
# and it would show up as error at precisely the hottest sites.
TrainingDay = tuple[date, Mapping[str, float], Mapping[str, TemperatureProfile]]


def _learn_offsets(history: Sequence[TrainingDay]) -> DailyLevelShape:
    """Average each hour's offset from its own site's whole-day level."""

    if not history:
        raise InsufficientHistoryError("at least one historical day is required")
    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    used_days = 0
    for _, levels, day in history:
        contributed = False
        for job_id, profile in day.items():
            level = levels.get(job_id)
            if level is None:
                continue
            for point in profile.points:
                minute = point.minute_of_day
                totals[minute] = totals.get(minute, 0.0) + (
                    point.temperature_c - level
                )
                counts[minute] = counts.get(minute, 0) + 1
                contributed = True
        if contributed:
            used_days += 1
    if not totals:
        raise InsufficientHistoryError(
            "no historical day paired a site level with a measured profile"
        )
    return DailyLevelShape(
        offsets_by_minute={
            minute: totals[minute] / counts[minute] for minute in totals
        },
        sample_counts=dict(counts),
        day_count=used_days,
    )


def _residuals(
    shape: DailyLevelShape, days: Sequence[TrainingDay]
) -> list[float]:
    """Signed prediction errors, each site anchored on its own level."""

    residuals: list[float] = []
    for _, levels, day in days:
        for job_id, profile in day.items():
            level = levels.get(job_id)
            if level is None:
                continue
            for point in profile.points:
                offset = shape.offsets_by_minute.get(point.minute_of_day)
                if offset is None:
                    continue
                residuals.append((level + offset) - point.temperature_c)
    return residuals


def unseen_site_error(
    history: Sequence[TrainingDay], *, holdout_days: int = 3
) -> float | None:
    """Mean absolute error at sites whose readings never trained the offsets.

    Each site is held out in turn: the offsets are relearned from the other
    sites only, then scored on the held-out one across the held-out days. If
    this is close to the day-holdout error, one area model can serve points a
    user drops anywhere in that area. If it is much worse, it cannot, and the
    product should say so rather than quietly generalising.
    """

    ordered = sorted(history, key=lambda item: item[0])
    if len(ordered) < holdout_days + 2:
        return None
    site_ids = sorted({job_id for _, _, day in ordered for job_id in day})
    if len(site_ids) < 2:
        return None

    train, holdout = ordered[:-holdout_days], ordered[-holdout_days:]
    absolute: list[float] = []
    for held_site in site_ids:
        without_site = [
            (day_date, levels, {k: v for k, v in day.items() if k != held_site})
            for day_date, levels, day in train
        ]
        if not any(day for _, _, day in without_site):
            continue
        shape = _learn_offsets(without_site)
        only_site = [
            (day_date, levels, {held_site: day[held_site]})
            for day_date, levels, day in holdout
            if held_site in day
        ]
        absolute.extend(abs(value) for value in _residuals(shape, only_site))
    if not absolute:
        return None
    return sum(absolute) / len(absolute)


def train_climatology(
    history: Sequence[TrainingDay],
    *,
    area_id: str,
    label: str,
    granularity_m: int,
    holdout_days: int = 3,
    trained_area: TrainedArea | None = None,
    trained_at_utc: datetime | None = None,
) -> DiurnalClimatology:
    """Learn offsets on the earlier days and score them on the later ones.

    The split is chronological rather than random. Randomly held-out days
    would let the model learn from the future of its own test set, which is
    exactly the leak that makes offline heat models look better than they
    behave in a dispatcher morning.
    """

    if holdout_days < 1:
        raise ValueError("at least one held-out day is required")
    ordered = sorted(history, key=lambda item: item[0])
    if len(ordered) < holdout_days + 2:
        raise InsufficientHistoryError(
            f"{len(ordered)} day(s) available; training needs at least "
            f"{holdout_days + 2}"
        )

    train = ordered[:-holdout_days]
    holdout = ordered[-holdout_days:]
    shape = _learn_offsets(train)

    day_scores: list[float] = []
    absolute: list[float] = []
    for held_out_day in holdout:
        residuals = _residuals(shape, [held_out_day])
        if not residuals:
            continue
        day_scores.append(max(abs(value) for value in residuals))
        absolute.extend(abs(value) for value in residuals)
    if not absolute:
        raise InsufficientHistoryError(
            "no held-out day shared an hour with the learned offsets"
        )

    return DiurnalClimatology(
        area_id=area_id,
        label=label,
        granularity_m=granularity_m,
        shape=shape,
        training_dates=tuple(day_date for day_date, _, _ in train),
        evaluation=ClimatologyEvaluation(
            holdout_dates=tuple(day_date for day_date, _, _ in holdout),
            mean_absolute_error_c=sum(absolute) / len(absolute),
            worst_absolute_error_c=max(absolute),
            reading_count=len(absolute),
            day_scores_c=tuple(day_scores),
            unseen_site_mae_c=unseen_site_error(ordered, holdout_days=holdout_days),
        ),
        trained_at_utc=trained_at_utc or datetime.now(UTC),
        trained_area=trained_area,
    )


def artifact_path(area_id: str, *, root: Path | None = None) -> Path:
    base = root if root is not None else DEFAULT_ARTIFACT_ROOT
    return base / f"{area_id}.json"


def save_climatology(model: DiurnalClimatology, *, root: Path | None = None) -> Path:
    path = artifact_path(model.area_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_climatology(area_id: str, *, root: Path | None = None) -> DiurnalClimatology:
    """Load one area's trained model, or say plainly that none exists."""

    path = artifact_path(area_id, root=root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClimatologyUnavailableError(
            f"no trained heat model for area {area_id!r}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"trained heat model for {area_id!r} is not readable JSON"
        ) from exc
    return DiurnalClimatology.from_json(payload)


def available_areas(*, root: Path | None = None) -> tuple[str, ...]:
    base = root if root is not None else DEFAULT_ARTIFACT_ROOT
    if not base.is_dir():
        return ()
    return tuple(sorted(path.stem for path in base.glob("*.json")))


__all__ = [
    "ARTIFACT_SCHEMA_VERSION",
    "DEFAULT_TRAINED_RADIUS_KM",
    "OutsideTrainedAreaError",
    "TrainedArea",
    "ClimatologyEvaluation",
    "ClimatologyUnavailableError",
    "DiurnalClimatology",
    "artifact_path",
    "available_areas",
    "unseen_site_error",
    "load_climatology",
    "save_climatology",
    "train_climatology",
]
