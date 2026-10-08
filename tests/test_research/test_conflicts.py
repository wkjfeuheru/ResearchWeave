"""Conflicts retain provenance and cannot turn stale recommendations into facts."""

import json

import pytest

from openharness.research.models import ArbitrationDecision, new_id
from openharness.research.store import ResearchError, ResearchStore
from openharness.utils.fs import atomic_write_text


def apply(store, action, **fields):
    return store.apply({"action": action, "operation_id": new_id("op"),
                        "expected_revision": store.load().revision, **fields})


def assessment(evidence_id):
    return {"evidence_id": evidence_id, "originality": "来自原始公告",
            "directness": "原文直接披露该指标", "scope_match": "同一公司及报告期",
            "timing_and_corrections": "检查发布日期及更正关系", "independence": "保留各自原始出处，不计转载票数",
            "reproducibility": "按原表格可复核"}


def decision(evidence_ids, step_ids, outcome="prefer_side"):
    result = {"outcome": outcome, "statement": "营收应按更正后的10亿元列示", "rationale": "核对双方原文和更正关系",
              "evidence_ids": evidence_ids, "step_ids": step_ids,
              "assessments": [assessment(key) for key in evidence_ids]}
    if outcome == "prefer_side":
        result.update(preferred_side=1, rejected_reasons=["原公告的12亿元已被更正"])
    if outcome == "conditional":
        result["conditions"] = ["合并口径12亿元，母公司口径10亿元"]
    if outcome == "unresolved":
        result["remaining_gaps"] = ["尚未取得明确解释差额的原始资料"]
    return ArbitrationDecision.model_validate(result)


def make_research(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "research")
    user = store.capture(origin_id="user", content="研究公司A营收", kind="user")
    apply(store, "set_context", goal="研究公司A营收", user_source_ids=[user.id])
    plan = apply(store, "create_plan", title="研究公司A", tasks=["核查资料"])
    apply(store, "update_task", task_id=plan["tasks"][0]["id"], status="in_progress")
    evidence_ids, step_ids, claims = [], [], []
    for index, amount in enumerate((12, 10)):
        source = store.capture(origin_id=f"report-{index}", content=f"营收{amount}亿元", kind="web",
                               locator=f"https://source{index}.example/report", published_at=f"2026-09-0{index + 1}")
        ev = apply(store, "add_evidence", source_id=source.id, statement=f"营收{amount}亿元")["evidence_id"]
        step = apply(store, "add_reasoning", evidence_ids=[ev], method="对照原文",
                     result=f"原文披露{amount}亿元", output=f"营收{amount}亿元")["step_id"]
        claim = apply(store, "add_conclusion", evidence_ids=[ev], step_ids=[step], statement=f"营收{amount}亿元")["conclusion_id"]
        evidence_ids.append(ev)
        step_ids.append(step)
        claims.append(claim)
    return store, evidence_ids, step_ids, claims


@pytest.fixture
def research(tmp_path):
    return make_research(tmp_path)


def conflict(research, **fields):
    store, evidence_ids, step_ids, claims = research
    sides = [{"statement": store.load().conclusions[claim].statement, "evidence_ids": [ev],
              "step_ids": [step], "conclusion_ids": [claim], "subject": "公司A", "period": "2026H1", "unit": "亿元"}
             for ev, step, claim in zip(evidence_ids, step_ids, claims)]
    return apply(store, "add_conflict", question="2026H1营收究竟为12还是10亿元？", kind="fact", sides=sides, **fields)["conflict_id"]


def investigate(research, conflict_id, outcome="prefer_side", **kwargs):
    store, evs, steps, _ = research
    arb, _ = store.begin_investigation(conflict_id, **kwargs)
    report = decision(evs, steps, outcome)
    store.finish_investigation(arb.id, report=report)
    return arb.id, report


@pytest.mark.parametrize("outcome", ["prefer_side", "compatible", "conditional", "unresolved"])
def test_atomic_decisions_preserve_all_old_claims(research, outcome):
    store, evs, _, claims = research
    frozen, _ = store.render_answer(f"原披露为12亿元[E:{evs[0]}]", "old-answer")
    cid = conflict(research)
    assert all(store.load().conclusions[key].needs_review for key in claims)
    arb_id, report = investigate(research, cid, outcome)
    command = {"action": "resolve_conflict", "operation_id": "resolve-once", "expected_revision": store.load().revision,
               "conflict_id": cid, "arbitration_id": arb_id, "decision": report.model_dump(mode="json")}
    result = store.apply(command)
    assert store.apply(command) == result | {"current_revision": store.load().revision}
    memory = store.load()
    arbitration = memory.arbitrations[arb_id]
    assert arbitration.replaced_conclusion_ids == claims
    assert all(key in memory.conclusions for key in claims)
    claim = memory.conclusions[result["conclusion_id"]]
    assert claim.status == ("conflicted" if outcome == "unresolved" else "tentative")
    assert claim.needs_review == (outcome == "unresolved")
    assert [item["id"] for item in store.view(memory)["conclusions"]] == [claim.id]
    assert memory.conflicts[cid].status == ("unresolved" if outcome == "unresolved" else "resolved")
    assert store.render_answer("ignored new text", "old-answer")[0] == frozen
    with pytest.raises(ResearchError, match="already investigated"):
        store.begin_investigation(cid)


def test_resolution_does_not_upgrade_verification(research):
    store, _, _, _ = research
    cid = conflict(research)
    arb_id, report = investigate(research, cid)
    before = store.load().revision
    with pytest.raises(ResearchError, match="verified evidence"):
        apply(store, "resolve_conflict", conflict_id=cid, arbitration_id=arb_id,
              decision=report.model_dump(), conclusion_status="verified")
    assert store.load().revision == before
    assert store.load().conflicts[cid].status == "awaiting_review"


def test_core_conflict_blocks_verified_claim_and_warns_final_answer(research):
    store, evs, steps, _ = research
    cid = conflict(research)
    with pytest.raises(ResearchError, match="core conflict"):
        apply(store, "add_conclusion", statement="确定为12亿元", evidence_ids=evs, step_ids=steps, status="verified")
    assert cid in store.prompt(20000)
    assert "核心争议尚未解决" in store.completion_warning()


def test_noncore_conflict_does_not_mark_conclusions_for_review(research):
    store, _, _, claims = research
    conflict(research, core=False)
    assert not any(store.load().conclusions[key].needs_review for key in claims)


def test_noncore_conflict_can_be_promoted_and_marks_dependent_claims(research):
    store, evs, steps, claims = research
    derived = apply(store, "add_conclusion", statement="相关营收判断", evidence_ids=evs, step_ids=steps)["conclusion_id"]
    cid = conflict(research, core=False)
    assert conflict(research, core=True) == cid
    memory = store.load()
    assert memory.conflicts[cid].core
    assert all(memory.conclusions[key].needs_review for key in claims + [derived])


def test_evidence_revision_reopens_decision_and_rejects_stale_report(research):
    store, evs, _, _ = research
    cid = conflict(research)
    arb_id, report = investigate(research, cid)
    original = store.load().evidence_pool[evs[1]]
    apply(store, "add_evidence", source_id=original.source_id, statement="数字需要进一步核查", supersedes=original.id)
    assert store.load().conflicts[cid].status == "open"
    with pytest.raises(ResearchError, match="latest completed|inputs changed"):
        apply(store, "resolve_conflict", conflict_id=cid, arbitration_id=arb_id, decision=report.model_dump())
    store.begin_investigation(cid)  # New evidence versions permit a fresh attempt.


def test_revision_of_cross_source_support_invalidates_dependents(research):
    store, evs, steps, _ = research
    # Supporting evidence is itself a dependency even if no calculation uses it.
    support = store.load().evidence_pool[evs[0]]
    dependant = apply(store, "add_evidence", source_id=support.source_id, statement="依赖佐证的判断",
                      supporting_evidence_ids=[evs[1]])["evidence_id"]
    derived_step = apply(store, "add_reasoning", evidence_ids=[dependant], method="佐证",
                         result="根据佐证形成判断", output="判断")["step_id"]
    claim = apply(store, "add_conclusion", statement="判断", evidence_ids=[dependant], step_ids=[derived_step])["conclusion_id"]
    original = store.load().evidence_pool[evs[1]]
    apply(store, "add_evidence", source_id=original.source_id, statement="撤回", status="retracted", supersedes=original.id)
    assert store.load().evidence_pool[dependant].needs_review
    assert store.load().conclusions[claim].needs_review


def test_counterevidence_reopens_and_scope_changes_reject_old_results(research):
    store, evs, _, _ = research
    cid = conflict(research)
    arb_id, report = investigate(research, cid)
    apply(store, "resolve_conflict", conflict_id=cid, arbitration_id=arb_id, decision=report.model_dump())
    apply(store, "reopen_conflict", conflict_id=cid, reason="用户要求检查反证", evidence_ids=evs)
    arb, _ = store.begin_investigation(cid, retry=True)
    apply(store, "create_plan", title="新研究", tasks=["新范围"], reused_evidence_ids=evs)
    result = store.finish_investigation(arb.id, report=report)
    assert result["status"] == "stale"
    assert not result["report"]
    with pytest.raises(ResearchError, match="current research scope"):
        apply(store, "reopen_conflict", conflict_id=cid, reason="旧争议")


def test_one_worker_recovery_and_explicit_retry(research):
    store, _, _, _ = research
    cid = conflict(research)
    arb, _ = store.begin_investigation(cid)
    with pytest.raises(ResearchError, match="Only one"):
        store.begin_investigation(cid, retry=True)
    recovered = ResearchStore(store.directory.parent, store.session_id, root=store.directory.parent)
    recovered.recover_investigations()
    assert recovered.load().arbitrations[arb.id].status == "interrupted"
    revision = recovered.load().revision
    recovered.recover_investigations()
    assert recovered.load().revision == revision
    with pytest.raises(ResearchError, match="already investigated"):
        recovered.begin_investigation(cid)
    recovered.begin_investigation(cid, retry=True)


def test_both_sides_must_be_examined_and_ids_are_session_local(research, tmp_path):
    store, evs, steps, _ = research
    cid = conflict(research)
    arb, _ = store.begin_investigation(cid)
    incomplete = decision(evs, steps).model_copy(update={"assessments": [decision(evs, steps).assessments[0]]})
    with pytest.raises(ResearchError, match="every side"):
        store.finish_investigation(arb.id, report=incomplete)
    assert store.load().arbitrations[arb.id].status == "running"
    other = ResearchStore(tmp_path, "b" * 12, root=tmp_path / "other")
    with pytest.raises(ResearchError, match="Unknown"):
        other.apply({"action": "read", "ids": [cid]})
    bad = decision(evs, steps).model_copy(update={"evidence_ids": ["ev_elsewhere"]})
    with pytest.raises(ResearchError, match="Unknown"):
        store.finish_investigation(arb.id, report=bad)


def test_original_schema_loads_with_empty_conflicts(research):
    store, _, _, _ = research
    raw = json.loads(store.path.read_text())
    raw.pop("conflicts")
    raw.pop("arbitrations")
    store.path.write_text(json.dumps(raw))
    assert not store.load().conflicts and not store.load().arbitrations


def seed_staging(store, baseline, tmp_path):
    staging = ResearchStore(tmp_path, "b" * 12, root=tmp_path / "staging")
    cloned = baseline.model_copy(deep=True)
    cloned.session_id = staging.session_id
    cloned.conflicts, cloned.arbitrations, cloned.operations, cloned.answers = {}, {}, {}, {}
    for source in cloned.sources.values():
        atomic_write_text(staging.directory / source.snapshot, store.read_source(source))
    staging._save(cloned, "seed", {})
    return staging


@pytest.mark.parametrize("status", ["timeout", "budget_exhausted", "interrupted", "failed"])
def test_partial_investigations_import_snapshots_without_formal_conclusions(research, tmp_path, status):
    store, _, _, claims = research
    cid = conflict(research)
    arb, baseline = store.begin_investigation(cid)
    staging = seed_staging(store, baseline, tmp_path)
    source = staging.capture(origin_id="child-tool", kind="web", content="更正说明原文", locator="https://new.example/report")
    ev = apply(staging, "add_evidence", source_id=source.id, statement="已取得更正说明")["evidence_id"]
    step = apply(staging, "add_reasoning", evidence_ids=[ev], method="核对", result="已取得原文", output="待进一步判断")["step_id"]
    result = store.finish_investigation(arb.id, staging=staging, baseline=baseline, status=status)
    memory = store.load()
    assert len(result["imported_ids"]) == 3
    assert source.id not in memory.sources and ev not in memory.evidence_pool and step not in memory.reasoning_chain
    imported_source = next(memory.sources[key] for key in result["imported_ids"] if key in memory.sources)
    assert store.read_source(imported_source) == "更正说明原文"
    assert set(memory.conclusions) == set(claims)
    assert memory.conflicts[cid].status == "interrupted"
    with pytest.raises(ResearchError, match="already investigated"):
        store.begin_investigation(cid)


def test_investigation_import_maps_report_and_keeps_parent_inputs(research, tmp_path):
    store, evs, steps, _ = research
    cid = conflict(research)
    arb, baseline = store.begin_investigation(cid)
    staging = seed_staging(store, baseline, tmp_path)
    check = apply(staging, "add_reasoning", evidence_ids=[evs[1]], method="原文核验", result="核对完整原文",
                  output="更正数字为10亿元", verification=True)["step_id"]
    checked = apply(staging, "verify_evidence", evidence_id=evs[1], level="source_checked", method="source",
                    verification_step_id=check, verification_note="已读取完整原文")["evidence_id"]
    synthesis = apply(staging, "add_reasoning", evidence_ids=[checked], method="引用已核对版本",
                      result="更正数字为10亿元", output="采信更正后的披露")["step_id"]
    report = decision(evs + [checked], steps + [check, synthesis])
    result = store.finish_investigation(arb.id, staging=staging, baseline=baseline, report=report)
    assert result["input_fingerprint"] == arb.input_fingerprint
    assert result["review_fingerprint"] != result["input_fingerprint"]
    imported = result["report"]["evidence_ids"][-1]
    assert imported != checked
    memory = store.load()
    assert memory.evidence_pool[evs[1]] == baseline.evidence_pool[evs[1]]
    assert memory.evidence_pool[imported].status == "source_checked"
    assert memory.evidence_pool[imported].supersedes is None
    apply(store, "resolve_conflict", conflict_id=cid, arbitration_id=arb.id, decision=result["report"])


def test_resolved_conflict_reopens_on_new_version_of_accepted_evidence(research):
    store, evs, _, _ = research
    cid = conflict(research)
    arb_id, report = investigate(research, cid)
    result = apply(store, "resolve_conflict", conflict_id=cid, arbitration_id=arb_id, decision=report.model_dump())
    original = store.load().evidence_pool[evs[1]]
    apply(store, "add_evidence", source_id=original.source_id, statement="新的更正版本", supersedes=original.id)
    assert store.load().conflicts[cid].status == "open"
    assert store.load().conclusions[result["conclusion_id"]].needs_review


def test_import_never_overwrites_a_corrupted_existing_snapshot(research, tmp_path):
    store, evs, _, _ = research
    cid = conflict(research)
    arb, baseline = store.begin_investigation(cid)
    staging = seed_staging(store, baseline, tmp_path)
    existing = baseline.sources[baseline.evidence_pool[evs[0]].source_id]
    staging.capture(origin_id="new-copy", kind="web", content=staging.read_source(existing))
    path = store.directory / existing.snapshot
    path.write_text("tampered original", encoding="utf-8")
    with pytest.raises(ResearchError, match="快照校验失败"):
        store.finish_investigation(arb.id, staging=staging, baseline=baseline, status="timeout")
    assert path.read_text() == "tampered original"
    assert store.load().revision == baseline.revision


@pytest.mark.parametrize("outcome,fields", [("conditional", {}), ("unresolved", {}), ("prefer_side", {"preferred_side": 0})])
def test_decision_requires_conditions_gaps_or_rejection_reasons(research, outcome, fields):
    _, evs, steps, _ = research
    raw = decision(evs, steps, "compatible").model_dump()
    with pytest.raises(ValueError):
        ArbitrationDecision.model_validate(raw | {"outcome": outcome, **fields})
