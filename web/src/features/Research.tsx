import { useRef, useState, type FormEvent } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowRight, FileText, Plus, Search } from 'lucide-react';
import { api, post } from '../api/client';
import type { Research, Session } from '../api/types';
import { Disclaimer, Drawer, EmptyState, ErrorState, Loading, Status } from '../components/ui';
import { useLive } from '../app/context';
import { Timeline } from './Report';

export function ResearchRows({ items, onInspect }: { items: Research[]; onInspect?: (item: Research) => void }) {
  return <div className="research-list">{items.map((item) => <div key={item.work_id} className="research-entry"><Link className="research-row" to={item.research_id && item.report_available ? `/research/${item.research_id}` : `/chat/${item.session_id}`}>
    <span className="document-icon"><FileText size={22} strokeWidth={1.5} /></span><div className="research-info"><h3>{item.title.split('\n')[0]}</h3><p>公司深度研究 <span>·</span> {item.symbol}{item.as_of && <> <span>·</span> 截至 {item.as_of.slice(0, 10)}</>}</p>
      {item.status === 'running' && <><div className="stage-indicator"><i /><i /><i /></div><p className="progress-copy">{item.progress || '研究在后台执行，阶段变化将自动更新'}</p></>}
      {item.content_status === 'partial' && <small className="warning-copy">部分内容可用，请留意证据缺口。</small>}
    </div><div className="research-row-end">{item.report_available ? <span className="report-link">查看报告 <ArrowRight size={17} /></span> : <Status status={item.status} />}</div>
  </Link>{onInspect && <div className="task-actions"><button className="text-button" onClick={() => onInspect(item)}>查看研究过程与操作 →</button></div>}</div>)}</div>;
}

export function ResearchPage() {
  const [search, setSearch] = useSearchParams();
  const [filter, setFilter] = useState('all');
  const [term, setTerm] = useState('');
  const [selected, setSelected] = useState<Research | null>(null);
  const { notify } = useLive();
  const query = useQuery({ queryKey: ['research'], queryFn: () => api<{ items: Research[] }>('/research'), refetchInterval: 3000 });
  const items = (query.data?.items ?? []).filter((item) => (filter === 'all' || (filter === 'active' ? ['running', 'queued'].includes(item.status) : item.status === filter)) && `${item.title} ${item.symbol}`.includes(term));
  const current = query.data?.items.find((item) => item.work_id === selected?.work_id) ?? selected;
  async function action(type: 'cancel' | 'resume') {
    if (!current) return;
    const reference = type === 'resume' ? current.research_id : current.work_id;
    try {
      await post(`/research/${reference}/${type}`);
      notify(type === 'cancel' ? '已请求停止这份研究。' : '已请求恢复原研究。');
      await query.refetch();
    } catch (error) { notify((error as Error).message); }
  }
  return <main className="content-page"><div className="page-intro"><div><p className="eyebrow">RESEARCH LIBRARY</p><h1>让研究持续发生。</h1><p>查看研究进度，回到结论背后的依据。</p></div><button className="primary-button" onClick={() => setSearch({ new: '1' })}><Plus size={18} />新建研究</button></div>
    <div className="list-toolbar"><div className="tabs">{[['all', '全部研究'], ['active', '进行中'], ['completed', '已完成'], ['interrupted', '已中断']].map(([value, label]) => <button className={filter === value ? 'selected' : ''} key={value} onClick={() => setFilter(value)}>{label}</button>)}</div><label className="search-field"><Search size={17} /><input aria-label="搜索研究" placeholder="搜索公司或研究主题" value={term} onChange={(event) => setTerm(event.target.value)} /></label></div>
    {query.isPending ? <Loading /> : query.error ? <ErrorState error={query.error} /> : items.length ? <ResearchRows items={items} onInspect={setSelected} /> : <EmptyState title="这里还没有研究记录">点击“新建研究”，明确公司与研究目标。研究不会因关闭页面而停止。</EmptyState>}
    <Disclaimer />{search.has('new') && <Drawer title="发起公司深度研究" onClose={() => setSearch({})}><ResearchForm onDone={() => setSearch({})} /></Drawer>}
    {current && <Drawer title="研究过程与操作" onClose={() => setSelected(null)}><div className="research-detail"><h3>{current.title.split('\n')[0]}</h3><Status status={current.status} />{current.progress && <p className="muted">{current.progress}</p>}</div><Timeline researchId={current.research_id ?? current.work_id} /><div className="drawer-actions">{current.status === 'running' && <button className="secondary-button" onClick={() => void action('cancel')}>停止这份研究</button>}{current.research_id && ['interrupted', 'failed', 'cancelled'].includes(current.status) && <button className="primary-button" onClick={() => void action('resume')}>恢复原研究</button>}</div></Drawer>}
  </main>;
}

function ResearchForm({ onDone }: { onDone: () => void }) {
  const [symbol, setSymbol] = useState('');
  const [goal, setGoal] = useState('');
  const [cutoff, setCutoff] = useState('');
  const [pending, setPending] = useState(false);
  const session = useRef<Session | null>(null);
  const navigate = useNavigate();
  const { notify } = useLive();
  const queryClient = useQueryClient();
  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    try {
      session.current ??= await post<Session>('/sessions', {});
      await post('/research', { session_id: session.current.id, symbol, text: goal, mode: 'deep_research', as_of: cutoff ? new Date(cutoff).toISOString() : null });
      await queryClient.invalidateQueries();
      onDone();
      navigate(`/chat/${session.current.id}`);
    } catch (error) { notify((error as Error).message); }
    finally { setPending(false); }
  }
  return <form className="detail-form" onSubmit={submit}><p className="muted">明确提交后才会启动独立研究，并消耗模型调用预算。当前只支持公司，不支持指数或板块。</p>
    <label>公司证券代码<input required placeholder="例如 600519.SH" value={symbol} onChange={(event) => setSymbol(event.target.value)} /></label>
    <label>研究目标<textarea required maxLength={12000} rows={5} placeholder="例如：分析经营现金流质量，重点核对利润兑现与资本开支，不做估值。" value={goal} onChange={(event) => setGoal(event.target.value)} /></label>
    <label>资料截止时间 <span className="muted">（可选，使用本机时区）</span><input type="datetime-local" value={cutoff} onChange={(event) => setCutoff(event.target.value)} /></label>
    <p className="form-note">启动后锁定研究范围。报告生成不代表所有结论均已核验。</p><button className="primary-button" disabled={pending}>{pending ? '正在提交…' : '确认并开始研究'}<ArrowRight size={17} /></button>
  </form>;
}
