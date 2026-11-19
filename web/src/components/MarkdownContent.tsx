import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { safeExternalUrl } from '../api/client';

export function MarkdownContent({ children }: { children: string }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml components={{
    a: ({ href, children: label }) => {
      const destination = href?.startsWith('#') ? href : safeExternalUrl(href);
      return destination ? <a href={destination} target={destination.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer">{label}</a> : <span>{label}</span>;
    },
    img: ({ alt }) => <span className="muted">{alt ? `[图片：${alt}]` : '[外部图片未加载]'}</span>,
  }}>{children}</ReactMarkdown></div>;
}
