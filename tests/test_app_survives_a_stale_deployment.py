"""The app must boot even when the library it calls is older than it is.

A hosted deployment installs certiroute once and afterwards only pulls files,
so app/main.py is re-read every run while the library can stay at the version
first deployed. That skew took the whole app down twice: once on a keyword
argument the old library did not accept, once on a module that did not exist
there yet. Both were import-time failures, so nothing rendered at all.

The rule this pins down: everything app/main.py imports at module level must
either predate the deployment or be guarded. The start time is the product and
is never withheld because an addition to it is missing.
"""

import ast
import subprocess
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[1] / "app" / "main.py"

# The commit the currently deployed container installed certiroute from.
DEPLOYED_BASELINE = "511ef63"


def _names_at(revision: str, module: str) -> set[str] | None:
    base = module.replace(".", "/")
    for candidate in (f"src/{base}.py", f"src/{base}/__init__.py"):
        found = subprocess.run(
            ["git", "show", f"{revision}:{candidate}"],
            capture_output=True,
            text=True,
        )
        if found.returncode == 0:
            tree = ast.parse(found.stdout)
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                )
            }
            names |= {
                target.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            # Constants are often annotated (``X: Final = ...``), which is an
            # AnnAssign rather than an Assign and is missed without this.
            names |= {
                node.target.id
                for node in ast.walk(tree)
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            }
            names |= {
                alias.asname or alias.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            return names
    return None


def test_no_unguarded_import_postdates_the_deployed_library() -> None:
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))

    problems = []
    for node in tree.body:  # module level only; guarded imports sit inside Try
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "certiroute"
        ):
            available = _names_at(DEPLOYED_BASELINE, node.module)
            if available is None:
                problems.append(f"{node.module} did not exist at {DEPLOYED_BASELINE}")
                continue
            missing = {alias.name for alias in node.names} - available
            if missing:
                problems.append(f"{node.module} lacks {sorted(missing)}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("certiroute"):
                    if _names_at(DEPLOYED_BASELINE, alias.name) is None:
                        problems.append(f"{alias.name} did not exist")

    assert not problems, (
        "these would crash a deployment serving the older library: "
        + "; ".join(problems)
        + " - guard the import, or move the lookup into app/main.py"
    )


def test_the_app_renders_when_the_heat_limit_module_is_absent(monkeypatch) -> None:
    """The exact second outage: a module the deployed library never had."""

    class Absent:
        """Refuse one module the way an older install would."""

        def find_spec(self, name, path=None, target=None):
            if name == "certiroute.heat_limit":
                raise ImportError("simulating a deployment without this module")
            return None

    for name in [n for n in sys.modules if n.startswith("certiroute")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [Absent(), *sys.meta_path])

    app = AppTest.from_file(APP_PATH)
    app.run(timeout=90)

    assert not app.exception, "a missing addition must not stop the app booting"
    assert any("CertiRoute" in str(block.value) for block in app.markdown)
