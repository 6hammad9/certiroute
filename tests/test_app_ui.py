"""UI-level tests for the Streamlit dashboard using Streamlit's AppTest."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"

PLAN_NAMES = [
    "As dispatched",
    "Standard route (shortest driving)",
    "Heat-aware",
    "CertiRoute recommendation",
]


def run_app() -> AppTest:
    app = AppTest.from_file(APP_PATH)
    app.run(timeout=30)
    assert not app.exception
    return app


def run_synthetic_app() -> AppTest:
    """Select the deterministic fallback so schedule assertions stay offline."""

    app = run_app()
    app.radio[0].set_value("Synthetic fallback")
    app.run(timeout=30)
    assert not app.exception
    return app


@pytest.fixture(scope="module")
def rendered_app() -> AppTest:
    return run_synthetic_app()


def all_text(app: AppTest) -> str:
    """Every prose element a visitor can read on the rendered page."""

    parts = [element.value for element in app.markdown]
    parts += [element.value for element in app.caption]
    parts += [element.value for element in app.success]
    parts += [element.value for element in app.info]
    return " ".join(str(part) for part in parts)


def test_landing_page_explains_the_problem_before_any_jargon(
    rendered_app: AppTest,
) -> None:
    text = all_text(rendered_app)

    # A first-time visitor must be told what problem this solves.
    assert "The problem." in text
    assert "What this does." in text
    assert "How to read this page" in text


def test_headline_metrics_use_plain_language(rendered_app: AppTest) -> None:
    labels = {metric.label for metric in rendered_app.metric}

    assert {
        "Heat exposure avoided",
        "Extra driving",
        "Work above 35 °C",
        "Crew back at depot",
    } <= labels


def test_exposure_metric_is_a_percentage_or_an_explicit_dash(
    rendered_app: AppTest,
) -> None:
    metric = next(
        metric
        for metric in rendered_app.metric
        if metric.label == "Heat exposure avoided"
    )

    assert metric.value == "—" or metric.value.endswith("%")


def test_a_plain_english_recommendation_is_stated(rendered_app: AppTest) -> None:
    # The recommendation is the first thing a judge should be able to read.
    recommendations = [element.value for element in rendered_app.success]
    recommendations += [element.value for element in rendered_app.info]
    joined = " ".join(recommendations)

    assert "Move " in joined or "No reordering needed" in joined


def test_comparison_table_names_all_four_plans_in_plain_language(
    rendered_app: AppTest,
) -> None:
    summary = rendered_app.dataframe[0].value

    assert list(summary["Plan"]) == PLAN_NAMES
    assert "What it optimises" in summary.columns
    assert (summary["Driving (min)"] > 0).all()
    assert (summary["Heat load, caution applied"] >= summary["Heat load"]).all()


def test_unusual_conditions_scenario_is_active_by_default(
    rendered_app: AppTest,
) -> None:
    text = all_text(rendered_app)

    assert "Simulated shift" in text
    assert "PHX-101" in text


def test_ordinary_day_scenario_removes_the_simulated_shift_notice() -> None:
    app = run_synthetic_app()

    app.radio[1].set_value("Ordinary summer day")
    app.run(timeout=30)

    assert not app.exception
    assert "Simulated shift" not in all_text(app)


def test_every_plan_can_be_inspected_in_detail() -> None:
    app = run_synthetic_app()

    for name in PLAN_NAMES:
        app.selectbox[0].set_value(name)
        app.run(timeout=30)
        assert not app.exception
        schedule = app.dataframe[1].value
        assert len(schedule) == 5
        assert set(schedule.columns) >= {
            "Job",
            "Start",
            "Finish",
            "Average (°C)",
            "Peak (°C)",
            "Certainty",
            "Minutes ≥35 °C",
        }


def test_caution_slider_extremes_render_without_errors() -> None:
    app = run_synthetic_app()

    for value in (0.0, 2.0):
        app.slider[0].set_value(value)
        app.run(timeout=30)
        assert not app.exception


def test_page_discloses_which_parts_are_real_and_which_are_synthetic(
    rendered_app: AppTest,
) -> None:
    text = all_text(rendered_app)

    assert "Synthetic and explicitly labelled" in text
    assert "Authored inputs, not yet measured" in text


def test_page_states_the_safety_boundary(rendered_app: AppTest) -> None:
    text = all_text(rendered_app)

    assert "not a safety" in text
    assert "does not determine that work is safe" in text
    assert "not a regulatory limit" in text


def test_heat_load_unit_is_explained_somewhere_visible(
    rendered_app: AppTest,
) -> None:
    text = all_text(rendered_app)

    assert "degree-hours above 27" in text.lower()


def test_real_mode_is_default_and_never_silently_uses_synthetic_profiles() -> None:
    app = run_app()
    text = all_text(app)

    assert app.radio[0].value == "FortyGuard API (real)"
    assert "Exact request plan" in text
    assert (
        "no schedule is presented as real-data-driven" in text
        or "driven by real FortyGuard per-tile API output" in text
    )
    assert "Demonstration data: temperature profiles are synthetic" not in text


def test_real_mode_exposes_exact_credit_gate_when_samples_are_missing() -> None:
    app = run_app()

    # A developer cache may already satisfy the exact historical request. When
    # it does not, the only fetch button must remain disabled until authorized.
    if app.checkbox:
        assert "I authorize up to" in app.checkbox[0].label
        fetch = next(
            button for button in app.button if button.label.startswith("Fetch ")
        )
        assert fetch.disabled


def test_live_tab_points_to_the_integrated_real_schedule(rendered_app: AppTest) -> None:
    text = all_text(rendered_app)

    assert "Real temperature data now drives the schedule" in text
    assert "maps each job to returned temperature tiles" in text
