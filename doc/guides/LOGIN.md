# 登录与独立用户工作区

更新日期：2026-11-04。当前已实现第一版账号登录、邀请注册、独立用户目录和服务端会话。设计依据及安全边界见[开发方案](../security/LOGIN_SYSTEM_DESIGN.md)。CLI 普通聊天仍使用自己的操作员配置，不需要网页登录。

短期会话检查点、任务和通知现以 Redis 为主存储，启动 Web 前须准备 Redis；已有 `runtime.sqlite3` 先停写导入。配置、服务启动与迁移见 [Redis 短期记忆](../features/REDIS_RUNTIME.md)。认证库和 Web 元数据仍使用下面的独立 SQLite，不属于运行库替换范围。

## 首次启用

安装 `.[web]`，构建前端后，在项目根目录创建账号。密码在终端交互输入两次，不能作为命令参数、环境变量或聊天消息发送：

```bash
.venv/bin/python -m pip install -e '.[web,pdf]'
.venv/bin/python -m finagent auth create-user yourname --email yourname@example.com
cd web
npm ci
npm run build
cd ..
.venv/bin/python -m finagent.web --port 8000
```

将示例用户名和邮箱换成实际信息；邮箱选填，不发送验证邮件。登录页面在 `http://127.0.0.1:8000/login`，也可以从首页自动跳转。已有 Web 进程需要重启；重启前先完成或取消运行中的任务，不能用重启假装恢复旧线程。

服务没有固定默认账号，也不会将历史数据交给第一个注册/登录的人。登录后工作区为空可能是正常现象；旧数据迁移见下文。

## 账号管理

以下命令由有权访问服务器数据目录的部署者执行，使用同一份配置文件；Web 普通账号没有跨用户管理接口。

```bash
.venv/bin/python -m finagent auth list-users
.venv/bin/python -m finagent auth invite --email invited@example.com --hours 48
.venv/bin/python -m finagent auth reset-password usr_ACCOUNT_ID
.venv/bin/python -m finagent auth disable usr_ACCOUNT_ID
.venv/bin/python -m finagent auth enable usr_ACCOUNT_ID
```

`usr_ACCOUNT_ID` 替换为 `list-users` 返回的不可变账号 ID。`invite` 输出单次邀请码，请通过受信任渠道交付；注册页需要该邀请码。邮箱绑定的邀请只能用于对应邮箱。`[auth].registration="closed"` 时关闭注册，登录页隐藏创建账户入口。忘记密码页面解释管理员重置流程，不伪造邮件发送。

重置密码、禁用或重新启用账号都会撤销已有登录会话。退出仅撤销当前会话，保留研究与数据；禁用账号后，服务定期取消该账号的前台与后台执行。

## 会话和存储

普通登录：浏览器会话 Cookie，服务端空闲 2 小时、绝对 12 小时。记住我：持久 Cookie，空闲 7 天、绝对 30 天。有效期由服务端校验；SSE 心跳不延长空闲时间。认证令牌不存入 localStorage 或 URL，密码不进入 Agent、记忆或 Embedding。

全局认证库为 `.finagent/auth/auth.sqlite3`；每个账号的 Web、资料、原始证据、报告、归档和长期记忆位于 `.finagent/users/usr_<ID>/`，运行检查点/任务/通知的 Redis 命名空间由该账号 `v3/` 根目录派生。`memory.user_id` 对 CLI 仍是配置值，对 Web 则由服务端账号强制替换。用户目录带 owner 标记，不能通过浏览器选择其他账号或根目录。

前端为每个登录身份建立独立 QueryClient，身份变化后销毁旧缓存、请求、幂等键与 SSE；游标按用户命名。私有请求还携带预期身份标识，服务端只用于检查其与 Cookie 身份是否一致，不能据此授予身份。这避免多标签页刚切换 Cookie、尚未收到广播时读入错误账号的数据。

## 旧数据迁移

迁移是部署者的显式维护操作，不自动执行。先停止新旧 Web 服务及其他会写入原目录的 CLI，确认旧队列和研究已结束。创建一个尚未使用的新账号，拿到 `usr_<ID>`，然后：

```bash
.venv/bin/python -m finagent auth migrate-legacy --owner usr_ACCOUNT_ID
.venv/bin/python -m finagent auth migrate-legacy --owner usr_ACCOUNT_ID --apply
```

第一条只输出计划；第二条才创建一致备份、迁移副本并切换目标目录。工具同时检查新旧 Web 进程租约和运行中的任务，拒绝覆盖已有用户数据。保留的业务 SQLite 使用 backup API 包含已提交 WAL 页，Redis 运行数据额外导出原子快照并导入目标账号命名空间；修复结构化目录引用及记忆 owner，原用户文字和原始证据 ID 保留。

备份及迁移清单位于 `.finagent/migration/`。源目录保留，完成后重复执行不会覆盖用户新增内容。若发现异常，停写并保存当前新目录及新增数据，按清单核对后恢复备份或迁移前版本；不能直接删除新数据回到匿名共享服务。不要手工复制某个用户目录给另一个账号。

历史归档消息中的原始路径文字保留；回读时只允许映射到该账号当前会话的迁移副本，不读取旧共享根目录。其他会话、认证库和目录外文件仍拒绝访问。

## 同源服务器部署

默认 `mode="local"` 仅接受 loopback 客户端，并且同样需要登录。对外部署时，在配置文件中显式设置：

```toml
[auth]
mode = "server"
allowed_hosts = ["research.example.com"]
allowed_origins = ["https://research.example.com"]
registration = "invite"
workers = 4
research_global = 3
research_per_user = 1
queue_global = 32
queue_per_user = 8
```

使用实际域名替换示例。在受控 HTTPS 反向代理后运行 `python -m finagent.web --host 0.0.0.0`，直接后端端口应受网络访问规则保护；代理保留正确 Host。应用不信任任意 Forwarded/X-Forwarded-* 来改变身份、源或 IP。前端与 API 保持同源，不设置通配 CORS；生产 Cookie 为 `__Host-finsight_session`，带 Secure/HttpOnly/SameSite，开发 Cookie 使用独立名称。生产没有公开 OpenAPI 接口。

当前只支持单进程，服务端租约禁止多个实例同时接管同一个根目录。全局线程池、研究和队列配额不会按用户数成倍扩大。更多实例、组织共享、邮件找回、MFA、Milvus 与对象存储是后续扩展，不属于本次已交付范围。

## 验证

```bash
.venv/bin/python -m unittest tests.test_auth tests.test_web -q
cd web
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

测试账号和研究资料均为临时合成数据。浏览器验收需要对应版本 Chromium；截图输出在 `web/test-results/`。真实金融服务与生产 HTTPS 部署需要在目标环境单独验证。

2026-11-04 本次交付验证：Python 全量 715 项通过（64.255 秒）；前端单测 8 项通过，生产构建通过；Chromium 浏览器验收 11 项通过（8.8 秒），包括桌面/360px 登录页、错误密码、记住我、退出、不同浏览器账号隔离、同浏览器多标签退出及既有聊天/报告/上传/诊断流程。最后的本机模式与公开导航校验调整后，认证/Web/启动相关 44 项再次通过；历史归档迁移与 Harness 相关 46 项通过。

全量回归曾有一次研究交接测试将 `complete` 返回为 `partial`，原因未确认；单项复现及后续全量重跑通过。未放宽其完成断言，只补充失败任务/阶段诊断。以上结果不构成连续运行稳定性或真实金融报告质量结论。

本次没有创建部署用账号，也没有替历史数据选择接收人；账号密码应由部署者通过交互命令设置，历史归属需显式迁移。桌面/手机截图分别为 `web/test-results/login-desktop.png` 与 `web/test-results/login-mobile.png`。
