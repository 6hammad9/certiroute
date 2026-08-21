"""UI-level tests for the Streamlit dashboard using Streamlit's AppTest."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"

STRATEGY_NAMES = [
    "Original order",
    "Efficiency only",
    "Heat aware",
    "Certainty aware",
]


def run_app() -> AppTest:
    app = AppTest.from_file(APP_PATH)
    app.run(timeout=30)
    assert not app.exception
    return app


@pytest.fixture(scope="module")
def rendered_app() -> AppTest:
    return run_app()


def test_all_top_level_metrics_render(rendered_app: AppTest) -> None:
    labels = {metric.label for metric in rendered_app.metric}

    assert {
        "Adjusted exposure reduction",
        "Recommended travel",
        "High-screening-temperature work",
        "Return to depot",
        "Jobs",
        "Raw exposure units",
        "Certainty-adjusted units",
    } <= labels


def test_exposure_reduction_metric_is_a_percentage_or_explicit_na(
    rendered_app: AppTest,
) -> None:
    metric = next(
        metric
        for metric in rendered_app.metric
        if metric.label == "Adjusted exposure reduction"
    )

    assert metric.value == "N/A" or metric.value.endswith("%")


def test_summary_table_compares_all_four_strategies(rendered_app: AppTest) -> None:
    summary = rendered_app.dataframe[0].value

    assert list(summary["Strategy"]) == STRATEGY_NAMES
    assert (summary["Travel (min)"] > 0).all()
    assert (summary["Adjusted exposure"] >= summary["Raw exposure"]).all()


def test_stress_test_scenario_is_active_by_default(rendered_app: AppTest) -> None:
    assert any(
        warning.value.startswith("Stress test active")
        for warning in rendered_app.warning
    )
    # The lowered PHX-101 certainty must reach the inspected schedule table.
    schedule = rendered_app.dataframe[1].value
    phx_101 = schedule[schedule["Job"] == "PHX-101"]
    assert phx_101["Certainty"].iloc[0] == pytest.approx(0.15)


def test_normal_conditions_scenario_removes_the_stress_warning() -> None:
    app = run_app()

    app.radio[0].set_value("Normal conditions")
    app.run(timeout=30)

    assert not app.exception
    assert not any(
        warning.value.startswith("Stress test active") for warning in app.warning
    )
    schedule = app.dataframe[1].value
    phx_101 = schedule[schedule["Job"] == "PHX-101"]
    assert phx_101["Certainty"].iloc[0] == pytest.approx(0.94)


def test_every_strategy_can_be_inspected() -> None:
    app = run_app()

    for name in STRATEGY_NAMES:
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


def test_uncertainty_slider_extremes_render_without_errors() -> None:
    app = run_app()

    for value in (0.0, 2.0):
        app.slider[0].set_value(value)
        app.run(timeout=30)
        assert not app.exception


def test_method_tab_states_the_safety_boundary(rendered_app: AppTest) -> None:
    method_text = " ".join(block.value for block in rendered_app.markdown)

    assert "does not determine that work is safe" in method_text


def test_comparison_caption_discloses_the_screening_threshold(
    rendered_app: AppTest,
) -> None:
    captions = " ".join(caption.value for caption in rendered_app.caption)

    assert "configurable comparison threshold" in captions
    assert "not a universal" in captions


def test_live_tab_never_submits_during_render(rendered_app: AppTest) -> None:
    # The live FortyGuard submission is gated behind an explicit button press;
    # rendering alone must not create network activity or activity IDs.
    assert "last_activity_id" not in rendered_app.session_state
