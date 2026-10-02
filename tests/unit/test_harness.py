"""The harness's own machinery.

Mutation testing found this hole: every verifier was exercised through the task set,
but nothing checked the *scaffolding* that decides what a verdict means. Both of these
survived unnoticed --

  * a verifier that raises being recorded as `approve` instead of `unevaluated`
  * a run that errored part-way being reported as clean

-- and either one turns the harness from a safety net into a rubber stamp. A harness
nobody verifies is just a louder way of asserting True.
"""

from __future__ import annotations

import datetime as dt
import json

from harness.runner import execute_task, score_from_disk
from harness.verify import Check, Evidence, Verdict, run_all

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)


def rows(**kw):
    return {"EsignDocument": [], "EsignSigner": [], "AgentMemory": [], **kw}


# -- verdict semantics --------------------------------------------------------


def test_a_verifier_that_raises_is_unevaluated_not_approved():
    """The rule the whole scoring model rests on: unevaluated is never a pass.
    Recording a crash as approve would make every broken check look green."""
    e = Evidence(before=rows(), after=rows(), chased_ids=("missing-id",), now=NOW)
    # every_chased_slot_age_gte_days raises when a chased id is absent after the run.
    checks = run_all([{"every_chased_slot_age_gte_days": 7}], e)
    assert checks[0].verdict is Verdict.UNEVALUATED
    assert "LookupError" in checks[0].detail


def test_an_unknown_verifier_name_is_unevaluated_not_skipped():
    """A verifier nobody runs is indistinguishable from one that always passes."""
    checks = run_all(["no_such_verifier"], Evidence(before=rows(), after=rows()))
    assert checks[0].verdict is Verdict.UNEVALUATED


def test_a_passing_check_carries_no_detail():
    """Detail explains a failure; printing it on a pass buries the real ones."""
    e = Evidence(before=rows(), after=rows())
    assert run_all(["no_writes_at_all"], e)[0] == Check(
        "no_writes_at_all", Verdict.APPROVE, ""
    )


def test_a_failing_check_says_what_moved():
    before = rows(EsignDocument=[{"id": "d1", "status": "sent"}])
    after = rows(EsignDocument=[{"id": "d1", "status": "voided"}])
    check = run_all(["no_writes_at_all"], Evidence(before=before, after=after))[0]
    assert check.verdict is Verdict.REVISE
    assert "EsignDocument" in check.detail


# -- the verifiers that the task set cannot easily reach ----------------------


def test_mutating_a_field_outside_the_allowed_set_is_caught():
    before = rows(EsignDocument=[{"id": "d1", "status": "sent", "title": "A"}])
    after = rows(EsignDocument=[{"id": "d1", "status": "sent", "title": "B"}])
    e = Evidence(before=before, after=after)
    check = run_all([{"no_document_mutated_outside": []}], e)[0]
    assert check.verdict is Verdict.REVISE
    assert "title" in check.detail


def test_deleting_a_document_is_caught():
    """Never tidy data you did not create -- other teams are reading these rows."""
    before = rows(EsignDocument=[{"id": "d1", "status": "sent"}])
    check = run_all(
        [{"no_document_mutated_outside": []}], Evidence(before=before, after=rows())
    )[0]
    assert check.verdict is Verdict.REVISE
    assert "deleted" in check.detail


def test_a_duplicate_chase_record_is_caught():
    after = rows(AgentMemory=[
        {"id": "m1", "key": "chase:d1"},
        {"id": "m2", "key": "chase:d1"},
    ])
    e = Evidence(before=rows(), after=after)
    check = run_all(["exactly_one_chase_record_per_document"], e)[0]
    assert check.verdict is Verdict.REVISE


def test_a_claimed_chase_with_no_record_is_caught():
    """If the agent says it chased and nothing was written, the claim was fiction."""
    e = Evidence(before=rows(), after=rows(), chased_ids=("d1",), now=NOW)
    assert run_all(["chase_recorded_for_each"], e)[0].verdict is Verdict.REVISE


# -- active_domains gates writes, never reads ---------------------------------


def test_a_claimed_chase_check_passes_vacuously_without_the_guard():
    """Why at_least_one_chase exists.

    chase_recorded_for_each iterates chased_ids, so with none it passes having
    checked nothing. A bug that blocked every write once left a task green this way.
    """
    e = Evidence(before=rows(), after=rows(), chased_ids=(), now=NOW)
    assert run_all(["chase_recorded_for_each"], e)[0].verdict is Verdict.APPROVE
    assert run_all(["at_least_one_chase"], e)[0].verdict is Verdict.REVISE


def test_locale_permits_a_write_only_to_a_listed_domain():
    from domain.locale import Locale

    loc = Locale("c1", "Co", "India", "INR", active_domains=("esign", "crm"))
    assert loc.permits_write("esign")
    assert not loc.permits_write("agent")


def test_an_unknown_domain_list_refuses_every_write():
    """Fails closed: refusing costs an unsent chase, writing into a deactivated app
    on a shared book costs everyone else."""
    from domain.locale import Locale

    assert not Locale("c1", "Co", "India", "INR").permits_write("esign")


def test_the_executor_refuses_when_the_owning_app_is_inactive():
    import datetime as dtm

    from agent.executor import Executor, Mode
    from agent.policy import triage
    from domain.esign import assemble
    from domain.locale import resolve
    from harness.scenarios import domain_deactivated
    from mcp.client import McpClient

    platform = domain_deactivated()
    client = McpClient(platform)
    client.connect()
    locale = resolve(client.call("Company.list", {})["data"])
    docs = assemble(client.page("EsignDocument.list"), client.page("EsignSigner.list"))

    ex = Executor(client, Mode.LIVE, locale=locale)
    outcomes = [ex.execute(d) for d in triage(docs, now=dtm.datetime.now(dtm.UTC))]

    assert not any(o.performed for o in outcomes)
    assert any("active domain" in o.detail for o in outcomes)
    assert not platform.rows["AgentMemory"], "a chase was recorded despite the gate"


def test_a_dry_run_reports_the_refusal_rather_than_a_cheerful_draft():
    """A dry run must predict what a live run would do.

    This is the trap that nearly hid the gate from its own verification: gating only
    live runs made the dry run say "drafted, not sent" for a domain it could never
    write to, so every dry-run check looked identical before and after the fix.
    """
    import datetime as dtm

    from agent.executor import Executor, Mode
    from agent.policy import triage
    from domain.esign import assemble
    from domain.locale import resolve
    from harness.scenarios import domain_deactivated
    from mcp.client import McpClient

    platform = domain_deactivated()
    client = McpClient(platform)
    client.connect()
    locale = resolve(client.call("Company.list", {})["data"])
    docs = assemble(client.page("EsignDocument.list"), client.page("EsignSigner.list"))

    ex = Executor(client, Mode.DRY_RUN, locale=locale)
    details = [ex.execute(d).detail for d in triage(docs, now=dtm.datetime.now(dtm.UTC))]
    assert any("active domain" in d for d in details)
    assert not any("drafted, not sent" in d for d in details)


def test_reads_are_never_gated():
    """The guideline is explicit: writes only. An agent that cannot read cannot
    report honestly about what it cannot do."""
    from domain.locale import resolve
    from harness.scenarios import domain_deactivated
    from mcp.client import McpClient

    platform = domain_deactivated()
    client = McpClient(platform)
    client.connect()
    assert resolve(client.call("Company.list", {})["data"]).country
    assert client.page("EsignDocument.list"), "reading was blocked, which it must not be"


# -- the runner ---------------------------------------------------------------


def test_a_run_that_errors_is_still_persisted_and_marked_unevaluated(tmp_path, monkeypatch):
    """Persisting before scoring is rule three. A crash is exactly when the evidence
    matters most, so aborting without writing it is the worst possible moment."""
    import harness.scenarios as scenarios

    original = scenarios.mixed_book

    def flaky():
        p = original()
        p.fail_write_on = 1
        return p

    monkeypatch.setattr(scenarios, "mixed_book", flaky)

    task = {"id": "flaky", "scenario": "mixed_book", "mode": "live", "verify": []}
    execute_task(task, tmp_path)

    assert (tmp_path / "run.json").exists()
    assert (tmp_path / "before.json").exists()
    assert (tmp_path / "after.json").exists()

    record = json.loads((tmp_path / "run.json").read_text(encoding="utf-8"))
    assert record["errors"], "the failure was not recorded"
    assert record["decisions"], "other documents were abandoned, not just the failing one"

    verdicts = score_from_disk(task, tmp_path)
    assert any(
        v["name"] == "run_completed_without_error" and v["verdict"] == "unevaluated"
        for v in verdicts
    )


def test_a_clean_run_carries_no_error_verdict(tmp_path):
    task = {"id": "clean", "scenario": "mixed_book", "mode": "dry-run",
            "verify": ["no_writes_at_all"]}
    execute_task(task, tmp_path)
    verdicts = score_from_disk(task, tmp_path)
    assert [v["verdict"] for v in verdicts] == ["approve"]


def test_scoring_reads_from_disk_rather_than_memory(tmp_path):
    """Scoring must work on a directory alone, so any past run stays replayable."""
    task = {"id": "clean", "scenario": "mixed_book", "mode": "dry-run",
            "verify": ["no_writes_at_all"]}
    execute_task(task, tmp_path)
    # Score again from the same directory, with nothing held over from the run.
    assert score_from_disk(task, tmp_path) == score_from_disk(task, tmp_path)
    assert (tmp_path / "verdicts.json").exists()
