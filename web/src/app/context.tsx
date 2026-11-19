import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { WebEvent } from '../api/types';
import { api, ApiError, authEpoch } from '../api/client';
import { useAuth } from './auth';

const LiveContext = createContext<{ events: WebEvent[]; connected: boolean; notify: (message: string) => void }>({
  events: [], connected: false, notify: () => undefined,
});

export const useLive = () => useContext(LiveContext);

export function LiveProvider({ children }: { children: ReactNode }) {
  const [events, setEvents] = useState<WebEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [notice, setNotice] = useState('');
  const queryClient = useQueryClient();
  const { user } = useAuth();
  useEffect(() => {
    const epoch = authEpoch();
    const key = `finsight:${user!.id}:event-cursor`;
    let cursor = Number(sessionStorage.getItem(key) ?? 0);
    const source = new EventSource(`/api/v1/events?after=${cursor}&expected_user_id=${encodeURIComponent(user!.id)}`);
    source.onopen = () => { if (epoch === authEpoch()) setConnected(true); };
    source.onerror = () => {
      if (epoch !== authEpoch()) return;
      setConnected(false);
      void api<{ user: { id: string } }>('/auth/me').then((result) => {
        if (epoch === authEpoch() && result.user.id !== user!.id) window.dispatchEvent(new Event('finsight:unauthenticated'));
      }).catch((error) => {
        if (epoch === authEpoch() && error instanceof ApiError && error.status === 401) window.dispatchEvent(new Event('finsight:unauthenticated'));
      });
    };
    source.onmessage = (message) => {
      if (epoch !== authEpoch()) return;
      const event = JSON.parse(message.data) as WebEvent;
      if (event.sequence <= cursor) return;
      cursor = event.sequence;
      sessionStorage.setItem(key, String(cursor));
      setEvents((previous) => [...previous, event].slice(-80));
      void queryClient.invalidateQueries();
    };
    return () => source.close();
  }, [queryClient, user]);
  useEffect(() => {
    if (!notice) return;
    const timeout = window.setTimeout(() => setNotice(''), 7000);
    return () => window.clearTimeout(timeout);
  }, [notice]);
  return <LiveContext.Provider value={{ events, connected, notify: setNotice }}>
    {children}
    {notice && <div className="toast" role="alert"><span>{notice}</span><button onClick={() => setNotice('')} aria-label="关闭提示">×</button></div>}
  </LiveContext.Provider>;
}
