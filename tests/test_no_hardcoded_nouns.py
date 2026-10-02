"""The agent is graded in two jurisdictions. It must not name a business noun itself.

Guideline section 6: an agent that hardcodes a tax field, assumes GST exists, or writes
an Indian invoice format into a US book passes on Suryodaya and fails on Keystone, and
the failure is silent -- the output is well formed and wrong.

domain/locale.py resolves every noun from GET /api/accounting/terminology and the entity
schemas. It is the only module permitted to spell one out. Everything else receives
resolved names as data.

This inspects string literals via the AST rather than grepping raw text, so a noun in a
comment or docstring explaining the rule does not trip the rule.

DRAFT -- re-author by hand before submission.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Modules whose job is to drive the agent, where a hardcoded noun is the bug.
SCANNED = ["agent", "domain", "harness", "mcp"]

# Two exemptions, both for modules whose job is to *be* a source of vocabulary
# rather than to consume one:
#   domain/locale.py  resolves terminology from the API
#   harness/fake.py   impersonates the platform, so it must return the platform's
#                     own values -- agent code reading from it is still bound by
#                     this rule, which is what actually matters
EXEMPT = {ROOT / "domain" / "locale.py", ROOT / "harness" / "fake.py"}

# Nouns that differ between the two books, or between the business verticals
# (school / clinic / retail / agency) where the same tables carry different words.
FORBIDDEN = {
    "customer",
    "client",
    "student",
    "patient",
    "invoice",
    "bill",
    "receipt",
    "vendor",
    "supplier",
    "employee",
    "salary",
    "payroll",
    "gst",
    "vat",
    "tds",
    "sales tax",
    "use tax",
    "ind as",
    "us gaap",
    "schedule iii",
    "rupee",
    "dollar",
    "inr",
    "usd",
}


def python_files() -> list[Path]:
    found: list[Path] = []
    for pkg in SCANNED:
        d = ROOT / pkg
        if d.exists():
            found.extend(p for p in d.rglob("*.py") if p not in EXEMPT)
    return found


def string_literals(path: Path) -> list[tuple[int, str]]:
    """Every string constant in the file, excluding docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                out.append((node.lineno, node.value))
    return out


def normalise(text: str) -> str:
    """Lowercase, with every non-letter run collapsed to a single space.

    Matching on whole tokens rather than substrings keeps protocol identifiers out of
    it: 'clientInfo' is an MCP field, not a business noun, and must not trip the check.
    The platform spells its own fields in snake_case (esign_document_id, body_text), so
    splitting on non-letters catches 'customer_name' while leaving camelCase protocol
    keys alone.
    """
    return " " + re.sub(r"[^a-z]+", " ", text.lower()).strip() + " "


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_module_names_no_business_noun(path: Path) -> None:
    offences = []
    for lineno, text in string_literals(path):
        haystack = normalise(text)
        for noun in FORBIDDEN:
            if f" {noun} " in haystack:
                where = f"{path.relative_to(ROOT)}:{lineno}"
                offences.append(f"{where} contains {noun!r}: {text[:80]!r}")

    assert not offences, (
        "Hardcoded business nouns found. Resolve these through domain/locale.py so the "
        "agent works on both books and on every vertical:\n  " + "\n  ".join(offences)
    )


def test_the_check_catches_a_planted_noun() -> None:
    """A lint nobody has seen fail is a lint nobody should trust."""
    assert " invoice " in normalise("draft the invoice")
    assert " customer " in normalise("customer_name")
    assert " gst " in normalise("GST applicable")
    # ...and does not fire on protocol identifiers.
    assert " client " not in normalise("clientInfo")


def test_the_check_itself_covers_something() -> None:
    """Guards against the scan silently matching no files and passing vacuously."""
    assert python_files(), "no modules scanned -- the noun check is not actually running"
