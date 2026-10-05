import { expect, test } from '@playwright/test';

test('model configuration, skills, streaming, approvals, stop and recovery', async ({ page }) => {
  const browserErrors: string[] = [];
  page.on('pageerror', error => browserErrors.push(error.message));
  await page.goto('/');
  await expect(page.getByText('让研究，从一个好问题开始')).toBeVisible();
  await page.getByRole('button', { name: '模型配置', exact: true }).click();
  await page.getByRole('button', { name: '添加模型', exact: true }).click();
  await page.getByLabel('配置名称').fill('浏览器测试模型');
  await page.getByLabel('模型名称').fill('browser-test-model');
  await page.getByLabel(/^API Key/).fill('browser-test-secret');
  await page.getByRole('button', { name: '保存配置' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const card = page.locator('.model-card').filter({ has: page.getByRole('heading', { name: '浏览器测试模型', exact: true }) });
  await card.getByRole('button', { name: '测试连接' }).click();
  await expect(card.getByText('连接成功，模型可正常响应')).toBeVisible();
  await card.getByRole('button', { name: '设为默认' }).click();
  await expect(card.getByText('默认', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'SkillHub', exact: true }).click();
  await expect(page.getByRole('switch', { name: '启用 财报穿透解析', exact: true })).toBeChecked();
  await page.getByRole('switch', { name: '启用 财报穿透解析', exact: true }).click();
  await expect(page.getByRole('switch', { name: '启用 财报穿透解析', exact: true })).not.toBeChecked();
  await page.getByRole('switch', { name: '启用 财报穿透解析', exact: true }).click();
  await expect(page.getByRole('switch', { name: '启用 财报穿透解析', exact: true })).toBeChecked();
  await page.getByLabel('搜索技能').fill('财报穿透解析');
  await expect(page.locator('.skill-card')).toHaveCount(1);
  await page.locator('.skill-card').getByRole('button', { name: '查看详情' }).click();
  await page.getByRole('button', { name: '在对话中试用' }).click();
  await expect(page.getByLabel('对话输入')).toHaveValue('/financial-statement-analysis 贵州茅台 2025年年报');
  await page.getByLabel('当前对话模型').selectOption({ label: '浏览器测试模型 · browser-test-model' });
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant')).toContainText('这是浏览器测试回复');
  await expect(page.locator('.message.assistant table')).toBeVisible();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(page.getByLabel('会话用量')).toContainText('输入 10');
  await expect(page.getByLabel('会话用量')).toContainText('命中率 0.0%');
  await expect(page.getByLabel('会话用量')).toContainText('缓存写入 未提供');
  await expect(page.locator('.message-list')).not.toContainText('runtime_context');
  await page.getByLabel('对话输入').fill('请写入测试文件');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  const approval = page.getByRole('dialog', { name: '操作确认' });
  await expect(approval.locator('.tag')).toHaveText('写入文件');
  await expect(approval).toContainText('当前权限模式要求你确认可能修改文件或数据的操作');
  await expect(approval).not.toContainText('write_file');
  await expect(approval).not.toContainText('/permissions');
  await expect(approval.getByRole('button', { name: '允许此次操作' })).toBeVisible();
  await approval.getByRole('button', { name: '取消', exact: true }).click();
  await expect(approval).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByLabel('对话输入').fill('请提问');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('你希望研究哪个时间范围');
  await page.getByLabel('补充信息').fill('最近一年');
  await page.getByRole('button', { name: '回复并继续' }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByLabel('对话输入').fill('请慢慢回复');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('这是部分回复');
  await page.getByRole('button', { name: '停止生成' }).click();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  const usage = await page.getByLabel('会话用量').textContent();
  const count = await page.locator('.message').count();
  await page.reload();
  await expect(page.locator('.message')).toHaveCount(count);
  await expect(page.getByLabel('会话用量')).toHaveText(usage || '');
  await page.locator('.message.assistant').last().getByRole('button', { name: '执行过程 · 已停止' }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('这是部分回复');
  await expect(page.getByRole('button', { name: '复制回复' }).first()).toBeVisible();
  await expect(page.locator('body')).not.toContainText('browser-test-secret');
  await page.screenshot({ path: 'test-results/research-chat.png', fullPage: true });
  await page.getByLabel('对话输入').fill('请显示测试标记');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('安全内容');
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(page.locator('.message.assistant script, .message.assistant img')).toHaveCount(0);
  expect(await page.evaluate(() => '__unsafe' in window)).toBe(false);
  await expect(page.getByRole('link', { name: '危险链接' })).not.toHaveAttribute('href', /^javascript:/);
  expect(browserErrors).toEqual([]);
});

test('responsive navigation and empty skill search', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: '打开导航' }).click();
  await page.getByRole('button', { name: 'SkillHub', exact: true }).click();
  await page.getByLabel('搜索技能').fill('不存在的技能');
  await expect(page.getByText('没有找到相关技能')).toBeVisible();
  await expect(page.locator('.sidebar')).not.toHaveClass(/open/);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
  await page.screenshot({ path: 'test-results/research-mobile.png', fullPage: true });
});

test('research progress, source footnotes and interrupt to replan', async ({ page }) => {
  const created = await page.request.post('/api/models', { data: {
    label: '研究记忆测试模型', api_format: 'openai', model: 'memory-test', api_key: 'memory-test-secret',
  } });
  expect(created.ok()).toBe(true);
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('开展测试研究');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByLabel('研究任务进度')).toContainText('2/2 项任务完成');
  await expect(page.getByLabel('研究任务进度')).toContainText('收集年报');
  await expect(page.locator('.message.assistant').last()).toContainText('来源：');
  await expect(page.locator('.message.assistant').last()).toContainText('report.txt');
  await expect(page.locator('.message.assistant').last()).toContainText('待核验');
  await expect(page.locator('.tool-row')).toHaveCount(0);
  await expect(page.locator('.message-list')).not.toContainText('research_memory');
  await page.reload();
  await expect(page.getByLabel('研究任务进度')).toContainText('2/2 项任务完成');
  await page.getByLabel('对话输入').fill('慢速研究公司 A');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('旧研究执行中');
  await expect(page.getByLabel('对话输入')).toBeEnabled();
  await page.getByLabel('对话输入').fill('改为研究公司 B');
  await page.getByRole('button', { name: '打断并修改', exact: true }).click();
  await expect(page.getByLabel('研究任务进度')).toContainText('公司 B 简要研究');
  await expect(page.getByLabel('研究任务进度')).toContainText('2/2 项任务完成');
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(page.locator('.message.user').filter({ hasText: '改为研究公司 B' })).toHaveCount(1);
  await page.reload();
  await expect(page.getByLabel('研究任务进度')).toContainText('公司 B 简要研究');
  await expect(page.locator('.message.user').filter({ hasText: '改为研究公司 B' })).toHaveCount(1);
  await expect(page.locator('.message-list')).not.toContainText('method');
  await page.screenshot({ path: 'test-results/research-memory.png', fullPage: true });
});

async function prepareProcessChat(page: import('@playwright/test').Page) {
  const created = await page.request.post('/api/models', { data: {
    label: '执行过程测试模型', api_format: 'openai', model: 'process-test', api_key: 'process-test-secret',
  } });
  expect(created.ok()).toBe(true);
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('查看执行过程');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
}

test('two levels of process disclosure, final answer and refresh recovery', async ({ page }) => {
  await prepareProcessChat(page);
  const turn = page.locator('.message.assistant').last();
  const heading = turn.locator('.process-heading');
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await expect(turn.locator('.process-content')).toContainText('我先读取两份资料，再整理结论');
  await expect(turn.locator('.activity-summary')).toContainText('读取 2 个文件');
  await expect(turn.locator('.activity-details')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(heading).toHaveText('执行过程 · 已完成');
  await expect(heading).toHaveAttribute('aria-expanded', 'false');
  await expect(turn.locator('.answer-content')).toHaveCount(1);
  await expect(turn.locator('.answer-content')).toContainText('已整理资料；第二份文件不可用');
  await heading.focus();
  await page.keyboard.press('Enter');
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await expect(turn.locator('.activity-summary')).toContainText('1 项失败');
  await turn.locator('.activity-summary').click();
  await expect(turn.locator('.activity-details')).toContainText('report.txt');
  await expect(turn.locator('.activity-details')).toContainText('missing.txt');
  await expect(turn.locator('.activity-status.completed')).toHaveCount(1);
  await expect(turn.locator('.activity-status.failed')).toHaveCount(1);
  await expect(turn.locator('.process-content .message-avatar, .process-content .copy-button')).toHaveCount(0);
  await expect(turn.locator('.process-content')).not.toContainText('测试资料：营业收入同比增长');
  await page.screenshot({ path: 'test-results/conversation-process.png', fullPage: true });
  await page.reload();
  await expect(heading).toHaveAttribute('aria-expanded', 'false');
  await heading.click();
  await turn.locator('.activity-summary').click();
  await expect(turn.locator('.activity-details')).toContainText('missing.txt');
  await expect(turn.locator('.answer-content')).toHaveCount(1);
});

test('manual process disclosure survives completion and stopping preserves partial progress', async ({ page }) => {
  await prepareProcessChat(page);
  const heading = page.locator('.process-heading').last();
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await heading.click();
  await heading.click();
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await page.getByLabel('对话输入').fill('请慢慢回复');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.message.assistant').last()).toContainText('这是部分回复');
  await page.getByRole('button', { name: '停止生成' }).click();
  const lastTurn = page.locator('.message.assistant').last();
  await expect(lastTurn.locator('.process-heading')).toHaveText('执行过程 · 已停止');
  await expect(lastTurn.locator('.process-heading')).toHaveAttribute('aria-expanded', 'false');
  await lastTurn.locator('.process-heading').click();
  await expect(lastTurn.locator('.process-content')).toContainText('这是部分回复');
  await expect(lastTurn.locator('.answer-content')).toHaveCount(0);
});

test('delete confirmation, failure retry, noncurrent and current session cleanup', async ({ page }) => {
  await prepareProcessChat(page);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  const currentId = await page.evaluate(() => sessionStorage.getItem('openharness.web.session'));
  const session = await (await page.request.get(`/api/sessions/${currentId}`)).json();
  const other = await (await page.request.post('/api/sessions', { data: { profile_id: session.profile_id } })).json();
  await page.reload();
  const otherRow = page.locator('.session-row').filter({ has: page.getByRole('button', { name: '新对话', exact: true }) }).last();
  await otherRow.hover();
  await otherRow.getByRole('button', { name: '删除对话：新对话', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '删除对话', exact: true });
  await expect(dialog).toContainText('无法恢复');
  await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeFocused();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  expect((await page.request.get(`/api/sessions/${other.session_id}`)).ok()).toBe(true);
  await otherRow.getByRole('button', { name: '删除对话：新对话', exact: true }).click();
  await page.route(`**/api/sessions/${other.session_id}`, async route => {
    if (route.request().method() === 'DELETE') await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '删除失败，请重试' }) });
    else await route.continue();
  });
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('删除失败');
  await expect(otherRow).toHaveCount(1);
  await page.unroute(`**/api/sessions/${other.session_id}`);
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect((await page.request.get(`/api/sessions/${other.session_id}`)).status()).toBe(404);
  expect(await page.evaluate(() => sessionStorage.getItem('openharness.web.session'))).toBe(currentId);
  await page.getByLabel('对话输入').fill('未发送草稿');
  await page.locator('.session-row.selected').hover();
  await page.locator('.session-row.selected .session-delete').click();
  await dialog.getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByText('让研究，从一个好问题开始')).toBeVisible();
  await expect(page.getByLabel('对话输入')).toHaveValue('');
  expect(await page.evaluate(() => sessionStorage.getItem('openharness.web.session'))).toBe(null);
  expect((await page.request.get(`/api/sessions/${currentId}`)).status()).toBe(404);
  await expect(page.getByText('连接已断开', { exact: false })).toHaveCount(0);
});

test('mobile deletion remains visible and returns to welcome', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await prepareProcessChat(page);
  await expect(page.getByRole('button', { name: '停止生成' })).toHaveCount(0);
  await page.getByRole('button', { name: '打开导航' }).click();
  const remove = page.locator('.session-row.selected .session-delete');
  await expect(remove).toBeVisible();
  await expect(remove).toHaveCSS('opacity', '1');
  await remove.click();
  await page.getByRole('dialog').getByRole('button', { name: '确认删除', exact: true }).click();
  await expect(page.getByText('让研究，从一个好问题开始')).toBeVisible();
  await expect(page.locator('.sidebar')).not.toHaveClass(/open/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
});

test('disconnect marks pending activity interrupted and restores the saved process', async ({ page }) => {
  await page.addInitScript(() => {
    const NativeSocket = window.WebSocket;
    window.WebSocket = class extends NativeSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        (window as Window & { testSocket?: WebSocket }).testSocket = this;
      }
    };
  });
  const created = await page.request.post('/api/models', { data: {
    label: '断线测试模型', api_format: 'openai', model: 'disconnect-test', api_key: 'disconnect-test-secret',
  } });
  const { id } = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('对话输入').fill('请提问');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.evaluate(() => (window as Window & { testSocket?: WebSocket }).testSocket!.close(4000, '测试断线'));
  await expect(page.getByRole('dialog')).toHaveCount(0);
  const heading = page.locator('.process-heading');
  await expect(heading).toHaveText('执行过程 · 已停止');
  await expect(heading).toHaveAttribute('aria-expanded', 'false');
  await heading.click();
  await page.locator('.activity-summary').click();
  await expect(page.locator('.activity-status.interrupted')).toHaveText('已中断');
  const sid = await page.evaluate(() => sessionStorage.getItem('openharness.web.session'));
  await expect.poll(async () => {
    const session = await (await page.request.get(`/api/sessions/${sid}`)).json();
    return session.messages.find((row: { role: string }) => row.role === 'activity')?.status;
  }).toBe('interrupted');
  await page.reload();
  await expect(heading).toHaveText('执行过程 · 已停止');
  await expect(page.getByText('连接已断开', { exact: false })).toHaveCount(0);
});

test('new stream content follows the bottom without interrupting reading above', async ({ page }) => {
  const created = await page.request.post('/api/models', { data: {
    label: '滚动测试模型', api_format: 'openai', model: 'scroll-test', api_key: 'scroll-test-secret',
  } });
  const { id } = await created.json();
  const session = await (await page.request.post('/api/sessions', { data: { profile_id: id } })).json();
  let routed: import('@playwright/test').WebSocketRoute | undefined;
  let requestId = '';
  await page.routeWebSocket(`**/api/sessions/${session.session_id}/ws`, route => {
    routed = route;
    route.send(JSON.stringify({ type: 'ready', session_id: session.session_id, session }));
    route.onMessage(raw => {
      const request = JSON.parse(String(raw));
      if (request.type === 'submit') {
        requestId = request.request_id;
        route.send(JSON.stringify({ type: 'started', session_id: session.session_id, request_id: requestId, profile_id: id, model: 'scroll-test' }));
      }
    });
  });
  await page.addInitScript(sid => sessionStorage.setItem('openharness.web.session', sid), session.session_id);
  await page.goto('/');
  await expect(page.getByLabel('当前对话模型')).toHaveValue(id);
  await page.getByLabel('对话输入').fill('长篇研究');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect.poll(() => requestId).not.toBe('');
  const send = (text: string) => routed!.send(JSON.stringify({ type: 'delta', session_id: session.session_id,
    request_id: requestId, turn_id: requestId, id: 'long-reply', text }));
  send('研究资料段落。\n\n'.repeat(100));
  const scroll = page.locator('.conversation-scroll');
  await expect.poll(() => scroll.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(10);
  await scroll.evaluate(element => { element.scrollTop = 0; });
  await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(0);
  send('新增资料。\n\n'.repeat(10));
  await expect(page.locator('.answer-content')).toContainText('新增资料');
  await expect.poll(() => scroll.evaluate(element => element.scrollTop)).toBe(0);
  await scroll.evaluate(element => { element.scrollTop = element.scrollHeight; });
  await expect.poll(() => scroll.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(10);
  send('继续补充。\n\n'.repeat(10));
  await expect(page.locator('.answer-content')).toContainText('继续补充');
  await expect.poll(() => scroll.evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(10);
});


test('attachments, real script artifacts, download, reload and isolation', async ({page}) => {
  const created = await page.request.post('/api/models', { data: {
    label: '附件验收模型', api_format: 'openai', model: 'file-test-model', api_key: 'file-test-secret',
  }});
  const {id} = await created.json();
  await page.goto('/');
  await page.getByLabel('当前对话模型').selectOption(id);
  await page.getByLabel('上传研究资料').setInputFiles({name:'sample.txt', mimeType:'text/plain', buffer:Buffer.from('Synthetic reference input')});
  await expect(page.getByLabel('会话附件')).toContainText('sample.txt');
  await expect(page.getByLabel('会话附件').getByRole('checkbox')).toBeChecked();
  await page.getByLabel('对话输入').fill('导出固定验收产物');
  await page.getByRole('button', {name:'发送消息', exact:true}).click();
  await page.getByRole('dialog', {name:'操作确认'}).getByRole('button', {name:'允许此次操作'}).click();
  await expect(page.getByRole('button', {name:'停止生成'})).toHaveCount(0);
  await expect(page.getByLabel('研究产物').getByRole('link')).toHaveCount(4);
  const response = await page.request.get(await page.getByLabel('研究产物').getByRole('link').filter({hasText:'digest.json'}).getAttribute('href') || '');
  expect(response.ok()).toBe(true);
  expect((await response.json()).reports[0].predictions[0].year).toBe(2026);
  await page.reload();
  await expect(page.getByLabel('研究产物').getByRole('link')).toHaveCount(4);
  await expect(page.getByLabel('会话附件')).toContainText('sample.txt');
  await page.getByRole('button', {name:'删除附件 sample.txt'}).click();
  await expect(page.getByLabel('会话附件')).toHaveCount(0);
  await page.getByRole('button', {name:'新建对话', exact:true}).click();
  await expect(page.getByLabel('研究产物')).toHaveCount(0);
  await expect(page.getByLabel('会话附件')).toHaveCount(0);
});
