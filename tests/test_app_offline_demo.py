"""What someone opening the live link on an unanticipated date must see.

The whole-day aggregate is a same-day signal, so a reading not captured while
its day was current can never be bought again; and a hosted deployment starts
every restart with an empty snapshot cache. Between those two facts, planning
"today" on a deployed instance stops working the moment the credits or the
publishing window run out - which is precisely when a reviewer is likely to
look. The days this repository ships have to carry the demo instead.
"""

from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import certiroute.map_picker as map_picker
from certiroute.config import get_settings
from certiroute.fortyguard import FortyGuardClient
from certiroute.measured import (
    DEFAULT_LEVEL_PATH,
    DEFAULT_PROFILE_PATH,
    available_days,
    level_days,
)
from tests.test_app_review import MapDriver, _button, _review

APP_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def offline_app(tmp_path_factory: pytest.TempPathFactory):
    """A deployment as a judge meets it: empty cache, every request refused."""

    patch = pytest.MonkeyPatch()
    driver = MapDriver()
    calls: list[str] = []

    def reject(*_args: object, **_kwargs: object) -> object:
        calls.append("network")
        raise AssertionError("a shipped day must not cost a request")

    patch.setenv("FORTYGUARD_API_KEY", "offline-test-key")
    patch.setenv("CERTIROUTE_NOW", "04:30")
    patch.setattr(FortyGuardClient, "create_heatmap", reject)
    patch.setattr(FortyGuardClient, "submit_heatmap", reject)
    patch.setattr(map_picker, "render_map_picker", driver.render)
    get_settings.cache_clear()

    # No snapshot cache at all - the state every restart begins in.
    patch.setenv(
        "CERTIROUTE_HEATMAP_CACHE_PATH", str(tmp_path_factory.mktemp("empty_cache"))
    )

    app = AppTest.from_file(APP_PATH)
    app.run(timeout=90)
    _button(app, "Load the Phoenix walkthrough").click().run(timeout=90)

    try:
        yield app, calls
    finally:
        patch.undo()
        get_settings.cache_clear()


def shipped_and_reviewable() -> list[date]:
    return [
        day
        for day in level_days("phoenix", path=ROOT / DEFAULT_LEVEL_PATH)
        if day in set(available_days("phoenix", path=ROOT / DEFAULT_PROFILE_PATH))
    ]


def test_the_repository_ships_days_that_need_no_request() -> None:
    days = shipped_and_reviewable()

    assert len(days) >= 20, "too few self-contained days to carry a demo"


def test_a_shipped_day_renders_with_every_request_refused(offline_app) -> None:
    """The judge's path: pick a day the build carries, and see the product."""

    app, calls = offline_app
    target = max(shipped_and_reviewable())

    _review(app, target)
    _button(app, "Create my heat-aware route").click().run(timeout=120)

    assert not app.exception
    text = " ".join(str(block.value) for block in app.markdown)
    assert "Reviewing a finished day" in text
    assert calls == [], "a shipped day must cost nothing"
