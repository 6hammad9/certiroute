"""Tests for the trained, shippable area heat model."""

from datetime import UTC, date, datetime

import pytest

from certiroute.climatology import (
    ARTIFACT_SCHEMA_VERSION,
    ClimatologyUnavailableError,
    DiurnalClimatology,
    available_areas,
    load_climatology,
    save_climatology,
    train_climatology,
    unseen_site_error,
)
from certiroute.forecasting import InsufficientHistoryError
from certiroute.optimization import ConditionPoint, TemperatureProfile

SITES = ("A", "B", "C")
HOURS = range(6, 12)


def profiles(readings: dict[int, float], sites=SITES, site_bias=0.0):
    return {
        site: TemperatureProfile(
            job_id=site,
            points=tuple(
                ConditionPoint(
                    minute_of_day=minute,
                    temperature_c=value + index * site_bias,
                    certainty=1.0,
                )
                for minute, value in sorted(readings.items())
            ),
        )
        for index, site in enumerate(sites)
    }


def curve(level: float, site_bias: float = 0.0):
    """A day whose hours sit at fixed offsets from each site own daily level.

    ``site_bias`` gives every site a persistent offset from the area, applied
    to its hourly readings *and* to its whole-day level, which is how a hot
    site genuinely behaves.
    """

    readings = {hour * 60: level + (hour - 9) for hour in HOURS}
    levels = {site: level + index * site_bias for index, site in enumerate(SITES)}
    return levels, profiles(readings, site_bias=site_bias)


def history(levels=(30.0, 31.0, 32.0, 33.0, 34.0), site_bias=0.0):
    return [
        (date(2026, 8, 10 + index), *curve(level, site_bias))
        for index, level in enumerate(levels)
    ]


def test_training_splits_chronologically_and_scores_the_later_days() -> None:
    model = train_climatology(
        history(),
        area_id="phoenix",
        label="Phoenix",
        granularity_m=60,
        holdout_days=2,
    )

    assert model.training_dates == (
        date(2026, 8, 10),
        date(2026, 8, 11),
        date(2026, 8, 12),
    )
    assert model.evaluation.holdout_dates == (date(2026, 8, 13), date(2026, 8, 14))
    # Offsets are exact for this construction, so held-out error is zero.
    assert model.evaluation.mean_absolute_error_c == pytest.approx(0.0, abs=1e-9)
    assert model.shape.offset_at(9 * 60) == pytest.approx(0.0)
    assert model.shape.offset_at(11 * 60) == pytest.approx(2.0)


def test_history_shorter_than_the_split_is_refused() -> None:
    with pytest.raises(InsufficientHistoryError, match="needs at least"):
        train_climatology(
            history(levels=(30.0, 31.0)),
            area_id="phoenix",
            label="Phoenix",
            granularity_m=60,
            holdout_days=2,
        )


def test_zero_holdout_days_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one held-out day"):
        train_climatology(
            history(),
            area_id="phoenix",
            label="Phoenix",
            granularity_m=60,
            holdout_days=0,
        )


def test_supported_miscoverage_reflects_the_number_of_held_out_days() -> None:
    """Few days cannot support a tight interval, and must not claim one."""

    three = train_climatology(
        history(),
        area_id="p",
        label="P",
        granularity_m=60,
        holdout_days=3,
    )
    # ceil((3+1) * 0.75) = 3 <= 3, so 25% miscoverage is the tightest honest one.
    assert three.evaluation.supported_miscoverage == pytest.approx(0.25)

    many = train_climatology(
        history(levels=tuple(30.0 + i for i in range(14))),
        area_id="p",
        label="P",
        granularity_m=60,
        holdout_days=9,
    )
    assert many.evaluation.supported_miscoverage == pytest.approx(0.1)


def test_each_site_is_anchored_on_its_own_level() -> None:
    model = train_climatology(
        history(),
        area_id="p",
        label="P",
        granularity_m=60,
        holdout_days=2,
    )

    predicted = model.predict_profiles({"A": 40.0, "B": 30.0})

    hot, _ = predicted["A"].condition_at(11 * 60)
    cool, _ = predicted["B"].condition_at(11 * 60)
    # The shared shape says +2 C at 11:00; the levels differ by 10 C.
    assert hot == pytest.approx(42.0)
    assert cool == pytest.approx(32.0)


def test_prediction_without_any_site_level_is_refused() -> None:
    model = train_climatology(
        history(), area_id="p", label="P", granularity_m=60, holdout_days=2
    )

    with pytest.raises(ValueError, match="at least one site level"):
        model.predict_profiles({})


def test_unseen_site_error_measures_sites_left_out_of_training() -> None:
    """The product applies one area model to unseen points, so this matters."""

    identical = unseen_site_error(history(), holdout_days=2)
    assert identical == pytest.approx(0.0, abs=1e-9)

    # Give each site its own persistent bias. Because every site is anchored
    # on its own daily level, a constant site bias cancels out and a held-out
    # site is still predicted exactly.
    biased = unseen_site_error(history(site_bias=1.5), holdout_days=2)
    assert biased == pytest.approx(0.0, abs=1e-9)


def test_unseen_site_error_is_none_with_a_single_site() -> None:
    one_site = [
        (day, {"A": levels["A"]}, {"A": prof["A"]})
        for day, levels, prof in history()
    ]
    assert unseen_site_error(one_site, holdout_days=2) is None


def test_artifact_survives_a_save_and_load_round_trip(tmp_path) -> None:
    model = train_climatology(
        history(),
        area_id="phoenix",
        label="Phoenix",
        granularity_m=60,
        holdout_days=2,
    )

    save_climatology(model, root=tmp_path)
    loaded = load_climatology("phoenix", root=tmp_path)

    assert loaded.area_id == model.area_id
    assert loaded.label == model.label
    assert loaded.granularity_m == model.granularity_m
    assert loaded.shape.offsets_by_minute == model.shape.offsets_by_minute
    assert loaded.training_dates == model.training_dates
    assert loaded.evaluation.holdout_dates == model.evaluation.holdout_dates
    assert loaded.evaluation.day_scores_c == model.evaluation.day_scores_c
    assert loaded.evaluation.unseen_site_mae_c == pytest.approx(
        model.evaluation.unseen_site_mae_c
    )
    assert available_areas(root=tmp_path) == ("phoenix",)


def test_missing_area_is_reported_rather_than_guessed(tmp_path) -> None:
    with pytest.raises(ClimatologyUnavailableError, match="no trained heat model"):
        load_climatology("nowhere", root=tmp_path)
    assert available_areas(root=tmp_path) == ()


def test_unreadable_artifact_is_refused(tmp_path) -> None:
    (tmp_path / "phoenix.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError, match="not readable JSON"):
        load_climatology("phoenix", root=tmp_path)


def test_future_schema_version_is_refused() -> None:
    payload = {
        "schema_version": ARTIFACT_SCHEMA_VERSION + 1,
        "area_id": "phoenix",
        "label": "Phoenix",
        "granularity_m": 60,
        "trained_at_utc": datetime.now(UTC).isoformat(),
        "training_dates": [],
        "offsets_by_minute": {},
        "sample_counts": {},
        "training_day_count": 0,
        "evaluation": {},
    }

    with pytest.raises(ValueError, match="unsupported climatology artifact"):
        DiurnalClimatology.from_json(payload)


def test_covers_reports_the_hours_the_model_actually_learned() -> None:
    model = train_climatology(
        history(), area_id="p", label="P", granularity_m=60, holdout_days=2
    )

    assert model.covers([6 * 60, 11 * 60])
    assert not model.covers([5 * 60])
