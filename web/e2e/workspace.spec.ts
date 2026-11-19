import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page, request }) => {
  for (const client of [page.request, request]) {
    const csrf = await (await client.get('/api/v1/auth/csrf')).json();
    const login = await client.post('/api/v1/auth/login', {
      data: { identifier: 'browser', password: 'synthetic-test-password-2026' },
      headers: { Origin: 'http://127.0.0.1:8001', 'X-CSRF-Token': csrf.csrf_token },
    });
    expect(login.ok()).toBeTruthy();
  }
});

test('workspace renders and completes a synthetic conversation', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '让每一次判断，都有据可循。' })).toBeVisible();
  await page.getByRole('textbox', { name: '研究问题' }).fill('请解释现金流质量');
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page).toHaveURL(/\/chat\/web_/);
  await expect(page.getByText('这是合成模型的测试答复，不是真实金融研究。', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('这是合成模型的测试答复，不是真实金融研究。', { exact: true })).toBeVisible();
});

test('report shows evidence, real status and follow-up', async ({ page, request }) => {
  const workspace = await (await request.get('/api/v1/workspace')).json();
  const research = workspace.research[0];
  await page.goto(`/research/${research.research_id}`);
  await expect(page.getByRole('heading', { name: '核心发现', exact: true })).toBeVisible();
  await expect(page.getByText('结果尚未核验', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '[1]', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '证据与来源' })).toBeVisible();
  await expect(page.getByText('原始披露片段（合成测试，非真实财务数据）', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭面板' }).click();
  await page.getByRole('textbox', { name: '研究问题' }).fill('这个结论有哪些证据？');
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page).toHaveURL(/\/chat\/web_/);
});

test('mobile navigation does not overflow', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('button', { name: '展开导航' }).click();
  await page.getByRole('link', { name: '研究任务', exact: true }).click();
  await expect(page.getByRole('heading', { name: '让研究持续发生。' })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(overflow).toBe(false);
});

test('workspace and report screenshots', async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 1040 });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '继续研究' })).toBeVisible();
  await page.screenshot({ path: 'test-results/workspace.png', fullPage: true });
  const workspace = await (await request.get('/api/v1/workspace')).json();
  await page.goto(`/research/${workspace.research[0].research_id}`);
  await expect(page.getByRole('heading', { name: '核心发现', exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/report.png', fullPage: true });
});

test('mobile report directory scrolls without covering the evidence', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const workspace = await (await request.get('/api/v1/workspace')).json();
  await page.goto(`/research/${workspace.research[0].research_id}`);
  await page.getByRole('button', { name: '报告目录', exact: true }).click();
  await page.getByRole('navigation', { name: '报告目录' }).getByRole('link', { name: '来源与引用', exact: true }).click();
  const heading = page.getByRole('heading', { name: '来源与引用', exact: true });
  await expect(heading).toBeInViewport();
  const sourceBox = await heading.boundingBox();
  const composerBox = await page.getByRole('textbox', { name: '研究问题' }).boundingBox();
  expect(sourceBox!.y + sourceBox!.height).toBeLessThan(composerBox!.y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
});

test('financial document library is no longer exposed', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('link', { name: '资料库', exact: true })).toHaveCount(0);
  await page.goto('/library');
  await expect(page.getByText('页面不存在', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '导入资料', exact: true })).toHaveCount(0);
});

test('failed model requests expose safe diagnostics instead of a terminal command', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('textbox', { name: '研究问题' }).fill('[simulate-connect-error]');
  await page.getByRole('button', { name: '发送问题' }).click();
  await expect(page).toHaveURL(/\/chat\/web_/);
  await page.getByText('故障诊断', { exact: true }).click();
  await expect(page.getByText('MODEL_UNAVAILABLE', { exact: true })).toBeVisible();
  await expect(page.getByText('ConnectError', { exact: true })).toBeVisible();
  await expect(page.locator('.assistant-message')).not.toContainText('/details');
  await page.reload();
  await page.getByText('故障诊断', { exact: true }).click();
  await expect(page.getByText('MODEL_UNAVAILABLE', { exact: true })).toBeVisible();
});
