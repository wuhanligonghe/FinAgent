const pendingKeys = new Map<string, string>();
const controllers = new Set<AbortController>();
let csrfToken = '';
let epoch = 0;
let expectedUser = '';

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string) { super(message); }
}
export const authEpoch = () => epoch;
export function setCsrfToken(value: string) { csrfToken = value; }
export function setApiUser(value: string) { expectedUser = value; }
export function resetApiState() {
  epoch += 1;
  controllers.forEach((controller) => controller.abort());
  controllers.clear(); pendingKeys.clear(); csrfToken = ''; expectedUser = '';
}

export async function api<Result>(path: string, options: RequestInit = {}): Promise<Result> {
  const started = epoch;
  const isAuth = path.startsWith('/auth/');
  const write = !['GET', 'HEAD', 'OPTIONS'].includes(options.method ?? 'GET');
  if (write && !csrfToken) {
    const result = await api<{ csrf_token: string }>('/auth/csrf');
    if (started !== epoch) throw new DOMException('身份已变化', 'AbortError');
    csrfToken = result.csrf_token;
  }
  const fingerprint = `${started}:${path}:${isAuth ? '' : options.body ?? ''}`;
  const headers = new Headers(options.headers);
  if (!isAuth && expectedUser) headers.set('X-FinSight-User', expectedUser);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (write) headers.set('X-CSRF-Token', csrfToken);
  if (!isAuth && options.method === 'POST' && !path.endsWith('/sessions') && !(options.body instanceof FormData)) {
    const key = pendingKeys.get(fingerprint) ?? crypto.randomUUID();
    pendingKeys.set(fingerprint, key);
    headers.set('Idempotency-Key', key);
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (options.signal?.aborted) controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  controllers.add(controller);
  try {
    const response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: 'same-origin', signal: controller.signal });
    const body = response.status === 204 ? undefined : await response.json().catch(() => ({}));
    if (started !== epoch) throw new DOMException('身份已变化', 'AbortError');
    if (!response.ok) {
      if (response.status < 500) pendingKeys.delete(fingerprint);
      if (response.status === 401 && !isAuth && typeof window !== 'undefined') window.dispatchEvent(new Event('finsight:unauthenticated'));
      throw new ApiError(body?.message ?? `请求失败 (${response.status})`, response.status, body?.code);
    }
    pendingKeys.delete(fingerprint);
    return body as Result;
  } finally {
    controllers.delete(controller);
    options.signal?.removeEventListener('abort', abort);
  }
}

export function post<Result>(path: string, body?: unknown) {
  return api<Result>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
}

export function safeExternalUrl(value: string | null | undefined) {
  try {
    const url = new URL(value ?? '');
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined;
  } catch {
    return undefined;
  }
}

export function safeReturnTo(value: string | null) {
  try {
    const decoded = decodeURIComponent(value ?? '/');
    if (!decoded.startsWith('/') || decoded.startsWith('//') || /[\\\u0000-\u0020]/.test(decoded)) return '/';
    const url = new URL(decoded, 'https://finsight.invalid');
    if (url.origin !== 'https://finsight.invalid' || /^\/(login|register)(\/|$)/.test(url.pathname)) return '/';
    return url.pathname + url.search + url.hash;
  } catch { return '/'; }
}
