import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { LiveProvider } from './app/context';
import { AuthProvider, RequireAuth, useAuth } from './app/auth';
import { Login, Register, AuthInformation } from './features/auth/Login';
import { Shell } from './app/Shell';
import { Workspace } from './features/Workspace';
import { Chat } from './features/Chat';
import { ResearchPage } from './features/Research';
import { ReportPage } from './features/Report';
import { SettingsPage } from './features/Settings';
import { EmptyState } from './components/ui';
import './styles/global.css';
import './styles/auth.css';

function WorkspaceScope() {
  const [queryClient] = React.useState(() => new QueryClient({ defaultOptions: { queries: { staleTime: 3000, retry: 1, refetchOnWindowFocus: true } } }));
  React.useEffect(() => () => { void queryClient.cancelQueries(); queryClient.clear(); }, [queryClient]);
  return <QueryClientProvider client={queryClient}><LiveProvider><Shell /></LiveProvider></QueryClientProvider>;
}

function UserWorkspace() {
  const { user, version } = useAuth();
  return <WorkspaceScope key={`${user!.id}:${version}`} />;
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><BrowserRouter><AuthProvider>
    <Routes>
      <Route path="login" element={<Login />} />
      <Route path="register" element={<Register />} />
      <Route path="forgot-password" element={<AuthInformation kind="forgot" />} />
      <Route path="privacy" element={<AuthInformation kind="privacy" />} />
      <Route path="terms" element={<AuthInformation kind="terms" />} />
      <Route element={<RequireAuth />}><Route element={<UserWorkspace />}>
      <Route index element={<Workspace />} />
      <Route path="chat/:sessionId" element={<Chat />} />
      <Route path="research" element={<ResearchPage />} />
      <Route path="research/:researchId" element={<ReportPage />} />
      <Route path="settings" element={<SettingsPage />} />
      <Route path="*" element={<EmptyState title="页面不存在">请从左侧导航返回研究工作台。</EmptyState>} />
    </Route></Route></Routes>
  </AuthProvider></BrowserRouter></React.StrictMode>,
);
