import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, safeExternalUrl, resetApiState, setCsrfToken, setApiUser, safeReturnTo } from './client';

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => { resetApiState(); setCsrfToken('synthetic-csrf'); });

describe('source URLs', () => {
  it('allows public HTTPS links', () => {
    expect(safeExternalUrl('https://example.com/report.pdf')).toBe('https://example.com/report.pdf');
  });
  it('rejects executable schemes, relative links and credentials', () => {
    for (const url of ['javascript:alert(1)', 'data:text/html,bad', '/etc/passwd', 'https://user:secret@example.com']) {
      expect(safeExternalUrl(url)).toBeUndefined();
    }
  });
});

describe('identity boundary', () => {
  it('sends the expected identity only as a consistency guard on private requests', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal('fetch', fetcher); setApiUser('user-a');
    await api('/workspace'); await api('/auth/me');
    expect(fetcher.mock.calls[0][1].headers.get('X-FinSight-User')).toBe('user-a');
    expect(fetcher.mock.calls[1][1].headers.get('X-FinSight-User')).toBeNull();
  });
  it('rejects external and encoded return paths', () => {
    for (const path of ['https://evil.example', '//evil.example', '/\\evil.example', '%2f%2fevil.example', '/login']) expect(safeReturnTo(path)).toBe('/');
    expect(safeReturnTo('/research/example?view=report')).toBe('/research/example?view=report');
  });
  it('rejects a slow response from the previous identity', async () => {
    let resolve!: (result: unknown) => void;
    vi.stubGlobal('fetch', vi.fn(() => new Promise((done) => { resolve = done; })));
    const pending = api('/workspace');
    resetApiState();
    resolve({ ok: true, json: async () => ({ private: 'old account' }) });
    await expect(pending).rejects.toThrow('身份已变化');
  });
  it('handles logout with no response body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 204 }));
    await expect(api('/auth/logout', { method: 'POST' })).resolves.toBeUndefined();
  });
});

describe('API submission', () => {
  it('retains the idempotency key after an ambiguous network failure', async () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'stable-request-id' });
    const fetcher = vi.fn().mockRejectedValueOnce(new TypeError('network interrupted'))
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'run-1' }) });
    vi.stubGlobal('fetch', fetcher);
    const options = { method: 'POST', body: JSON.stringify({ text: 'question' }) };
    await expect(api('/sessions/example/messages', options)).rejects.toThrow('network interrupted');
    await expect(api('/sessions/example/messages', options)).resolves.toEqual({ id: 'run-1' });
    const firstHeaders = fetcher.mock.calls[0][1].headers as Headers;
    const secondHeaders = fetcher.mock.calls[1][1].headers as Headers;
    expect(firstHeaders.get('Idempotency-Key')).toBe(secondHeaders.get('Idempotency-Key'));
  });
  it('surfaces the server error without leaking the raw response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({ message: '访问被拒绝' }) }));
    await expect(api('/workspace')).rejects.toThrow('访问被拒绝');
  });
});
