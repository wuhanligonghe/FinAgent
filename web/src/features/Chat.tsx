import { useEffect, useRef } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Link, useParams } from 'react-router-dom';
import { ArrowDown, CircleStop, Sparkles } from 'lucide-react';
import { api, post } from '../api/client';
import type { Research, Run, Session } from '../api/types';
import { Composer, Disclaimer, EmptyState, ErrorState, Loading, Status } from '../components/ui';
import { useLive } from '../app/context';
import { MarkdownContent } from '../components/MarkdownContent';

export function Chat() {
  const { sessionId } = useParams();
  const { events, notify } = useLive();
  const queryClient = useQueryClient();
  const bottom = useRef<HTMLDivElement>(null);
  const query = useQuery({ queryKey: ['messages', sessionId], queryFn: () => api<{ session: Session; items: Run[]; next_cursor: number | null }>(`/sessions/${sessionId}/messages`),
    refetchInterval: 2500 });
  const research = useQuery({ queryKey: ['research'], queryFn: () => api<{ items: Research[] }>('/research') });
  const runs = query.data?.items ?? [];
  const active = runs.find((run) => ['queued', 'running', 'cancelling'].includes(run.status));
  const activity = events.filter((event) => event.session_id === sessionId && (event.type.startsWith('tool.') || event.type === 'research.progress')).slice(-4);
  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }, [runs.length, active?.status]);
  async function send(text: string) {
    await post(`/sessions/${sessionId}/messages`, { text });
    await queryClient.invalidateQueries({ queryKey: ['messages', sessionId] });
  }
  async function cancel() {
    try { await post(`/runs/${active?.id}/cancel`); await query.refetch(); }
    catch (error) { notify((error as Error).message); }
  }
  if (query.isPending) return <Loading />;
  if (query.error) return <ErrorState error={query.error} />;
  return <main className="chat-page"><div className="chat-heading"><div><p className="eyebrow">持续研究 · 上下文已保存</p><h1>{query.data.session.title}</h1></div><Link className="text-button" to="/research?new=1">新建公司研究 ↗</Link></div>
    <div className="messages">
      {runs.length >= 50 && <p className="muted">当前展示最近50轮对话，历史上下文仍由后端保留。</p>}
      {!runs.length && <EmptyState title="想从哪里开始？">可以查询数据、解读财报，或讨论一个金融概念。</EmptyState>}
      {runs.map((run) => <div className="turn" key={run.id}>
        {run.text && <div className="user-message"><span className="message-author">你</span><p>{run.text}</p></div>}
        <div className="assistant-message"><div className="assistant-label"><Sparkles size={16} />FinSight{run.kind === 'notification' && <small>研究通知</small>}</div>
          {run.result?.answer ? <MarkdownContent>{run.status === 'failed' ? run.result.answer.replace('可用 /details 查看诊断。', '可展开下方的故障诊断。') : run.result.answer}</MarkdownContent> : run.error ? <div className="inline-error" role="alert">{run.error}</div> : <Status status={run.status} />}
          {run.result?.tool_calls === 0 && <p className="message-note">本轮未调用工具，不代表重新查询或核验过数据。</p>}
          {run.status === 'failed' && <details className="run-diagnostics"><summary>故障诊断</summary>
            {run.result?.diagnostic ? <><dl><div><dt>错误类型</dt><dd>{run.result.diagnostic.code}</dd></div>{run.result.diagnostic.transport_error && <div><dt>网络错误</dt><dd>{run.result.diagnostic.transport_error}</dd></div>}{run.result.diagnostic.http_status && <div><dt>HTTP 状态</dt><dd>{run.result.diagnostic.http_status}</dd></div>}</dl><p>{run.result.diagnostic.hint}</p></> : <p>该历史记录未保存结构化诊断。请检查后端网络与 FINAGENT_MODEL_PROXY 配置；修改后需要重启后端。</p>}
            <p>模型尝试 {run.result?.model_calls ?? '未知'} 次；工具调用 {run.result?.tool_calls ?? '未知'} 次。</p><code>{run.id}</code>
          </details>}
          {['interrupted', 'cancelled', 'needs_clarification', 'failed'].includes(run.status) && run.result?.answer && <Status status={run.status} />}
        </div>
      </div>)}
      {active && activity.length > 0 && <div className="tool-activity"><p className="eyebrow">正在处理</p>{activity.map((event) => <p key={event.event_id}><span className="activity-dot" />{event.payload.message ?? `${event.payload.name} · ${event.type === 'tool.started' ? '调用中' : '已返回'}`}</p>)}</div>}
      {research.data?.items.filter((item) => item.session_id === sessionId).map((item) => <Link className="research-inline" key={item.work_id} to={item.research_id ? `/research/${item.research_id}` : '/research'}><FileResearchIcon /><span>{item.symbol} · 公司研究</span><Status status={item.status} /><ArrowDown size={16} /></Link>)}
      <div ref={bottom} />
    </div>
    <div className="chat-composer">{active && <button className="stop-button" onClick={() => void cancel()}><CircleStop size={16} />只停止当前回复</button>}<Composer compact busy={!!active} onSend={send} placeholder="继续追问，或提出新的研究问题…" /><Disclaimer /></div>
  </main>;
}

function FileResearchIcon() { return <Sparkles size={18} />; }
