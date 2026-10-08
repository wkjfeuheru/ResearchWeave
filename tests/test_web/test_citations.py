"""Web-only citation projection must not change frozen research evidence."""

import copy
from types import SimpleNamespace

import pytest

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock
from openharness.engine.stream_events import AssistantTurnComplete
from openharness.research.store import ResearchStore
from openharness.web.citations import project_answer_rows, render_web_answer
from openharness.web.runtime import BrowserConnection, session_view
from tests.test_research.test_store import apply, evidence, store as store
from tests.test_web.test_app import add_model, add_session


def mixed_answer(store):
    keys = {kind: evidence(store, kind=kind) for kind in ("mcp", "web", "search", "tool", "file", "user")}
    text = "；".join(f"{kind}[E:{key}]" for kind, key in keys.items())
    text += f"；重复[E:{keys['web']}]；未知[E:missing]"
    return keys, store.render_answer(text, "answer-mixed")[1]


def test_only_web_sources_are_numbered_and_each_is_a_list_item(store):
    keys, answer = mixed_answer(store)
    before = copy.deepcopy(answer)
    result = render_web_answer(answer)
    assert result.startswith("mcp；web[1]；search[2]；tool；file；user；重复[1]；未知[来源不可核验]")
    assert "\n\n来源：\n\n1. " in result and "\n2. " in result
    assert "\n3." not in result and "搜索摘要" in result and "发布日期未知" in result
    assert answer == before and len(answer["citations"]) == 6
    apply(store, "add_evidence", source_id=store.load().evidence_pool[keys['web']].source_id,
          statement="更正", status="retracted", supersedes=keys['web'])
    assert render_web_answer(store.load().answers['answer-mixed']) == result
    message = ConversationMessage(role="assistant", content=[TextBlock(text=answer['rendered'])], research_citations=answer)
    assert message.api_content()[0].text == answer['model_text']


def test_legacy_projection_and_unreliable_associations(store):
    _, answer = mixed_answer(store)
    legacy = copy.deepcopy(answer)
    legacy.pop('model_text')
    legacy.pop('answer_id')
    assert render_web_answer(legacy) == render_web_answer(answer)
    row = {"id": "old", "role": "assistant", "text": answer['rendered']}
    projected = project_answer_rows([row], {"old-answer": legacy})[0]
    assert projected['answer_id'] == 'old-answer' and projected['text'] == render_web_answer(answer)
    assert row['text'] == answer['rendered']
    assert project_answer_rows([row], {"a": legacy, "b": legacy}) == [row]
    assert project_answer_rows([dict(row, answer_id="missing")], {"a": legacy})[0]['text'] == row['text']


@pytest.mark.parametrize('kind,locator', [('mcp', 'https://example.org'), ('tool', 'tool:bash'), ('web', 'file:report'), ('search', 'javascript:alert(1)'), ('web', 'https://bad host/report'), ('web', 'https://example.org:bad/report')])
def test_no_website_sources_means_no_empty_footer(store, kind, locator):
    source = store.capture(origin_id='source', content='data', kind=kind, locator=locator)
    key = apply(store, 'add_evidence', source_id=source.id, statement='data')['evidence_id']
    _, answer = store.render_answer(f'结论[E:{key}]。', 'non-web')
    assert render_web_answer(answer) == '结论。'


@pytest.mark.asyncio
async def test_live_history_and_compacted_history_share_frozen_projection(workspace):
    client, app, _, _, cwd = workspace
    sid = add_session(client, add_model(client))
    research = ResearchStore(cwd, sid)
    _, answer = mixed_answer(research)
    events = []

    async def send_json(event):
        events.append(event)

    connection = BrowserConnection(SimpleNamespace(send_json=send_json), sid, app.state.workspace)
    connection.rows = []
    connection.request_id = 'request'
    message = ConversationMessage(role='assistant', content=[TextBlock(text=answer['rendered'])], research_citations=answer)
    await connection.event(AssistantTurnComplete(message, UsageSnapshot()))
    live = next(event['message'] for event in events if event['type'] == 'message')
    assert live['text'] == render_web_answer(answer) and live['answer_id'] == 'answer-mixed'
    record = app.state.workspace.record(sid)
    record['messages'] = []  # Model compaction must not remove the historical citation source.
    record['display_messages'] = connection.rows
    app.state.workspace.store.write(record)
    assert client.get(f'/api/sessions/{sid}').json()['messages'][0]['text'] == live['text']
    record['display_messages'] = [{'id': 'legacy', 'role': 'assistant', 'text': answer['rendered']}]
    app.state.workspace.store.write(record)
    assert client.get(f'/api/sessions/{sid}').json()['messages'][0]['text'] == live['text']
    record.pop('display_messages')
    record['messages'] = [message.model_dump(mode='json')]
    assert session_view(record)['messages'][0]['text'] == live['text']
