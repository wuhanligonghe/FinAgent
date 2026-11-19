import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { Link, Navigate, useNavigate, useSearchParams } from 'react-router-dom';
import { Check, Eye, EyeOff, FileText, Network, TrendingUp } from 'lucide-react';
import { api, post, safeReturnTo } from '../../api/client';
import { useAuth } from '../../app/auth';

export function AuthLayout({ children }: { children: ReactNode }) {
  return <main className="auth-page"><section className="auth-brand-panel" aria-label="FinSight 金融研究工作台">
    <Link className="auth-brand" to="/login"><span className="auth-logo" aria-hidden="true"><i /><i /><i /></span>FinSight</Link>
    <div className="auth-brand-copy"><h1>让每一次分析，都有据可循</h1><p>连接市场数据与原始证据，构建你的研究判断。</p></div>
    <div className="auth-diagram" aria-hidden="true">
      <svg className="auth-connectors" viewBox="0 0 620 300"><path d="M220 80 C285 80 235 150 302 150 M220 240 C285 240 235 150 302 150 M435 150 L490 150" /><circle cx="300" cy="150" r="6" /><path d="M481 144 L489 150 L481 156" /></svg>
      <div className="auth-market auth-glass"><strong><TrendingUp size={19} />市场数据</strong><svg viewBox="0 0 220 100"><defs><linearGradient id="chart-fill" x1="0" x2="0" y1="0" y2="1"><stop stopColor="#3ee5ce" stopOpacity=".32" /><stop offset="1" stopColor="#3ee5ce" stopOpacity="0" /></linearGradient></defs><path d="M5 88 L25 67 L40 77 L65 38 L80 50 L95 45 L110 59 L130 30 L145 41 L162 17 L178 25 L210 5 L210 95 L5 95 Z" fill="url(#chart-fill)" /><path d="M5 88 L25 67 L40 77 L65 38 L80 50 L95 45 L110 59 L130 30 L145 41 L162 17 L178 25 L210 5" fill="none" stroke="#48e6cf" strokeWidth="2" /></svg></div>
      <div className="auth-evidence auth-glass"><strong><FileText size={19} />原始证据</strong><div><FileText size={21} /><span><i /><i /></span></div><div><FileText size={21} /><span><i /><i /></span></div></div>
      <div className="auth-analysis auth-glass"><strong><Network size={19} />分析节点</strong>{[1, 2, 3].map((i) => <div key={i}><b /><span><i /><i /></span></div>)}</div>
      <div className="auth-conclusion auth-glass"><strong><span><Check size={15} /></span>研究结论</strong><i /><i /><i /></div>
    </div><p className="auth-brand-footer">行情洞察 · 原文检索 · 深度研究</p>
  </section><section className="auth-form-panel"><div className="auth-mobile-brand">FinSight</div><div className="auth-form-content">{children}</div><footer><Link to="/privacy">隐私政策</Link><Link to="/terms">服务条款</Link></footer></section></main>;
}

function PasswordField({ value, onChange, fresh = false }: { value: string; onChange: (value: string) => void; fresh?: boolean }) {
  const [visible, setVisible] = useState(false);
  return <label className="auth-field">密码<span className="auth-password"><input required minLength={fresh ? 15 : 1} maxLength={128} type={visible ? 'text' : 'password'} autoComplete={fresh ? 'new-password' : 'current-password'} placeholder={fresh ? '设置15至128个字符的密码' : '请输入密码'} value={value} onChange={(e) => onChange(e.target.value)} /><button type="button" aria-label={visible ? '隐藏密码' : '显示密码'} onClick={() => setVisible(!visible)}>{visible ? <EyeOff size={21} /> : <Eye size={21} />}</button></span></label>;
}

export function Login() {
  const auth = useAuth(); const [params] = useSearchParams(); const navigate = useNavigate();
  const [identifier, setIdentifier] = useState(''); const [password, setPassword] = useState('');
  const [remember, setRemember] = useState(false); const [busy, setBusy] = useState(false);
  const [error, setError] = useState(''); const [registration, setRegistration] = useState(false);
  useEffect(() => { let active = true; api<{ registration: string }>('/auth/csrf').then((r) => { if (active) setRegistration(r.registration === 'invite'); }).catch(() => undefined); return () => { active = false; }; }, []);
  const next = safeReturnTo(params.get('return_to'));
  if (auth.user && !auth.checking) return <Navigate to={next} replace />;
  const submit = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setError('');
    try { await auth.login(identifier.trim(), password, remember); setPassword(''); navigate(next, { replace: true }); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '登录未完成，请重试'); }
    finally { setBusy(false); }
  };
  return <AuthLayout><h2>欢迎回来</h2><p className="auth-subtitle">登录 FinSight，继续你的研究</p><form onSubmit={submit}>
    <label className="auth-field">邮箱或用户名<input required maxLength={254} autoComplete="username" placeholder="请输入邮箱或用户名" value={identifier} onChange={(e) => setIdentifier(e.target.value)} /></label>
    <PasswordField value={password} onChange={setPassword} />
    <div className="auth-options"><label><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />记住我</label><Link to="/forgot-password">忘记密码？</Link></div>
    {error && <p className="auth-error" role="alert">{error}</p>}{auth.error && <p className="auth-error" role="alert">{auth.error} <button type="button" onClick={() => void auth.refresh()}>重试</button></p>}<button className="auth-submit" disabled={busy || auth.checking}>{busy ? '登录中…' : '登录'}</button>
  </form>{registration && <p className="auth-register-link">还没有账号？ <Link to="/register">创建账户</Link></p>}</AuthLayout>;
}

export function Register() {
  const [username, setUsername] = useState(''); const [email, setEmail] = useState('');
  const [password, setPassword] = useState(''); const [invite, setInvite] = useState('');
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [done, setDone] = useState(false);
  const submit = async (e: FormEvent) => { e.preventDefault(); setBusy(true); setError('');
    try { await post('/auth/register', { username, email: email || null, password, invite }); setPassword(''); setInvite(''); setDone(true); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '注册未完成'); } finally { setBusy(false); }
  };
  return <AuthLayout><h2>创建账户</h2><p className="auth-subtitle">使用管理员提供的邀请码加入</p>{done ? <div className="auth-information" role="status"><p>账户已创建，现在可以登录。</p><Link className="auth-submit" to="/login">前往登录</Link></div> : <form onSubmit={submit}>
    <label className="auth-field">用户名<input required minLength={3} maxLength={32} pattern="[A-Za-z0-9_-]+" autoComplete="username" placeholder="3至32位字母、数字或下划线" value={username} onChange={(e) => setUsername(e.target.value)} /></label>
    <label className="auth-field">邮箱（选填）<input type="email" autoComplete="email" maxLength={254} placeholder="用于邮箱登录" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
    <PasswordField value={password} onChange={setPassword} fresh />
    <label className="auth-field">邀请码<input required autoComplete="off" maxLength={128} placeholder="请输入管理员提供的邀请码" value={invite} onChange={(e) => setInvite(e.target.value)} /></label>
    {error && <p className="auth-error" role="alert">{error}</p>}<button className="auth-submit" disabled={busy}>{busy ? '正在创建…' : '创建账户'}</button>
  </form>}<p className="auth-register-link"><Link to="/login">返回登录</Link></p></AuthLayout>;
}

export function AuthInformation({ kind }: { kind: 'forgot' | 'privacy' | 'terms' }) {
  const content = {
    forgot: { title: '重置密码', text: '请联系为你提供账号的管理员。管理员可以在服务器上重置密码；重置后，原有登录会话将失效。当前未提供邮件找回，请勿向他人发送你的密码。' },
    privacy: { title: '隐私政策', text: '本工作台由部署者运营。账号用于隔离对话、研究报告和长期记忆。执行研究时，请求和获授权的材料可能发送到部署者配置的模型、Embedding 与金融数据服务。退出登录不会删除历史数据或停止已启动的研究。数据保留、删除和运营联系方式请向提供账号的管理员确认。' },
    terms: { title: '服务条款', text: '仅使用你有权访问和处理的资料。研究输出需要结合原始来源、数据时间及口径核查，不构成投资收益保证。不得尝试读取其他用户的数据、共享登录凭证或绕过服务限制。服务范围与可用性由部署者提供；有问题请联系提供账号的管理员。' },
  }[kind];
  return <AuthLayout><h2>{content.title}</h2><div className="auth-information"><p>{content.text}</p><Link to="/login">返回登录</Link></div></AuthLayout>;
}
