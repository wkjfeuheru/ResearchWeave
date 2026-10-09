import { test, expect } from '@playwright/test';

test('skill package metadata, selected entry loading and independent switches', async ({ page }) => {
  const entries: string[] = [];
  page.on('request', request => {
    if (request.method() === 'GET' && /\/api\/skills\/(analysis-modeling|report-generation)\//.test(request.url())) entries.push(request.url());
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'SkillHub', exact: true }).click();
  await expect(page.locator('.skill-card')).toHaveCount(2);
  await page.getByLabel('搜索技能').fill('industry-commentary');
  await expect(page.locator('.skill-card')).toHaveCount(1);
  await page.locator('.skill-card').getByRole('button', { name: '查看详情' }).click();
  expect(entries).toHaveLength(0);
  const section = page.locator('.skill-instructions').filter({ has: page.getByRole('heading', { name: '/industry-commentary', exact: true }) });
  await section.getByRole('button', { name: '查看技能流程' }).click();
  await expect(section.getByRole('heading', { name: '适用与禁用场景', exact: true })).toBeVisible();
  expect(entries).toHaveLength(1);
  expect(entries[0]).toContain('/report-generation/industry-commentary');
  const toggle = section.getByRole('switch', { name: '启用技能 industry-commentary', exact: true });
  await toggle.click();
  await expect(toggle).not.toBeChecked();
  await expect(section.getByRole('button', { name: '查看技能流程' })).toBeDisabled();
  await expect(page.getByRole('switch', { name: '启用技能 financial-commentary', exact: true })).toBeChecked();
  await toggle.click();
  await expect(toggle).toBeChecked();
  const packageToggle = page.getByRole('dialog').getByRole('switch', { name: '启用 报告生成 Skill 包', exact: true });
  await packageToggle.click();
  await expect(packageToggle).not.toBeChecked();
  await expect(toggle).toBeDisabled();
  await packageToggle.click();
  await expect(packageToggle).toBeChecked();
});
