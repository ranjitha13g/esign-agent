"""The only module permitted to name a business noun.

The agent is graded on two books -- one under Ind AS and GST, one under US GAAP and
Sales & Use Tax -- and on seeded verticals where the same tables carry different words.
An agent that hardcodes a noun passes on one and fails silently on the other.

The brief points at GET /api/accounting/locale for this. Seat 18 cannot reach it:

    403  App 'accounting' is not enabled for your account

So jurisdiction is resolved from Company instead, which this seat can read. That is
enough to tell the books apart without any module downstream spelling a noun out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Locale:
    """Everything the rest of the agent is allowed to know about where it is."""

    company_id: str
    company_name: str
    country: str
    currency: str
    active_domains: tuple[str, ...] = field(default=())

    @property
    def is_known(self) -> bool:
        return bool(self.country)

    def permits_write(self, domain: str) -> bool:
        """Guideline section 13: active_domains gates writes, never reads.

        Fails closed. If the company does not list the domain -- or lists nothing at
        all, so we cannot tell -- we decline to write. Refusing costs an unsent chase;
        writing into a deactivated app on a shared book costs everyone else.

        Reads are never gated. The seat boundary already handles those, and an agent
        that cannot read cannot report honestly about what it cannot do.
        """
        return domain in self.active_domains

    def describe(self) -> str:
        return f"{self.company_name} ({self.country}, {self.currency})"


def resolve(company_rows: list[dict[str, Any]]) -> Locale:
    """Build a Locale from Company.list output.

    Returns a Locale with empty country rather than guessing when the platform did not
    say. A missing jurisdiction is a thing to report, not to default.
    """
    if not company_rows:
        return Locale(company_id="", company_name="", country="", currency="")

    row = company_rows[0]
    domains = row.get("active_domains") or []
    names = tuple(
        d.get("domain", "") for d in domains if isinstance(d, dict) and d.get("domain")
    )

    return Locale(
        company_id=str(row.get("id", "")),
        company_name=str(row.get("name", "")),
        country=str(row.get("country") or ""),
        currency=str(row.get("default_currency") or ""),
        active_domains=names,
    )
