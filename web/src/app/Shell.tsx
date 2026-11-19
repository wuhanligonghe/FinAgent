import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom';
import { ClipboardList, LogOut, Menu, MessageCircle, Plus, Settings, UserRound, X } from 'lucide-react';
import { api } from '../api/client';
import type { Session } from '../api/types';
import { useLive } from './context';
import { useAuth } from './auth';

export function Shell() {
  const [open, setOpen] = useState(false);
  const { connected, notify } = useLive();
  const { user, logout } = useAuth();
  const location = useLocation();
  const { data } = useQuery({ queryKey: ['sessions'], queryFn: () => api<{ items: Session[] }>('/sessions?limit=6') });
  const report = /^\/research\/.+/.test(location.pathname);
  return <div className="app-shell">
    {open && <button className="sidebar-scrim" aria-label="收起导航" onClick={() => setOpen(false)} />}
    <aside className={`sidebar ${open ? 'is-open' : ''}`}>
      <Link to="/" className="brand" onClick={() => setOpen(false)} aria-label="FinSight 首页"><span className="brand-mark"><i /><i /></span>FinSight</Link>
      <button className="mobile-close icon-button" aria-label="收起导航" onClick={() => setOpen(false)}><X size={20} /></button>
      <Link to="/" className="new-conversation" onClick={() => setOpen(false)}><Plus size={21} />新建对话</Link>
      <nav aria-label="主导航">
        <NavLink to="/" end className={location.pathname.startsWith('/chat') ? 'active' : undefined} onClick={() => setOpen(false)}><MessageCircle size={20} />对话</NavLink>
        <NavLink to="/research" onClick={() => setOpen(false)}><ClipboardList size={20} />研究任务</NavLink>
      </nav>
      <div className="sidebar-recents"><p className="eyebrow">最近对话</p>
        {data?.items.length ? data.items.map((session) => <Link key={session.id} to={`/chat/${session.id}`} onClick={() => setOpen(false)}><MessageCircle size={15} /><span>{session.title}</span></Link>) : <p className="sidebar-empty">你的研究，将从这里延续。</p>}
      </div>
      <div className="sidebar-bottom"><NavLink to="/settings"><Settings size={19} />设置</NavLink><div className="user-row"><span className="avatar"><UserRound size={18} /></span><span>{user?.display_name}</span><button className="user-logout" aria-label="退出登录" title="退出登录" onClick={() => void logout().catch(() => notify('退出未完成，请检查连接后重试。'))}><LogOut size={17} /></button></div></div>
    </aside>
    <div className={`main-shell ${report ? 'report-shell' : ''}`}>
      <header className="page-header"><div className="header-title"><button className="mobile-menu icon-button" aria-label="展开导航" onClick={() => setOpen(true)}><Menu size={21} /></button>
        {report ? <><Link to="/research">研究任务</Link><span className="slash">/</span><strong>研究报告</strong></> : <strong>{location.pathname === '/settings' ? '设置' : location.pathname === '/research' ? '研究任务' : '研究工作台'}</strong>}
      </div><div className="header-actions"><span className={`connection ${connected ? 'online' : ''}`} title={connected ? '进度连接正常' : '进度连接中，数据将自动刷新'}><i />{connected ? '已连接' : '连接中'}</span></div></header>
      <Outlet />
    </div>
  </div>;
}
