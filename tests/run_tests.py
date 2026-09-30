"""Tiny pytest compatible runner for environments without pytest.

It implements just enough of the pytest surface the test suite uses:
``raises``, conftest fixtures, fixture injection and the built in
``tmp_path`` / ``capsys`` helpers.  Run it with
``python3 -m tests.run_tests``.
"""

from __future__ import annotations

import importlib
import inspect
import io
import sys
import tempfile
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Callable


class _Raises:
    def __init__(self, exc_type: type[BaseException]) -> None:
        self.exc_type = exc_type
        self.value: BaseException | None = None

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            raise AssertionError(f"{self.exc_type.__name__} was not raised")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        return True


class _CapSys:
    def __init__(self) -> None:
        self._stdout = io.StringIO()
        self._stderr = io.StringIO()

    def readouterr(self) -> SimpleNamespace:
        return SimpleNamespace(out=self._stdout.getvalue(), err=self._stderr.getvalue())


class _Approx:
    def __init__(self, expected: float, rel: float = 1e-6, abs_: float = 1e-6) -> None:
        self.expected = expected
        self.rel = rel
        self.abs = abs_

    def __eq__(self, other: object) -> bool:
        try:
            difference = abs(float(other) - float(self.expected))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return NotImplemented
        return difference <= max(self.abs, self.rel * abs(float(self.expected)))  # type: ignore[arg-type]


class _PytestShim(ModuleType):
    ANY = object()

    @staticmethod
    def approx(expected: float, rel: float = 1e-6, abs: float = 1e-6) -> _Approx:
        return _Approx(expected, rel=rel, abs_=abs)

    @staticmethod
    def fixture(*args: Any, **kwargs: Any) -> Callable:
        def decorator(func: Callable) -> Callable:
            func.__pytest_fixture__ = True
            return func

        if args and callable(args[0]):
            return decorator(args[0])
        return decorator

    @staticmethod
    def raises(exc_type: type[BaseException]) -> _Raises:
        return _Raises(exc_type)

    @staticmethod
    def skip(reason: str = "") -> None:
        raise _Skipped(reason)


class _Skipped(Exception):
    pass


def _fixtures(module: ModuleType) -> dict[str, Callable]:
    return {
        name: value
        for name, value in vars(module).items()
        if callable(value) and getattr(value, "__pytest_fixture__", False)
    }


def _build_fixture(name: str, fixtures: dict[str, Callable], cache: dict[str, Any]) -> Any:
    if name in cache:
        return cache[name]
    if name == "tmp_path":
        cache[name] = Path(tempfile.mkdtemp())
        return cache[name]
    if name == "capsys":
        cache[name] = _CapSys()
        return cache[name]
    factory = fixtures[name]
    kwargs = {
        param: _build_fixture(param, fixtures, cache)
        for param in inspect.signature(factory).parameters
    }
    cache[name] = factory(**kwargs)
    return cache[name]


def run_module(module: ModuleType, fixtures: dict[str, Callable]) -> tuple[int, int, int]:
    passed = failed = skipped = 0
    tests = [(name, obj) for name, obj in vars(module).items() if name.startswith("test_") and callable(obj)]
    for name, test in sorted(tests):
        cache: dict[str, Any] = {}
        capsys: _CapSys | None = None
        kwargs: dict[str, Any] = {}
        try:
            for param, bound in inspect.signature(test).parameters.items():
                if bound.default is not inspect.Parameter.empty and param not in fixtures and param not in ("tmp_path", "capsys"):
                    continue
                value = _build_fixture(param, fixtures, cache)
                if isinstance(value, _CapSys):
                    capsys = value
                kwargs[param] = value
            if capsys is not None:
                with redirect_stdout(capsys._stdout), redirect_stderr(capsys._stderr):
                    test(**kwargs)
            else:
                test(**kwargs)
            passed += 1
            print(f"  PASS {module.__name__}.{name}")
        except _Skipped as exc:
            skipped += 1
            print(f"  SKIP {module.__name__}.{name} ({exc})")
        except Exception:  # noqa: BLE001 - report any test failure
            failed += 1
            print(f"  FAIL {module.__name__}.{name}")
            traceback.print_exc()
    return passed, failed, skipped


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    shim = _PytestShim("pytest")
    sys.modules.setdefault("pytest", shim)

    import tests  # noqa: F401
    import tests.conftest as conftest

    shared_fixtures = _fixtures(conftest)
    tests_dir = Path(__file__).resolve().parent
    total_pass = total_fail = total_skip = 0
    for path in sorted(tests_dir.glob("test_*.py")):
        module_name = f"tests.{path.stem}"
        print(module_name)
        module = importlib.import_module(module_name)
        fixtures = {**shared_fixtures, **_fixtures(module)}
        passed, failed, skipped = run_module(module, fixtures)
        total_pass += passed
        total_fail += failed
        total_skip += skipped
    print(f"\n{total_pass} passed, {total_fail} failed, {total_skip} skipped")
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
