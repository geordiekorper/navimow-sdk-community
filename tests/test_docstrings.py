"""The docstrings have the form the lint rules cannot check.

ruff holds a docstring to the code where it has a rule: an Args section names
every argument, a returned value and a raised exception are documented. It has
no rule that a function with parameters has an Args section at all, nor that a
private function or a method of a private class has a docstring. These tests
add both, for every class, function and method of the live path, and of the
tools and noxfile.py where the tests run beside them. Legacy code, moved
verbatim from upstream, is left as upstream documented it.

A function defined inside another function is not held to this: it is part of
its parent's body. One defined under an if, a try, a with or a loop at module
or class level is.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import mower_sdk

PACKAGE = Path(mower_sdk.__file__).resolve().parent
REPOSITORY = Path(__file__).resolve().parent.parent

Function = ast.FunctionDef | ast.AsyncFunctionDef


def parameters(function: Function) -> list[str]:
    """The names a caller passes: every parameter but a method's self or cls.

    Args:
        function: The function's syntax node.

    Returns:
        The parameter names in the order of the signature.
    """
    arguments = function.args
    names = [a.arg for a in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    names += [a.arg for a in (arguments.vararg, arguments.kwarg) if a is not None]
    return [name for name in names if name not in ("self", "cls")]


def gaps(module: str, source: str) -> list[str]:
    """What is missing from the docstrings of one module's source.

    Args:
        module: The module's name, for the dotted names in the result.
        source: The module's source text.

    Returns:
        One line for each class, function or method without a docstring, each
        function that takes arguments and has no Args section, and each
        function that takes none and has one; empty when nothing is missing.
    """
    found: list[str] = []

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if not isinstance(node, ast.ClassDef | Function):
                # A definition under an if, a try, a with or a loop is still a
                # definition of the module or the class.
                for field in ("body", "orelse", "finalbody"):
                    walk(getattr(node, field, []), prefix)
                for part in (*getattr(node, "handlers", []), *getattr(node, "cases", [])):
                    walk(part.body, prefix)
                continue
            name = f"{prefix}.{node.name}"
            docstring = ast.get_docstring(node)
            if not docstring:
                found.append(f"{name}: no docstring")
            elif isinstance(node, Function):
                has_section = any(line.strip() == "Args:" for line in docstring.splitlines())
                if parameters(node) and not has_section:
                    found.append(f"{name}: takes {', '.join(parameters(node))} and has no Args")
                if has_section and not parameters(node):
                    found.append(f"{name}: has an Args section and takes no argument")
            if isinstance(node, ast.ClassDef):
                walk(node.body, name)

    walk(ast.parse(source).body, module)
    return found


def test_the_live_path_has_no_gap() -> None:
    modules = sorted(PACKAGE.glob("*.py"))
    assert {"api", "errors", "location", "models", "mqtt", "sdk", "watchdog"} <= {
        path.stem for path in modules
    }
    found = [gap for path in modules for gap in gaps(path.stem, path.read_text(encoding="utf-8"))]
    assert found == []


def test_the_tools_have_no_gap() -> None:
    tools = sorted((REPOSITORY / "tools").glob("*.py"))
    if not tools:
        # The wheel check runs a copy of tests/ outside the checkout, without tools/.
        pytest.skip("the tools are not next to the tests")
    assert {"gatelib", "gitlint_rules", "port_upstream"} <= {path.stem for path in tools}
    files = [*tools, REPOSITORY / "noxfile.py"]
    found = [gap for path in files for gap in gaps(path.stem, path.read_text(encoding="utf-8"))]
    assert found == []


SAMPLE = '''
class Documented:
    """A class."""

    def method(self, value, *rest, flag=False, **options):
        """Do it.

        Args:
            value: One.
            *rest: More.
            flag: A switch.
            **options: The rest.
        """

    def no_arguments(self):
        """Take nothing."""

    @classmethod
    def build(cls):
        """Take nothing but cls."""

    def _private(self, value):
        """Private, with an argument and no section."""

    def needless(self):
        """Take nothing.

        Args:
            nothing: There is no such argument.
        """


class Bare:
    def undocumented(self):
        pass


def function(value):
    """A function without the section."""

    def nested(inner):
        return inner

    return nested(value)


async def coroutine():
    pass


if True:

    def conditional(value):
        pass
else:
    try:

        def attempted():
            pass
    except ImportError:

        def fallback():
            """Documented."""
'''


def test_the_check_finds_each_kind_of_gap_and_nothing_else() -> None:
    assert gaps("sample", SAMPLE) == [
        "sample.Documented._private: takes value and has no Args",
        "sample.Documented.needless: has an Args section and takes no argument",
        "sample.Bare: no docstring",
        "sample.Bare.undocumented: no docstring",
        "sample.function: takes value and has no Args",
        "sample.coroutine: no docstring",
        "sample.conditional: no docstring",
        "sample.attempted: no docstring",
    ]
