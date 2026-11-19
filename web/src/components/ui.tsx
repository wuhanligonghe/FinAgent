import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { ArrowUp, FileText, LoaderCircle, X } from 'lucide-react';
import { useLive } from '../app/context';

const labels: Record<string, string> = {
  queued: '等待执行', running: '研究中', completed: '已完成', failed: '执行失败', cancelled: '已取消',
  cancelling: '正在停止', interrupted: '执行已中断', needs_clarification: '等待补充信息',
  suspended: '已暂停', complete: '内容完整', partial: '部分内容', unavailable: '内容不可用',
  pending: '待审阅', passed: '审阅已完成', issues_found: '存在待核对事项',
};

export const statusLabel = (status: string) => labels[status] ?? status;

export function Status({ status }: { status: string }) {
  return <span className={`status status-${status}`}><i />{statusLabel(status)}</span>;
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty-state"><FileText size={24} strokeWidth={1.4} /><h3>{title}</h3>{children && <p>{children}</p>}</div>;
}

export function Loading() {
  return <div className="loading" role="status"><LoaderCircle className="spin" size={20} /> 正在读取…</div>;
}

export function ErrorState({ error, retry }: { error: Error | null; retry?: () => void }) {
  return <div className="error-state" role="alert"><p>{error?.message ?? '内容暂时不可用'}</p>{retry && <button className="text-button" onClick={retry}>重新加载</button>}</div>;
}

export function Disclaimer() {
  return <p className="disclaimer">AI 辅助研究，不构成投资建议。</p>;
}

export function Composer({ onSend, placeholder = '想研究哪家公司，或了解什么市场问题？', compact = false,
  busy = false, initialText = '' }: {
  onSend: (text: string) => Promise<void>; placeholder?: string; compact?: boolean; busy?: boolean; initialText?: string;
}) {
  const [text, setText] = useState(initialText);
  const [sending, setSending] = useState(false);
  const { notify } = useLive();
  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!text.trim() || sending || busy) return;
    setSending(true);
    try {
      await onSend(text.trim());
      setText('');
    } catch (error) {
      notify(error instanceof Error ? error.message : '提交失败，请重试');
    } finally {
      setSending(false);
    }
  }
  return <form className={`composer ${compact ? 'composer-compact' : ''}`} onSubmit={submit}>
    <textarea aria-label="研究问题" placeholder={placeholder} value={text} maxLength={12000}
      rows={compact ? 2 : 4} onChange={(event) => setText(event.target.value)}
      onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); } }} />
    <div className="composer-toolbar">
      <span className="composer-hint">{busy ? '当前回复完成后可继续提问' : 'Enter 发送 · Shift + Enter 换行'}</span>
      <button className="send-button" type="submit" disabled={!text.trim() || sending || busy} aria-label="发送问题">
        {sending ? <LoaderCircle className="spin" size={20} /> : <ArrowUp size={23} />}
      </button>
    </div>
  </form>;
}

export function Drawer({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const panel = useRef<HTMLElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previousFocus = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close.current();
      if (event.key !== 'Tab') return;
      const controls = [...(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input, textarea, select') ?? [])].filter((element) => element.offsetParent !== null);
      const first = controls[0];
      const last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keyboard);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener('keydown', keyboard);
      previousFocus?.focus();
    };
  }, []);
  return <div className="drawer-backdrop" onClick={onClose}><section ref={panel} className="drawer" role="dialog" aria-modal="true" aria-label={title}
    onClick={(event) => event.stopPropagation()}>
    <header><h2>{title}</h2><button className="icon-button" onClick={onClose} aria-label="关闭面板"><X size={21} /></button></header>
    {children}
  </section></div>;
}
