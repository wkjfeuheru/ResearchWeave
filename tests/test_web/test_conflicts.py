"""Public conflict progress, crash recovery and privacy at the Web boundary."""

from openharness.research.store import ResearchStore
from openharness.tools.research_memory_tool import ResearchMemoryTool
from openharness.web.activity import describe_tool
from tests.test_research.test_conflicts import apply
from tests.test_web.test_app import add_model, add_session


def test_web_recovers_interrupted_investigation_without_exposing_raw_records(workspace):
    client, _, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    store = ResearchStore(cwd, sid)
    user = store.capture(origin_id="user", kind="user", content="核查营收")
    apply(store, "set_context", goal="核查营收", user_source_ids=[user.id])
    apply(store, "create_plan", title="核查营收", tasks=["对比原文"])
    sides = []
    for index in range(2):
        source = store.capture(origin_id=f"original-{index}", kind="web", content=f"private snapshot {index}",
                               locator=f"https://source{index}.example/report")
        ev = apply(store, "add_evidence", source_id=source.id, statement=f"营收{index}")["evidence_id"]
        sides.append({"statement": f"营收{index}", "evidence_ids": [ev]})
    cid = apply(store, "add_conflict", question="两个营收数字冲突", kind="fact", sides=sides)["conflict_id"]
    arb, _ = store.begin_investigation(cid)
    response = client.get(f"/api/sessions/{sid}").json()
    assert response["research_progress"]["conflicts"] == [
        {"id": cid, "question": "两个营收数字冲突", "status": "interrupted", "core": True}]
    assert store.load().arbitrations[arb.id].status == "interrupted"
    assert "private snapshot" not in str(response) and "assessments" not in str(response)
    revision = store.load().revision
    client.get(f"/api/sessions/{sid}")
    assert store.load().revision == revision


def test_conflict_activity_labels_are_deliberate_summaries():
    assert describe_tool("investigate_conflict", {"conflict_id": "private"}) == {
        "category": "other", "label": "核查冲突原文", "target": ""}
    assert describe_tool("research_memory", {"operation": {"action": "resolve_conflict", "decision": {"rationale": "private"}}})["label"] == "提交争议裁决"
    actions = ResearchMemoryTool().input_model.model_json_schema()["properties"]["operation"]["discriminator"]["mapping"]
    assert {"add_conflict", "resolve_conflict", "reopen_conflict"} <= actions.keys()
