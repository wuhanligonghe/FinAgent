import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { Disclaimer, ErrorState, Loading } from '../components/ui';

export function SettingsPage() {
  const query = useQuery({ queryKey: ['settings'], queryFn: () => api<{ mode: string; model_configured: boolean; model_proxy_configured: boolean; token_streaming: boolean; web_tools: string }>('/settings') });
  return <main className="content-page settings-page"><div className="page-intro"><div><p className="eyebrow">WORKSPACE SETTINGS</p><h1>安静、可靠的研究空间。</h1><p>服务状态与运行边界。</p></div></div>{query.isPending ? <Loading /> : query.error ? <ErrorState error={query.error} /> : <dl className="settings-list"><div><dt>运行模式</dt><dd>{query.data.mode}</dd></div><div><dt>主模型配置</dt><dd>{query.data.model_configured ? '已配置（不代表服务当前可用）' : '尚未配置，请检查后端 finagent.toml 与 .env'}</dd></div><div><dt>实时展示</dt><dd>工具与研究阶段事件；最终答复完成后展示，不模拟 token 流</dd></div><div><dt>工具权限</dt><dd>{query.data.web_tools}</dd></div><div><dt>部署边界</dt><dd>已启用登录和独立用户工作区。默认仅本机访问，外部访问需要部署者配置 HTTPS 站点。</dd></div></dl>}<Disclaimer /></main>;
}
