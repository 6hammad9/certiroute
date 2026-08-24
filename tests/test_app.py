from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_app_renders_without_exceptions(tmp_path, monkeypatch) -> None:
    app_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    monkeypatch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(tmp_path))
    app = AppTest.from_file(app_path)

    app.run(timeout=15)

    assert not app.exception
    assert any("CertiRoute" in str(block.value) for block in app.markdown)
