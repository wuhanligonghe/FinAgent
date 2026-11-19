import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { api, ApiError, post, resetApiState, setCsrfToken, setApiUser } from '../api/client';

export type User = { id: string; username: string; display_name: string };
type Auth = { user: User | null; checking: boolean; version: number; error: string; refresh: () => Promise<void>;
  login: (identifier: string, password: string, remember: boolean) => Promise<void>; logout: () => Promise<void> };
const AuthContext = createContext<Auth>(null!);
export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const [error, setError] = useState('');
  const [version, setVersion] = useState(0);
  const current = useRef<User | null>(null);
  const sequence = useRef(0);
  const channel = useRef<BroadcastChannel | null>(null);
  const commit = useCallback((next: User | null, force = false) => {
    if (force || current.current?.id !== next?.id) {
      const old = current.current?.id;
      resetApiState();
      if (old) sessionStorage.removeItem(`finsight:${old}:event-cursor`);
      current.current = next; setVersion((v) => v + 1);
    }
    setApiUser(next?.id ?? '');
    setUser(next); setChecking(false); setError('');
  }, []);
  const broadcast = () => {
    channel.current?.postMessage('identity-changed');
    if (!channel.current) localStorage.setItem('finsight-auth-change', String(Date.now()));
  };
  const refresh = useCallback(async () => {
    const ticket = ++sequence.current;
    try {
      const result = await api<{ user: User }>('/auth/me');
      if (ticket !== sequence.current) return;
      commit(result.user);
      const metadata = await api<{ csrf_token: string }>('/auth/csrf');
      if (ticket === sequence.current) setCsrfToken(metadata.csrf_token);
    } catch (cause) {
      if (ticket !== sequence.current || (cause instanceof DOMException && cause.name === 'AbortError')) return;
      if (cause instanceof ApiError && cause.status === 401) commit(null);
      else { setChecking(true); setError('暂时无法确认登录状态，请检查连接后重试。'); }
    }
  }, [commit]);
  useEffect(() => {
    void refresh();
    const invalid = () => { resetApiState(); setChecking(true); void refresh(); };
    const focus = () => { void refresh(); };
    const changed = () => { resetApiState(); setChecking(true); void refresh(); };
    const storage = (event: StorageEvent) => { if (event.key === 'finsight-auth-change') changed(); };
    if (typeof BroadcastChannel !== 'undefined') {
      channel.current = new BroadcastChannel('finsight-auth');
      channel.current.onmessage = changed;
    }
    window.addEventListener('finsight:unauthenticated', invalid);
    window.addEventListener('focus', focus);
    window.addEventListener('storage', storage);
    return () => {
      ++sequence.current;
      channel.current?.close(); channel.current = null;
      window.removeEventListener('finsight:unauthenticated', invalid);
      window.removeEventListener('focus', focus);
      window.removeEventListener('storage', storage);
    };
  }, [commit, refresh]);
  const login = async (identifier: string, password: string, remember: boolean) => {
    ++sequence.current; resetApiState();
    const result = await post<{ user: User; csrf_token: string }>('/auth/login', { identifier, password, remember });
    commit(result.user, true); setCsrfToken(result.csrf_token); broadcast();
  };
  const logout = async () => {
    await post('/auth/logout');
    ++sequence.current; commit(null, true); broadcast();
  };
  return <AuthContext.Provider value={{ user, checking, version, error, refresh, login, logout }}>{children}</AuthContext.Provider>;
}

export function RequireAuth() {
  const { user, checking, error, refresh } = useAuth();
  const location = useLocation();
  if (checking) return <div className="auth-check" role="status">{error || '正在确认登录状态…'}{error && <button onClick={() => void refresh()}>重新连接</button>}</div>;
  if (!user) return <Navigate to={`/login?return_to=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  return <Outlet />;
}
