from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_app_renders_without_exceptions() -> None:
    app_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    app = AppTest.from_file(app_path)

    app.run(timeout=15)

    assert not app.exception
    assert "CertiRoute" in app.title[0].value
