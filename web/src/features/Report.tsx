import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useNavigate, useParams } from 'react-router-dom';
import { Download, ExternalLink, List, ListTree } from 'lucide-react';
import { api, post, safeExternalUrl } from '../api/client';
import type { Citation, Report, Run } from '../api/types';
import { Composer, Disclaimer, Drawer, EmptyState, ErrorState, Loading, Status, statusLabel } from '../components/ui';
import { useLive } from '../app/context';
import { MarkdownContent } from '../components/MarkdownContent';

export function ReportPage() {
  const { researchId } = useParams();
  const [citation, setCitation] = useState<string | null>(null);
  const [timeline, setTimeline] = useState(false);
  const [tocOpen, setTocOpen] = useState(false);
  const [active, setActive] = useState('findings');
  const navigate = useNavigate();
  const query = useQuery({ queryKey: ['report', researchId], queryFn: () => api<Report>(`/research/${researchId}/report`) });
  const { notify } = useLive();
  useEffect(() => {
    if (!query.data) return;
    const observer = new IntersectionObserver((entries) => {
      const visible = entries.find((entry) => entry.isIntersecting);
      if (visible) setActive(visible.target.id);
    }, { rootMargin: '-10% 0px -65% 0px' });
    document.querySelectorAll('[data-report-section]').forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [query.data]);
  async function ask(text: string) {
    const run = await post<Run>(`/research/${researchId}/questions`, { text });
    navigate(`/chat/${run.session_id}`);
  }
  async function action(type: 'cancel' | 'resume') {
    try { await post(`/research/${researchId}/${type}`); notify(type === 'cancel' ? '已请求停止指定研究。' : '已请求恢复原研究。'); }
    catch (error) { notify((error as Error).message); }
  }
  if (query.isPending) return <Loading />;
  if (query.error) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const report = query.data;
  const toc = [{ id: 'findings', title: '核心发现' }, ...report.sections.map(({ id, title }) => ({ id, title })), { id: 'limitations', title: '风险与证据缺口' }, { id: 'references', title: '来源与引用' }];
  return <main className="report-page"><div className="report-tools"><button className="text-button" onClick={() => setTimeline(true)}><ListTree size={17} />查看研究过程</button><a className="text-button" href={`/api/v1/research/${researchId}/export?format=markdown`} download><Download size={17} />导出</a><button className="icon-button toc-toggle" onClick={() => setTocOpen(!tocOpen)} aria-label="报告目录"><List size={19} /></button></div>
    <article className="report-article"><div className="report-body"><header className="report-heading"><span className="category-label">公司深度研究</span><h1>{report.title}</h1><p className="report-subtitle">从证据出发，理解结论与边界。</p><div className="report-meta"><span>{report.scope.symbol}</span><span>资料截止：{report.scope.as_of.slice(0, 10)}</span><Status status={report.execution_status} /></div><div className="report-quality"><span>内容：{statusLabel(report.content_status)}</span><span>审阅：{statusLabel(report.review_status)}</span><span>{report.outcome_verified ? '结果已核验（以核验范围为准）' : '结果尚未核验'}</span></div></header>
      <section id="findings" data-report-section className="key-findings"><h2>核心发现</h2>{report.key_findings.length ? <ol>{report.key_findings.map((finding, index) => <li key={finding.claim_id}><span className="finding-number">{String(index + 1).padStart(2, '0')}</span><div>{finding.text}{finding.citation_ids.map((id) => <button className="citation-mark" key={id} onClick={() => setCitation(id)}>[{id}]</button>)}</div></li>)}</ol> : <p>没有可提取的已接纳主张，请查看正文与覆盖限制。</p>}<small>摘自已接纳主张，完整结论与限制以正文为准。</small></section>
      {report.sections.map((section) => <section className="report-section" data-report-section id={section.id} key={section.id}><h2>{section.title}</h2><MarkdownContent>{section.markdown}</MarkdownContent></section>)}
      <section className="report-section limitations" id="limitations" data-report-section><h2>风险与证据缺口</h2>{report.limitations.length ? <ul>{report.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p>请结合正文中的阶段状态、质量门及覆盖限制阅读。未列出缺口不代表没有风险。</p>}</section>
      <section className="report-section" id="references" data-report-section><h2>来源与引用</h2>{report.citations.length ? report.citations.map((item) => <button className="reference-row" key={item.id} onClick={() => setCitation(item.id)}><strong>[{item.id}] {item.source}</strong><span>{item.locator ?? '查看来源定位'}</span><ExternalLink size={15} /></button>) : <p className="muted">当前报告未提供可展示的引用清单。</p>}</section>
      </div>
      <div className="report-followup"><div className="followup-suggestions"><button onClick={() => void ask('哪些结论仍需补充证据？').catch((error) => notify(error.message))}>哪些结论仍需补充证据？</button><button onClick={() => void ask('请解释经营现金流与利润的差异。').catch((error) => notify(error.message))}>解释现金流与利润的差异</button></div><Composer compact onSend={ask} placeholder="针对这份报告继续提问…" /><Disclaimer /></div>
    </article>
    <aside className={`report-toc ${tocOpen ? 'toc-open' : ''}`}><h3>本报告</h3><nav aria-label="报告目录">{toc.map((item) => <a key={item.id} href={`#${item.id}`} className={active === item.id ? 'current' : ''} onClick={() => setTocOpen(false)}>{item.title}</a>)}</nav><div className="citation-preview"><h3>引用预览</h3>{report.citations[0] ? <><strong>[1] {report.citations[0].source}</strong><p>{report.citations[0].locator}</p><button className="text-button" onClick={() => setCitation('1')}>查看证据 <ExternalLink size={13} /></button></> : <p className="muted">没有可用引用</p>}</div></aside>
    {citation && <Drawer title="证据与来源" onClose={() => setCitation(null)}><CitationDetail researchId={researchId!} citationId={citation} /></Drawer>}
    {timeline && <Drawer title="研究过程" onClose={() => setTimeline(false)}><Timeline researchId={researchId!} /><div className="drawer-actions">{report.execution_status === 'running' && <button className="secondary-button" onClick={() => void action('cancel')}>停止这份研究</button>}{['interrupted', 'failed', 'cancelled'].includes(report.execution_status) && <button className="primary-button" onClick={() => void action('resume')}>恢复原研究</button>}</div></Drawer>}
  </main>;
}

function CitationDetail({ researchId, citationId }: { researchId: string; citationId: string }) {
  const query = useQuery({ queryKey: ['citation', researchId, citationId], queryFn: () => api<Citation>(`/research/${researchId}/citations/${citationId}`) });
  if (query.isPending) return <Loading />;
  if (query.error) return <ErrorState error={query.error} />;
  const source = safeExternalUrl(query.data.source_url);
  return <div className="citation-detail"><span className="category-label">引用 [{citationId}]</span><h3>{query.data.source}</h3><p className="muted">原文定位：{query.data.locator ?? '未提供'}</p>{query.data.excerpt ? <blockquote>{query.data.excerpt}</blockquote> : <EmptyState title="暂无精确原文预览">请按原始定位查看来源文件。</EmptyState>}<p className="form-note">{query.data.note}</p>{source && <a className="primary-button" href={source} target="_blank" rel="noopener noreferrer">打开原始来源 <ExternalLink size={16} /></a>}</div>;
}

export function Timeline({ researchId }: { researchId: string }) {
  const query = useQuery({ queryKey: ['timeline', researchId], queryFn: () => api<{ stages: { role: string; status: string; reason: string }[] }>(`/research/${researchId}`), refetchInterval: 3000 });
  if (query.isPending) return <Loading />;
  if (query.error) return <ErrorState error={query.error} />;
  return <div className="timeline">{query.data.stages.length ? query.data.stages.map((stage, index) => <div key={index}><span className="timeline-dot" /><h3>{stage.role}</h3><Status status={stage.status} />{stage.reason && <p className="muted">{stage.reason}</p>}</div>) : <EmptyState title="尚无阶段记录" />}</div>;
}
