"""Tests for the visual system, and for the one way it can silently break."""

import pytest

from certiroute.theme import (
    RESULT_MODE_STYLES,
    STYLESHEET,
    as_markup,
    corners,
    icon,
)

INJECTED = (STYLESHEET, RESULT_MODE_STYLES)


@pytest.mark.parametrize("markup", INJECTED)
def test_injected_markup_contains_no_blank_line(markup: str) -> None:
    """A blank line ends the raw-HTML block and dumps the CSS onto the page.

    CommonMark terminates a raw-HTML block at the first blank line, so a
    stylesheet written with spacing between its sections stops being a
    stylesheet at the first section break and every rule after it renders as
    visible paragraph text above the interface. Python cannot notice; only a
    browser can. Hence this test.
    """

    blank = [
        index
        for index, line in enumerate(markup.splitlines(), start=1)
        if not line.strip()
    ]
    assert blank == [], f"blank lines at {blank} will leak CSS onto the page"


def test_as_markup_strips_the_blank_lines_it_is_given() -> None:
    compacted = as_markup("<style>\n", "a {}\n\n\nb {}\n", "\n</style>")

    assert "\n\n" not in compacted
    assert compacted.splitlines() == ["<style>", "a {}", "b {}", "</style>"]


def test_stylesheet_opens_with_markup_and_closes_the_style_element() -> None:
    lines = STYLESHEET.splitlines()

    assert lines[0].startswith("<link ")
    assert "<style>" in STYLESHEET
    assert lines[-1] == "</style>"
    assert STYLESHEET.count("<style>") == STYLESHEET.count("</style>")


def test_the_industrial_palette_is_present_and_complete() -> None:
    """One accent and two full ramps; nothing else may creep in as colour."""

    assert "--color-accent: #5980a6" in STYLESHEET
    assert "--color-bg: #f2f2f3" in STYLESHEET
    for step in (100, 300, 500, 700, 900):
        assert f"--n{step}:" in STYLESHEET
        assert f"--a{step}:" in STYLESHEET


def test_every_declared_font_names_a_system_fallback() -> None:
    """The app must never wait on a network font to render text."""

    for stack, fallback in (
        ("--font-heading", "sans-serif"),
        ("--font-body", "sans-serif"),
        ("--font-mono", "monospace"),
    ):
        line = next(ln for ln in STYLESHEET.splitlines() if ln.startswith(f"  {stack}"))
        assert line.rstrip().endswith(f"{fallback};")


def test_icons_are_drawn_and_inherit_colour() -> None:
    markup = icon("sunrise", size=20)

    assert markup.startswith("<svg ")
    assert 'stroke="currentColor"' in markup
    assert 'width="20"' in markup and 'height="20"' in markup
    # Decorative next to its own label, so it is hidden from screen readers.
    assert 'aria-hidden="true"' in markup


def test_an_unknown_icon_is_refused_rather_than_rendered_empty() -> None:
    with pytest.raises(KeyError, match="unknown icon"):
        icon("not-an-icon")


def test_icon_class_is_extendable_for_targeted_styling() -> None:
    assert 'class="icon route-mark"' in icon("route", extra_class="route-mark")


def test_registration_marks_come_as_a_set_of_four() -> None:
    """A blueprint panel reads as a drawing only with all four corners."""

    marks = corners()

    for position in ("tl", "tr", "bl", "br"):
        assert f'class="corner {position}"' in marks


def test_the_system_draws_square_rather_than_rounded() -> None:
    """Rounded corners would undo the drawing the rest of it is imitating."""

    assert "border-radius: 0" in STYLESHEET
    # No leftover soft radii from the previous system.
    for stale in ("--r-sm", "--r-md", "--r-lg", "--r-xl"):
        assert stale not in STYLESHEET


# --- The graded record must not count the same days twice --------------------


def test_evening_before_grades_are_counted_separately(tmp_path) -> None:
    """Both files describe the same nine days from two different vantage points.

    Globbing them together would advertise eighteen graded days across six
    cities, which is the same evidence claimed twice.
    """

    import json

    from certiroute.showcase import load_grade_summary

    def write(name: str, *, evening_before: bool) -> None:
        (tmp_path / name).write_text(
            json.dumps(
                {
                    "area_id": "phoenix",
                    "planned_the_evening_before": evening_before,
                    "model": {"held_out_mae_c": 0.77},
                    "graded_days": [{"realized_reduction": 0.25}],
                    "summary": {
                        "graded_day_count": 4,
                        "picked_best_start": 4,
                        "worst_regret_units": 0.0,
                    },
                }
            ),
            encoding="utf-8",
        )

    write("recommendation_grades_phoenix.json", evening_before=False)
    write("recommendation_grades_phoenix_day_ahead.json", evening_before=True)

    summary = load_grade_summary(tmp_path)

    assert summary.graded_days == 4
    assert summary.areas == ("phoenix",)
    assert summary.day_ahead_days == 4
    assert summary.day_ahead_best_start == 4
