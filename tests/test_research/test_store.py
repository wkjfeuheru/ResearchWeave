"""Research records must survive restarts without inventing provenance or sharing sessions."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from openharness.research.models import new_id
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.research.store import ResearchError, ResearchStore
from openharness.services.token_estimation import estimate_tokens


@pytest.fixture
def store(tmp_path):
    return ResearchStore(tmp_path, "a" * 12, root=tmp_path / "memory")


def apply(store, action, **fields):
    return store.apply({"action": action, "operation_id": new_id("op"),
                        "expected_revision": store.load().revision, **fields})


def plan(store, goal="公司 A"):
    source = store.capture(origin_id=new_id("msg"), kind="user", content=goal)
    apply(store, "set_context", goal=goal, user_source_ids=[source.id], constraints=["数据截止至 2026-09-30"])
    return apply(store, "create_plan", title=goal, tasks=["收集资料", "核验与分析"])


def evidence(store, text="营收增长", kind="web", is_error=False):
    source = store.capture(origin_id=new_id("tool"), content=text, kind=kind, title="年报",
                           locator="https://example.org/report", is_error=is_error)
    result = apply(store, "add_evidence", source_id=source.id, statement=text)
    return result["evidence_id"]


def reasoning(store, ids, **fields):
    return apply(store, "add_reasoning", evidence_ids=ids, method="比较同口径财务数据",
                 result="同比增长", output="增长趋势", **fields)["step_id"]


def test_sessions_are_isolated_and_restart_preserves_snapshot(store, tmp_path):
    plan(store)
    ev = evidence(store)
    loaded = ResearchStore(tmp_path, store.session_id, root=tmp_path / "memory")
    assert ev in loaded.load().evidence_pool
    other = ResearchStore(tmp_path, "b" * 12, root=tmp_path / "memory")
    assert not other.load().evidence_pool
    with pytest.raises(ResearchError, match="Unknown"):
        other.apply({"action": "read", "ids": [ev]})
    source = loaded.load().sources[loaded.load().evidence_pool[ev].source_id]
    assert loaded.read_source(source) == "营收增长"
    assert source.published_at is None
    assert source.collected_at.endswith("Z")


def test_write_retry_is_idempotent_and_conflicts_do_not_overwrite(store):
    user = store.capture(origin_id="user", content="公司 A", kind="user")
    command = {"action": "set_context", "operation_id": "same", "expected_revision": 1,
               "goal": "公司 A", "user_source_ids": [user.id]}
    first = store.apply(command)
    assert store.apply(command)["context_id"] == first["context_id"]
    assert store.load().revision == 2
    with pytest.raises(ResearchError, match="already used"):
        store.apply(command | {"goal": "公司 B"})
    with pytest.raises(ResearchError, match="Revision conflict"):
        store.apply(command | {"operation_id": "new"})
    assert store.load().revision == 2


def test_concurrent_captures_keep_all_sources(store):
    def capture(index):
        store.capture(origin_id=f"tool-{index}", content=f"report {index}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(capture, range(12)))
    assert len(store.load().sources) == 12
    assert store.load().revision == 12


def test_failed_tool_cannot_become_evidence(store):
    source = store.capture(origin_id="failed", content="timeout", is_error=True)
    with pytest.raises(ResearchError, match="Failed tool"):
        apply(store, "add_evidence", source_id=source.id, statement="收益增长")
    assert not store.load().evidence_pool


def test_verified_claim_requires_verification_and_verified_evidence(store):
    ev = evidence(store, kind="search")
    step = reasoning(store, [ev])
    with pytest.raises(ResearchError, match="verified evidence"):
        apply(store, "add_conclusion", statement="增长已核验", status="verified", evidence_ids=[ev], step_ids=[step])
    with pytest.raises(ResearchError, match="explicit verification"):
        source_id = store.load().evidence_pool[ev].source_id
        apply(store, "add_evidence", source_id=source_id, statement="增长已核验", status="verified")
    check = reasoning(store, [ev], verification=True)
    verified = apply(store, "add_evidence", source_id=store.load().evidence_pool[ev].source_id,
                     statement="已核对原文的营收增长", supersedes=ev, status="verified",
                     verification_step_id=check, verification_note="已比对原文及同口径表格")["evidence_id"]
    final = reasoning(store, [verified], verification=True)
    claim = apply(store, "add_conclusion", statement="增长已核验", status="verified", evidence_ids=[verified], step_ids=[final])
    assert store.load().conclusions[claim["conclusion_id"]].status == "verified"


def test_retraction_invalidates_transitive_calculations_and_retains_old_versions(store):
    ev = evidence(store)
    calculation = reasoning(store, [ev])
    source = store.capture(origin_id="calc", kind="calculation", content="10 / 5 = 2")
    derived = apply(store, "add_evidence", source_id=source.id, statement="倍数为 2",
                    input_evidence_ids=[ev], calculation_step_id=calculation)["evidence_id"]
    step = reasoning(store, [derived])
    claim = apply(store, "add_conclusion", statement="规模翻倍", evidence_ids=[derived], step_ids=[step])["conclusion_id"]
    apply(store, "add_evidence", source_id=store.load().evidence_pool[ev].source_id,
          statement="数据口径有误，撤回", status="retracted", supersedes=ev)
    memory = store.load()
    assert memory.conclusions[claim].needs_review
    assert ev in memory.evidence_pool
    assert len(memory.evidence_pool) == 3


def test_calculation_needs_traced_inputs(store):
    source = store.capture(origin_id="calc", kind="calculation", content="2")
    with pytest.raises(ResearchError, match="Calculation evidence needs"):
        apply(store, "add_evidence", source_id=source.id, statement="倍数为 2")


def test_archived_evidence_needs_explicit_reuse(store):
    first = plan(store)
    ev = evidence(store)
    second = plan(store, "公司 B")
    assert store.load().plans[first["plan_id"]].archived
    assert not store.view(store.load())["evidence_pool"]
    with pytest.raises(ResearchError, match="Explicitly reuse"):
        reasoning(store, [ev])
    with pytest.raises(ResearchError, match="Explicitly reuse"):
        apply(store, "add_evidence", source_id=store.load().evidence_pool[ev].source_id, statement="复制旧证据")
    apply(store, "create_plan", title="对比 A 与 B", tasks=["比较"], reused_evidence_ids=[ev])
    step = reasoning(store, [ev])
    assert step in store.load().reasoning_chain
    assert store.load().plans[second["plan_id"]].archived


def test_completed_tasks_do_not_verify_conclusions(store):
    current = plan(store)
    ev = evidence(store)
    step = reasoning(store, [ev])
    claim = apply(store, "add_conclusion", statement="可能增长", evidence_ids=[ev], step_ids=[step])["conclusion_id"]
    for task in current["tasks"]:
        apply(store, "update_task", task_id=task["id"], status="completed")
    assert store.progress()["completed"] == 2
    assert store.load().conclusions[claim].status == "tentative"


def test_steer_persists_before_replan_and_old_task_cannot_advance(store):
    first = plan(store)
    store.interrupt(request_id="next", target_request_id="old", text="改研究 B")
    assert store.load().pending_steers["next"]["text"] == "改研究 B"
    assert store.load().research_state.current_plan_id == first["plan_id"]
    store.require_replan("next")
    assert store.progress()["replan_required"]
    with pytest.raises(ResearchError, match="Create a current plan"):
        apply(store, "update_task", task_id=first["tasks"][0]["id"], status="completed")
    plan(store, "B")
    assert not store.progress()["replan_required"]


def test_citations_are_program_rendered_and_frozen_per_answer(store):
    ev = evidence(store)
    original, answer = store.render_answer(f"增长 [E:{ev}]。未知 [E:invented]", "answer1")
    assert "[1]" in original and "https://example.org/report" in original
    assert "待核验" in original and "发布日期未知" in original
    assert "来源不可核验" in original
    apply(store, "add_evidence", source_id=store.load().evidence_pool[ev].source_id,
          statement="撤回", status="retracted", supersedes=ev)
    assert store.render_answer("different", "answer1")[0] == original
    assert answer["citations"][ev]["evidence"]["status"] == "pending"
    assert "来源：" not in store.render_answer("你好", "hello")[0]


def test_display_numbers_never_become_model_replay_or_new_citations(store):
    ev = evidence(store)
    model_text = f"营收增长 [E:{ev}]。"
    rendered, frozen = store.render_answer(model_text, "canonical")
    message = ConversationMessage(role="assistant", content=[TextBlock(text=rendered)], research_citations=frozen)
    assert message.api_content()[0].text == model_text
    assert message.text == rendered and "来源：" in message.text
    # Old persisted answers are migrated using their own frozen numbering.
    legacy = dict(frozen)
    legacy.pop("model_text")
    old = message.model_copy(update={"research_citations": legacy})
    assert old.api_content()[0].text == model_text
    unrelated, answer = store.render_answer("复用编号 [1] 是没有依据的。", "numeric")
    assert "来源不可核验" in unrelated and not answer["citations"]
    assert answer["invalid"] == ["[1]"]


def test_prompt_uses_whole_records_and_enforces_mandatory_budget(store):
    plan(store)
    ev = evidence(store, text="财务资料" * 6000)
    prompt = store.prompt(800)
    payload = json.loads(prompt.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert estimate_tokens(prompt) <= 800
    assert payload["task_context"]["goal"] == "公司 A"
    assert not payload["evidence_pool"]  # Large records are omitted, never sliced.
    assert store.apply({"action": "read", "ids": [ev]})["records"][0]["statement"] == "财务资料" * 6000
    apply(store, "set_context", goal="研究范围" * 3000, user_source_ids=[next(iter(store.load().sources))])
    with pytest.raises(ResearchError, match="超过注入预算"):
        store.prompt(800)


def test_corruption_is_reported_without_reset(store):
    source = store.capture(origin_id="source", content="original")
    state = store.path.read_bytes()
    (store.directory / source.snapshot).unlink()
    with pytest.raises(ResearchError, match="损坏"):
        store.load()
    assert store.path.read_bytes() == state


def test_write_failure_does_not_publish_partial_state(store, monkeypatch):
    plan(store)
    previous = store.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr("openharness.research.store.atomic_write_text", fail)
    with pytest.raises(OSError, match="disk full"):
        apply(store, "create_plan", title="replacement", tasks=["新任务"])
    assert store.path.read_bytes() == previous


def test_reasoning_cycle_in_corrupted_state_is_rejected(store):
    ev = evidence(store)
    step = reasoning(store, [ev])
    data = json.loads(store.path.read_text())
    data["reasoning_chain"][step]["prior_step_ids"] = [step]
    store.path.write_text(json.dumps(data))
    with pytest.raises(ResearchError, match="损坏"):
        store.load()


def test_changed_files_get_new_snapshots_and_repeated_origins_cannot_change(store):
    first = store.capture(origin_id="read-1", kind="file", locator="/tmp/report.txt:1-2", content="2025: 100")
    repeated = store.capture(origin_id="read-1", kind="file", locator="/tmp/report.txt:1-2", content="2025: 100")
    assert repeated.id == first.id and store.load().revision == 1
    with pytest.raises(ResearchError, match="different content"):
        store.capture(origin_id="read-1", content="2025: 120")
    second = store.capture(origin_id="read-2", kind="file", locator=first.locator, content="2025: 120")
    assert second.content_hash != first.content_hash
    assert store.read_source(first) == "2025: 100"
    assert store.read_source(second) == "2025: 120"
    (store.directory / first.snapshot).write_text("modified outside the store")
    with pytest.raises(ResearchError, match="快照校验失败"):
        store.capture(origin_id="read-3", content="2025: 100")
    with pytest.raises(ResearchError, match="快照校验失败"):
        store.read_source(first)


def test_new_plan_does_not_inject_archived_user_documents(store):
    plan(store)
    document = store.capture(origin_id="old-document", kind="user", content="A 的旧资料")
    plan(store, "公司 B")
    assert document.id not in {source["id"] for source in store.view(store.load())["sources"]}


def test_revision_history_cycle_and_missing_plan_are_corruption(store):
    current = plan(store)
    ev = evidence(store)
    data = json.loads(store.path.read_text())
    data["plans"][current["plan_id"]]["supersedes"] = current["plan_id"]
    store.path.write_text(json.dumps(data))
    with pytest.raises(ResearchError, match="损坏"):
        store.load()
    data["plans"][current["plan_id"]]["supersedes"] = None
    data["evidence_pool"][ev]["plan_id"] = "plan_missing"
    store.path.write_text(json.dumps(data))
    with pytest.raises(ResearchError, match="损坏"):
        store.load()
