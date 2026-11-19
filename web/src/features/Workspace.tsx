import { useQuery } from '@tanstack/react-query';
import { useNavigate, Link } from 'react-router-dom';
import { ArrowRight, FileText, Search, Scale } from 'lucide-react';
import { api, post } from '../api/client';
import type { Research, Run, Session } from '../api/types';
import { Composer, Disclaimer, EmptyState, ErrorState } from '../components/ui';
import { ResearchRows } from './Research';
import { useRef, useState } from 'react';

export function Workspace() {
  const navigate = useNavigate();
  const [draft, setDraft] = useState('');
  const pendingSession = useRef<{ text: string; session: Session } | null>(null);
  const { data, error, refetch } = useQuery({ queryKey: ['workspace'], queryFn: () => api<{ sessions: Session[]; research: Research[] }>('/workspace') });
  async function send(text: string) {
    if (pendingSession.current?.text !== text) {
      pendingSession.current = { text, session: await post<Session>('/sessions', {}) };
    }
    const session = pendingSession.current.session;
    await post<Run>(`/sessions/${session.id}/messages`, { text });
    pendingSession.current = null;
    navigate(`/chat/${session.id}`);
  }
  return <main className="workspace">
    <section className="welcome"><div className="welcome-kicker"><span /> YOUR RESEARCH, IN FOCUS</div><h1>让每一次判断，都有据可循。</h1><p>从一个问题开始，连接数据、财报与深度研究。</p></section>
    <Composer key={draft} initialText={draft} onSend={send} />
    <div className="suggestions"><button onClick={() => setDraft('我想了解一家公司的财务表现，请先确认公司和报告期。')}><FileText size={18} />了解公司的财务表现</button><button onClick={() => setDraft('我想比较两家公司的盈利质量，请先向我确认公司和报告期。')}><Scale size={18} />比较两家公司的盈利质量</button><Link to="/research?new=1"><Search size={18} />发起公司深度研究</Link></div>
    <section className="continue-research"><div className="section-heading"><h2>继续研究</h2><Link to="/research">查看全部 <ArrowRight size={15} /></Link></div>
      {error ? <ErrorState error={error} retry={() => void refetch()} /> : data?.research.length ? <ResearchRows items={data.research.slice(0, 3)} /> : <EmptyState title="每一份洞见，都从一个好问题开始">提出你的第一个问题，或发起一份公司深度研究。研究进度会保留在这里。</EmptyState>}
    </section><Disclaimer />
  </main>;
}
