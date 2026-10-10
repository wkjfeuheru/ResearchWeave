"""Conflicts retain provenance and cannot turn stale recommendations into facts."""


import pytest

from researchx.state.models import ArbitrationDecision, new_id
from researchx.state.store import ResearchError, ResearchStore
from tests.postgres_helpers import raw_state, source_path


async def apply(store, action, **fields):
    return (await store.apply(
        {
            "action": action,
            "operation_id": new_id("op"),
            "expected_revision": (await store.load()).revision,
            **fields,
        }
    ))


def assessment(evidence_id):
    return {
        "evidence_id": evidence_id,
        "originality": "来自原始公告",
        "directness": "原文直接披露该指标",
        "scope_match": "同一公司及报告期",
        "timing_and_corrections": "检查发布日期及更正关系",
        "independence": "保留各自原始出处，不计转载票数",
        "reproducibility": "按原表格可复核",
    }


def decision(evidence_ids, step_ids, outcome="prefer_side"):
    result = {
        "outcome": outcome,
        "statement": "营收应按更正后的10亿元列示",
        "rationale": "核对双方原文和更正关系",
        "evidence_ids": evidence_ids,
        "step_ids": step_ids,
        "assessments": [assessment(key) for key in evidence_ids],
    }
    if outcome == "prefer_side":
        result.update(preferred_side=1, rejected_reasons=["原公告的12亿元已被更正"])
    if outcome == "conditional":
        result["conditions"] = ["合并口径12亿元，母公司口径10亿元"]
    if outcome == "unresolved":
        result["remaining_gaps"] = ["尚未取得明确解释差额的原始资料"]
    return ArbitrationDecision.model_validate(result)


async def make_research(tmp_path):
    store = ResearchStore(tmp_path, "a" * 12, root=tmp_path / "research")
    user = (await store.capture(origin_id="user", content="研究公司A营收", kind="user"))
    (await apply(store, "set_context", goal="研究公司A营收", user_source_ids=[user.id]))
    plan = (await apply(store, "create_plan", title="研究公司A", tasks=["核查资料"]))
    (await apply(store, "update_task", task_id=plan["tasks"][0]["id"], status="in_progress"))
    evidence_ids, step_ids, claims = [], [], []
    for index, amount in enumerate((12, 10)):
        source = (await store.capture(
            origin_id=f"report-{index}",
            content=f"营收{amount}亿元",
            kind="web",
            locator=f"https://source{index}.example/report",
            published_at=f"2026-09-0{index + 1}",
        ))
        ev = (await apply(store, "add_evidence", source_id=source.id, statement=f"营收{amount}亿元"))[
            "evidence_id"
        ]
        step = (await apply(
            store,
            "add_reasoning",
            evidence_ids=[ev],
            method="对照原文",
            result=f"原文披露{amount}亿元",
            output=f"营收{amount}亿元",
        ))["step_id"]
        claim = (await apply(
            store,
            "add_conclusion",
            evidence_ids=[ev],
            step_ids=[step],
            statement=f"营收{amount}亿元",
        ))["conclusion_id"]
        evidence_ids.append(ev)
        step_ids.append(step)
        claims.append(claim)
    return store, evidence_ids, step_ids, claims


@pytest.fixture
async def research(tmp_path):
    return (await make_research(tmp_path))


async def conflict(research, **fields):
    store, evidence_ids, step_ids, claims = research
    sides = [
        {
            "statement": (await store.load()).conclusions[claim].statement,
            "evidence_ids": [ev],
            "step_ids": [step],
            "conclusion_ids": [claim],
            "subject": "公司A",
            "period": "2026H1",
            "unit": "亿元",
        }
        for ev, step, claim in zip(evidence_ids, step_ids, claims)
    ]
    return (await apply(
        store,
        "add_conflict",
        question="2026H1营收究竟为12还是10亿元？",
        kind="fact",
        sides=sides,
        **fields,
    ))["conflict_id"]


async def investigate(research, conflict_id, outcome="prefer_side", **kwargs):
    store, evs, steps, _ = research
    arb, _ = (await store.begin_investigation(conflict_id, **kwargs))
    report = decision(evs, steps, outcome)
    (await store.finish_investigation(arb.id, report=report))
    return arb.id, report


@pytest.mark.parametrize("outcome", ["prefer_side", "compatible", "conditional", "unresolved"])
async def test_atomic_decisions_preserve_all_old_claims(research, outcome):
    store, evs, _, claims = research
    frozen, _ = (await store.render_answer(f"原披露为12亿元[E:{evs[0]}]", "old-answer"))
    cid = (await conflict(research))
    assert all(value.needs_review for key, value in (await store.load()).conclusions.items() if key in claims)
    arb_id, report = (await investigate(research, cid, outcome))
    command = {
        "action": "resolve_conflict",
        "operation_id": "resolve-once",
        "expected_revision": (await store.load()).revision,
        "conflict_id": cid,
        "arbitration_id": arb_id,
        "decision": report.model_dump(mode="json"),
    }
    result = (await store.apply(command))
    assert (await store.apply(command)) == result | {"current_revision": (await store.load()).revision}
    memory = (await store.load())
    arbitration = memory.arbitrations[arb_id]
    assert arbitration.replaced_conclusion_ids == claims
    assert all(key in memory.conclusions for key in claims)
    claim = memory.conclusions[result["conclusion_id"]]
    assert claim.status == ("conflicted" if outcome == "unresolved" else "tentative")
    assert claim.needs_review == (outcome == "unresolved")
    assert [item["id"] for item in store.view(memory)["conclusions"]] == [claim.id]
    assert memory.conflicts[cid].status == ("unresolved" if outcome == "unresolved" else "resolved")
    assert (await store.render_answer("ignored new text", "old-answer"))[0] == frozen
    with pytest.raises(ResearchError, match="already investigated"):
        (await store.begin_investigation(cid))


async def test_resolution_does_not_upgrade_verification(research):
    store, _, _, _ = research
    cid = (await conflict(research))
    arb_id, report = (await investigate(research, cid))
    before = (await store.load()).revision
    with pytest.raises(ResearchError, match="verified evidence"):
        (await apply(
            store,
            "resolve_conflict",
            conflict_id=cid,
            arbitration_id=arb_id,
            decision=report.model_dump(),
            conclusion_status="verified",
        ))
    assert (await store.load()).revision == before
    assert (await store.load()).conflicts[cid].status == "awaiting_review"


async def test_core_conflict_blocks_verified_claim_and_warns_final_answer(research):
    store, evs, steps, _ = research
    cid = (await conflict(research))
    with pytest.raises(ResearchError, match="core conflict"):
        (await apply(
            store,
            "add_conclusion",
            statement="确定为12亿元",
            evidence_ids=evs,
            step_ids=steps,
            status="verified",
        ))
    assert cid in (await store.prompt(20000))
    assert "核心争议尚未解决" in (await store.completion_warning())


async def test_noncore_conflict_does_not_mark_conclusions_for_review(research):
    store, _, _, claims = research
    (await conflict(research, core=False))
    assert not any(value.needs_review for key, value in (await store.load()).conclusions.items() if key in claims)


async def test_noncore_conflict_can_be_promoted_and_marks_dependent_claims(research):
    store, evs, steps, claims = research
    derived = (await apply(
        store, "add_conclusion", statement="相关营收判断", evidence_ids=evs, step_ids=steps
    ))["conclusion_id"]
    cid = (await conflict(research, core=False))
    assert (await conflict(research, core=True)) == cid
    memory = (await store.load())
    assert memory.conflicts[cid].core
    assert all(memory.conclusions[key].needs_review for key in claims + [derived])


async def test_evidence_revision_reopens_decision_and_rejects_stale_report(research):
    store, evs, _, _ = research
    cid = (await conflict(research))
    arb_id, report = (await investigate(research, cid))
    original = (await store.load()).evidence_pool[evs[1]]
    (await apply(
        store,
        "add_evidence",
        source_id=original.source_id,
        statement="数字需要进一步核查",
        supersedes=original.id,
    ))
    assert (await store.load()).conflicts[cid].status == "open"
    with pytest.raises(ResearchError, match="latest completed|inputs changed"):
        (await apply(
            store,
            "resolve_conflict",
            conflict_id=cid,
            arbitration_id=arb_id,
            decision=report.model_dump(),
        ))
    (await store.begin_investigation(cid))  # New evidence versions permit a fresh attempt.


async def test_revision_of_cross_source_support_invalidates_dependents(research):
    store, evs, steps, _ = research
    # Supporting evidence is itself a dependency even if no calculation uses it.
    support = (await store.load()).evidence_pool[evs[0]]
    dependant = (await apply(
        store,
        "add_evidence",
        source_id=support.source_id,
        statement="依赖佐证的判断",
        supporting_evidence_ids=[evs[1]],
    ))["evidence_id"]
    derived_step = (await apply(
        store,
        "add_reasoning",
        evidence_ids=[dependant],
        method="佐证",
        result="根据佐证形成判断",
        output="判断",
    ))["step_id"]
    claim = (await apply(
        store, "add_conclusion", statement="判断", evidence_ids=[dependant], step_ids=[derived_step]
    ))["conclusion_id"]
    original = (await store.load()).evidence_pool[evs[1]]
    (await apply(
        store,
        "add_evidence",
        source_id=original.source_id,
        statement="撤回",
        status="retracted",
        supersedes=original.id,
    ))
    assert (await store.load()).evidence_pool[dependant].needs_review
    assert (await store.load()).conclusions[claim].needs_review


async def test_counterevidence_reopens_and_scope_changes_reject_old_results(research):
    store, evs, _, _ = research
    cid = (await conflict(research))
    arb_id, report = (await investigate(research, cid))
    (await apply(
        store,
        "resolve_conflict",
        conflict_id=cid,
        arbitration_id=arb_id,
        decision=report.model_dump(),
    ))
    (await apply(store, "reopen_conflict", conflict_id=cid, reason="用户要求检查反证", evidence_ids=evs))
    arb, _ = (await store.begin_investigation(cid, retry=True))
    (await apply(store, "create_plan", title="新研究", tasks=["新范围"], reused_evidence_ids=evs))
    result = (await store.finish_investigation(arb.id, report=report))
    assert result["status"] == "stale"
    assert not result["report"]
    with pytest.raises(ResearchError, match="current research scope"):
        (await apply(store, "reopen_conflict", conflict_id=cid, reason="旧争议"))


async def test_one_worker_recovery_and_explicit_retry(research):
    store, _, _, _ = research
    cid = (await conflict(research))
    arb, _ = (await store.begin_investigation(cid))
    with pytest.raises(ResearchError, match="Only one"):
        (await store.begin_investigation(cid, retry=True))
    recovered = ResearchStore(store.cwd, store.session_id, root=store.directory.parent)
    (await recovered.recover_investigations())
    assert (await recovered.load()).arbitrations[arb.id].status == "interrupted"
    revision = (await recovered.load()).revision
    (await recovered.recover_investigations())
    assert (await recovered.load()).revision == revision
    with pytest.raises(ResearchError, match="already investigated"):
        (await recovered.begin_investigation(cid))
    (await recovered.begin_investigation(cid, retry=True))


async def test_both_sides_must_be_examined_and_ids_are_session_local(research, tmp_path):
    store, evs, steps, _ = research
    cid = (await conflict(research))
    arb, _ = (await store.begin_investigation(cid))
    incomplete = decision(evs, steps).model_copy(
        update={"assessments": [decision(evs, steps).assessments[0]]}
    )
    with pytest.raises(ResearchError, match="every side"):
        (await store.finish_investigation(arb.id, report=incomplete))
    assert (await store.load()).arbitrations[arb.id].status == "running"
    other = ResearchStore(tmp_path, "b" * 12, root=tmp_path / "other")
    with pytest.raises(ResearchError, match="Unknown"):
        (await other.apply({"action": "read", "ids": [cid]}))
    bad = decision(evs, steps).model_copy(update={"evidence_ids": ["ev_elsewhere"]})
    with pytest.raises(ResearchError, match="Unknown"):
        (await store.finish_investigation(arb.id, report=bad))


async def test_original_schema_loads_with_empty_conflicts(research):
    store, _, _, _ = research
    # Old optional fields remain accepted by the model at the explicit import boundary.
    from researchx.state.models import ResearchMemory
    raw = await raw_state(store)
    raw.pop("conflicts")
    raw.pop("arbitrations")
    restored = ResearchMemory.model_validate(raw)
    assert not restored.conflicts and not restored.arbitrations
    assert not (await store.load()).conflicts and not (await store.load()).arbitrations



async def seed_staging(store, baseline, tmp_path):
    staging = ResearchStore(tmp_path, "b" * 12, root=tmp_path / "staging")
    cloned = baseline.model_copy(deep=True)
    cloned.session_id = staging.session_id
    cloned.conflicts, cloned.arbitrations, cloned.operations, cloned.answers = {}, {}, {}, {}
    async with staging.transaction() as db:
        from sqlalchemy import update
        from researchx.storage import schema as s
        from researchx.storage.research_records import scope
        for source in cloned.sources.values():
            await staging.write_snapshot(await store.read_source(source))
        await db.execute(update(s.research_sessions).where(scope(s.research_sessions, staging.workspace_id, staging.session_id)).values(revision=cloned.revision))
        await staging._save(cloned, "seed", {})
    return staging


@pytest.mark.parametrize("status", ["timeout", "budget_exhausted", "interrupted", "failed"])
async def test_partial_investigations_import_snapshots_without_formal_conclusions(
    research, tmp_path, status
):
    store, _, _, claims = research
    cid = (await conflict(research))
    arb, baseline = (await store.begin_investigation(cid))
    staging = (await seed_staging(store, baseline, tmp_path))
    source = (await staging.capture(
        origin_id="child-tool",
        kind="web",
        content="更正说明原文",
        locator="https://new.example/report",
    ))
    ev = (await apply(staging, "add_evidence", source_id=source.id, statement="已取得更正说明"))[
        "evidence_id"
    ]
    step = (await apply(
        staging,
        "add_reasoning",
        evidence_ids=[ev],
        method="核对",
        result="已取得原文",
        output="待进一步判断",
    ))["step_id"]
    result = (await store.finish_investigation(arb.id, staging=staging, baseline=baseline, status=status))
    memory = (await store.load())
    assert len(result["imported_ids"]) == 3
    assert (
        source.id not in memory.sources
        and ev not in memory.evidence_pool
        and step not in memory.reasoning_chain
    )
    imported_source = next(
        memory.sources[key] for key in result["imported_ids"] if key in memory.sources
    )
    assert (await store.read_source(imported_source)) == "更正说明原文"
    assert set(memory.conclusions) == set(claims)
    assert memory.conflicts[cid].status == "interrupted"
    with pytest.raises(ResearchError, match="already investigated"):
        (await store.begin_investigation(cid))


async def test_investigation_import_maps_report_and_keeps_parent_inputs(research, tmp_path):
    store, evs, steps, _ = research
    cid = (await conflict(research))
    arb, baseline = (await store.begin_investigation(cid))
    staging = (await seed_staging(store, baseline, tmp_path))
    check = (await apply(
        staging,
        "add_reasoning",
        evidence_ids=[evs[1]],
        method="原文核验",
        result="核对完整原文",
        output="更正数字为10亿元",
        verification=True,
    ))["step_id"]
    checked = (await apply(
        staging,
        "verify_evidence",
        evidence_id=evs[1],
        level="source_checked",
        method="source",
        verification_step_id=check,
        verification_note="已读取完整原文",
    ))["evidence_id"]
    synthesis = (await apply(
        staging,
        "add_reasoning",
        evidence_ids=[checked],
        method="引用已核对版本",
        result="更正数字为10亿元",
        output="采信更正后的披露",
    ))["step_id"]
    report = decision(evs + [checked], steps + [check, synthesis])
    result = (await store.finish_investigation(arb.id, staging=staging, baseline=baseline, report=report))
    assert result["input_fingerprint"] == arb.input_fingerprint
    assert result["review_fingerprint"] != result["input_fingerprint"]
    imported = result["report"]["evidence_ids"][-1]
    assert imported != checked
    memory = (await store.load())
    assert memory.evidence_pool[evs[1]] == baseline.evidence_pool[evs[1]]
    assert memory.evidence_pool[imported].status == "source_checked"
    assert memory.evidence_pool[imported].supersedes is None
    (await apply(
        store, "resolve_conflict", conflict_id=cid, arbitration_id=arb.id, decision=result["report"]
    ))


async def test_resolved_conflict_reopens_on_new_version_of_accepted_evidence(research):
    store, evs, _, _ = research
    cid = (await conflict(research))
    arb_id, report = (await investigate(research, cid))
    result = (await apply(
        store,
        "resolve_conflict",
        conflict_id=cid,
        arbitration_id=arb_id,
        decision=report.model_dump(),
    ))
    original = (await store.load()).evidence_pool[evs[1]]
    (await apply(
        store,
        "add_evidence",
        source_id=original.source_id,
        statement="新的更正版本",
        supersedes=original.id,
    ))
    assert (await store.load()).conflicts[cid].status == "open"
    assert (await store.load()).conclusions[result["conclusion_id"]].needs_review


async def test_import_never_overwrites_a_corrupted_existing_snapshot(research, tmp_path):
    store, evs, _, _ = research
    cid = (await conflict(research))
    arb, baseline = (await store.begin_investigation(cid))
    staging = (await seed_staging(store, baseline, tmp_path))
    existing = baseline.sources[baseline.evidence_pool[evs[0]].source_id]
    (await staging.capture(origin_id="new-copy", kind="web", content=(await staging.read_source(existing))))
    path = await source_path(store, existing)
    path.write_text("tampered original", encoding="utf-8")
    with pytest.raises(ResearchError, match="快照校验失败"):
        (await store.finish_investigation(arb.id, staging=staging, baseline=baseline, status="timeout"))
    assert path.read_text() == "tampered original"
    assert (await store.load()).revision == baseline.revision


@pytest.mark.parametrize(
    "outcome,fields",
    [("conditional", {}), ("unresolved", {}), ("prefer_side", {"preferred_side": 0})],
)
def test_decision_requires_conditions_gaps_or_rejection_reasons(research, outcome, fields):
    _, evs, steps, _ = research
    raw = decision(evs, steps, "compatible").model_dump()
    with pytest.raises(ValueError):
        ArbitrationDecision.model_validate(raw | {"outcome": outcome, **fields})
