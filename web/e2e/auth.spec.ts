import { expect, test, type Page } from '@playwright/test';

async function signIn(page: Page, identifier = 'browser') {
  await page.goto('/login');
  await page.getByLabel('邮箱或用户名', { exact: true }).fill(identifier);
  await page.getByLabel('密码', { exact: true }).fill('synthetic-test-password-2026');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('button', { name: '退出登录' })).toBeVisible();
}

test('login design, safe public routes and mobile layout', async ({ page }) => {
  await page.goto('/library');
  await expect(page).toHaveURL(/\/login\?/);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '让每一次分析，都有据可循' })).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({ path: 'test-results/login-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 360, height: 800 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await page.screenshot({ path: 'test-results/login-mobile.png', fullPage: true });
  await page.getByRole('link', { name: '忘记密码？' }).click();
  await expect(page.getByRole('heading', { name: '重置密码' })).toBeVisible();
  await expect(page.getByText('邮件已发送')).toHaveCount(0);
});

test('invalid login, password visibility, remembered login and logout', async ({ page }) => {
  await page.goto('/login?return_to=%2f%2fevil.example');
  await page.getByLabel('邮箱或用户名', { exact: true }).fill('browser');
  await page.getByLabel('密码', { exact: true }).fill('incorrect password');
  await page.getByRole('button', { name: '显示密码' }).click();
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('type', 'text');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveText('账号或密码错误');
  await page.getByLabel('密码', { exact: true }).fill('synthetic-test-password-2026');
  await page.getByLabel('记住我').check();
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL('/');
  await page.reload();
  await expect(page.getByRole('button', { name: '退出登录' })).toBeVisible();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  expect((await page.request.get('/api/v1/workspace')).status()).toBe(401);
});

test('different browser accounts cannot read another session or report', async ({ page, browser }) => {
  await signIn(page);
  const data = await (await page.request.get('/api/v1/workspace')).json();
  const context = await browser.newContext();
  try {
    const other = await context.newPage();
    await signIn(other, 'other');
    const own = await (await other.request.get('/api/v1/workspace')).json();
    expect(own.sessions).toEqual([]); expect(own.research).toEqual([]);
    expect((await other.request.get(`/api/v1/sessions/${data.sessions[0].id}/messages`)).status()).toBe(404);
    expect((await other.request.get(`/api/v1/research/${data.research[0].research_id}/report`)).status()).toBe(404);
    await other.reload();
    await expect(other.getByText('合成测试 · 现金流分析', { exact: true })).toHaveCount(0);
  } finally { await context.close(); }
});

test('logout in another tab removes private UI and closes protected access', async ({ page, context }) => {
  await signIn(page);
  const tab = await context.newPage();
  await tab.goto('/');
  await expect(tab.getByRole('button', { name: '退出登录' })).toBeVisible();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(tab.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(tab.getByRole('button', { name: '退出登录' })).toHaveCount(0);
});
