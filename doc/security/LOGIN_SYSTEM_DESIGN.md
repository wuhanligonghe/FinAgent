# FinSight 登录系统与用户数据隔离开发方案

日期：2026-11-04。适用工程：React / Vite / TypeScript + FastAPI + SessionHost。

**文档状态：第一版已实现。当前代码已接入账号认证、邀请注册、独立用户目录、Cookie/CSRF、跨标签页身份清理、全局资源配额及离线迁移工具。首次启用和管理命令见[登录使用指南](../guides/LOGIN.md)。本文件保留设计依据与原分阶段计划，真实部署和完整业务效果仍按实际验证结果判断。**

当前实现集中在 `web/auth.py`、`auth_store.py`、`auth_cli.py`、`user_runtime.py`、`migration.py`；认证输入合同与路由放在同一个 `auth.py`，未额外拆出原计划中的 `auth_schemas.py`。前端认证页放在 `features/auth/Login.tsx`，同文件包含注册和说明页面。认证始终开启，没有匿名业务兼容开关；React Query 使用每身份独立 QueryClient，而非复用全局缓存。私有 HTTP/SSE 的预期身份标识只用于与真实 Cookie 身份比对，不能授予权限。

## 1. 目标、范围与交付边界

第一版交付：按照现有设计图实现登录页，支持邮箱或用户名登录、记住我、退出、登录恢复、邀请注册、管理员创建/重置账号；使不同用户的对话、任务、研究报告、引用证据、上传资料、记忆、事件和导出产物相互隔离。

第一版采用单进程部署、服务端不透明会话 Cookie、每用户独立数据根目录。SQLite 继续用于账号和业务存储。Milvus、组织共享空间、第三方 OAuth、邮件找回、MFA 和水平扩容分别列为后续工作；登录系统不依赖先部署向量数据库。

“用户”是经过服务端认证的账号，不是浏览器传来的 `user_id`、聊天会话 ID、配置中的 `memory.user_id` 或模型生成的任务字段。第一版以单账号为资源所有者；未来 `tenant_id` 代表组织，`user_id` 代表成员，两者不能提前混为一谈。

## 2. 改造前基线与改造位置

| 当前位置 | 当前行为 | 登录改造要求 |
| --- | --- | --- |
| [web/app.py](../../src/finagent/web/app.py) → `local_boundary()`、各业务路由 | 依赖本机来源边界；工作区和事件没有账号身份 | 保留来源检查，新增认证依赖，所有私有接口绑定当前用户上下文 |
| [web/store.py](../../src/finagent/web/store.py) → `WebStore` | `web.sqlite3` 保存全局 Web 会话、执行记录、事件、资料 ID | 每用户独立实例；查询只能访问本人存储 |
| [web/session_manager.py](../../src/finagent/web/session_manager.py) → `SessionManager` | 单个全局 Store、RuntimeStore、Host 缓存和执行器 | 经当前用户选择 Manager；缓存、锁、后台通知和恢复路径一起隔离 |
| [web/reports.py](../../src/finagent/web/reports.py) → `ReportAdapter.load()` | 只检查报告是否属于某个 Web 会话 | 在本人 Manager 中查归属，再在本人数据根目录读报告与证据 |
| [harness/host.py](../../src/finagent/harness/host.py) → `SessionHost.__init__()` | 数据根来自共享配置；记忆用户来自部署配置 | 每用户深复制配置，绑定不可变账号 ID、数据目录和项目 ID |
| [knowledge.py](../../src/finagent/knowledge.py)、[data/evidence.py](../../src/finagent/data/evidence.py)、[research/store.py](../../src/finagent/research/store.py) | 库、原始证据、规范结果和研究都依赖 `settings.data_dir` | 上传、检索、计算、引用回读必须使用同一个用户根目录 |
| [main.tsx](../../web/src/main.tsx)、[context.tsx](../../web/src/app/context.tsx) | 页面直接进入工作台；LiveProvider 挂载即连接全局 SSE | 登录状态确认前不挂载业务页面、不发业务查询、不建立 SSE |
| [api/client.ts](../../web/src/api/client.ts) | 模块级幂等键 Map，通用错误未暴露状态码 | 增加类型化 401/403、CSRF、取消请求、身份切换时清空幂等键 |

仅给 `sessions` 增加用户列仍不完整：旧的 `ResearchStore(settings.data_dir)`、`EvidenceStore(settings.data_dir)`、全局 SSE、资料检索和宿主缓存都可能绕过会话归属检查。

## 3. 页面设计与交互

视觉依据：[FinSight 金融研究登录页](../../image/FinSight金融研究登录页.png)。该图是设计参考，不把整张截图作为网页背景实现。

### 3.1 布局与样式

- 桌面按约 49% / 51% 左右分栏。左侧深海军蓝与柔和青绿色渐变，右侧接近白色；复用现有 [tokens.css](../../web/src/styles/tokens.css)，新增认证页专用样式，避免影响工作台。
- 左上角保留 FinSight 标识。主标题为“让每一次分析，都有据可循”，副标题为“连接市场数据与原始证据，构建你的研究判断。”；下方用 SVG/CSS 实现市场数据、原始证据、分析节点、研究结论的简化连接图，作为装饰，不能伪装为真实行情。
- 右侧表单最大宽度约 440px，桌面适度居中；标题“欢迎回来”，辅助文字“登录 FinSight，继续你的研究”。输入框约 48–56px，高度和间距按截图比例调整。
- 768px 以下隐藏大型品牌图，显示紧凑标识和全宽表单；支持 360px 窄屏，不产生水平滚动。品牌装饰设 `aria-hidden`。
- 输入项使用真正的 `label`，提供键盘焦点、错误关联、足够对比度；密码显示按钮使用明确的无障碍名称，支持 Enter 提交和密码管理器。账号使用 `autocomplete="username"`，密码使用 `current-password`；允许粘贴密码。

### 3.2 表单与入口

| 元素 | 行为 |
| --- | --- |
| 邮箱或用户名 | 去除前后空白，服务端规范化；不能在前端推断账号是否存在 |
| 密码 | 显示/隐藏只改变输入类型；不得 trim、截断、写入日志或浏览器存储 |
| 记住我 | 默认不勾选，仅改变登录会话有效期，不保存密码 |
| 登录按钮 | 校验后提交；请求中禁用重复提交并显示“登录中…” |
| 账号/密码错误 | 同一文案“账号或密码错误”，不说明账号是否存在、被禁用或尚未注册 |
| 忘记密码？ | 第一版打开管理员重置说明页；没有邮件能力时不能展示“重置邮件已发送” |
| 创建账户 | 第一版进入邀请注册页；邀请码必填，公开注册默认关闭。邀请功能未交付时必须隐藏入口，不做空链接 |
| 隐私政策 / 服务条款 | 连接可阅读的实际页面；上线前补齐站点身份、数据处理和联系信息，不使用占位链接 |

### 3.3 前端路由与状态

公开路由：`/login`、`/register`（邀请模式）、`/forgot-password`、`/privacy`、`/terms`。其余工作台路由放到 `RequireAuth` 内；`LiveProvider` 只包裹认证后的业务区域。

`AuthProvider` 状态为 `checking / authenticated / anonymous`。首次打开调用 `/api/v1/auth/me`；`checking` 显示中性加载态，不能先展示上一个用户的工作区。登录成功后只接受站内路径作为 `return_to`，拒绝协议、`//`、反斜杠和经过编码的外部跳转；默认返回 `/`。后端仍独立鉴权。

账号切换或 401 时：先卸载业务区域并关闭 SSE，取消所有旧身份请求，清空 React Query、事件数组、通知、幂等键 Map 和旧游标，再建立新身份上下文。每次身份变化增加 `auth_epoch`，旧请求和旧 SSE 回调的 epoch 不符时丢弃，防止慢响应把 A 的内容写入 B 的页面。查询键和游标包含账号 ID，例如 `['user', user.id, 'sessions']`、`finsight:<user_id>:event-cursor`。

同一浏览器多个标签页共享登录 Cookie，因此切换账号影响所有标签页。使用 `BroadcastChannel` 广播失效通知；收到通知时重新请求 `/auth/me`，不能信任广播里的用户对象。未收到广播的页面在重新获得焦点时也复查身份。不要把会话令牌放在 localStorage、sessionStorage 或 URL。

## 4. 认证方案与会话生命周期

### 4.1 服务端会话

采用随机不透明令牌，而非浏览器持有 JWT。令牌使用系统安全随机源生成至少 32 字节；浏览器仅持有 Cookie，数据库仅保存令牌 SHA-256 摘要。令牌是高熵随机值，使用 SHA-256 生成查找摘要；用户密码必须使用慢哈希，两者不能混用。

生产 Cookie 示例：

```http
Set-Cookie: __Host-finsight_session=<opaque-token>; Path=/; HttpOnly; Secure; SameSite=Lax
```

不设置 `Domain`。本机 HTTP 开发使用不同名称 `finsight_dev_session`，只允许绑定 loopback 且显式开启开发模式；生产必须 HTTPS，不能通过开关把公网 Cookie 降级为不安全模式。页面/API 不缓存身份与业务响应。

| 模式 | 浏览器 Cookie | 服务端空闲超时 | 服务端绝对上限 |
| --- | --- | --- | --- |
| 未勾选记住我 | 会话 Cookie，无 Max-Age | 2 小时 | 12 小时 |
| 勾选记住我 | 持久 Cookie，Max-Age 30 天 | 7 天 | 30 天 |

以上为第一版可配置产品默认值；服务器是有效期权威，浏览器恢复会话不能延长绝对上限。更新 `last_seen_at` 应限频，避免每个 SSE 心跳写库；SSE 心跳不算用户活动。登录成功更换会话令牌并撤销匿名预会话，防止固定会话攻击。

退出撤销当前登录会话并删除同名、同 Path Cookie；修改密码和禁用账号撤销该用户全部登录会话。登出不删除长期数据，已经授权启动的研究默认继续，仍属于原用户；账号被禁用时停止接受请求，并取消/暂停其后台执行。上述后台策略必须在 UI 和测试中一致。

### 4.2 密码和防爆破

使用维护中的 Argon2id 库，如 `argon2-cffi`，在实现阶段加入明确依赖。参数不得低于 OWASP 建议的 19MiB、2 次迭代、并行度 1，并在部署机器测试开销。密码建议最少 15 字符、最大 128 字符，允许空格和 Unicode，不设机械的特殊字符组合规则，不静默截断；身份字段使用独立、明确的规范化规则。

用户名规范化为小写 ASCII（3–32 位字母、数字、下划线、连字符，不能含 `@`）；邮箱使用经库校验的、业务约定大小写不敏感的登录键。显示名称单独保存。用户名和邮箱各有唯一索引，避免同一输入匹配两名用户。密码哈希包含盐和参数，验证成功后可按策略升级哈希。

不存在的账号也执行固定 dummy 哈希验证；对错误、禁用账号统一响应。登录按来源 IP 与规范化登录标识分别限流，并发哈希也有全局上限，防止内存耗尽。第一版采用账号 15 分钟窗口内最多 5 次失败、每 IP 每分钟 30 次的限流，成功登录清除该账号失败计数，限流返回 429 与 Retry-After；部署压测后调整，避免永久锁号成为拒绝服务手段。限流状态保存在共享认证库；多进程时不能仅用进程内 Map。

### 4.3 CSRF、来源与日志

所有业务写操作以及登录、邀请注册、退出均验证来源和 CSRF。`GET /auth/csrf` 为当前会话返回同步令牌，尚未登录时建立 10 分钟有效的匿名预会话，并限制创建频率、定期清理；该匿名会话没有用户权限。登录后轮换登录令牌与 CSRF 令牌，前端只在内存保存 CSRF，写请求发送 `X-CSRF-Token`。服务端绑定会话验证，不能只比较两个任意可写 Cookie。GET/HEAD 不执行业务修改。

沿用并改造 Host/Origin/Fetch-Metadata 检查。开发走 Vite 同源代理；部署通过明确的允许主机和站点 Origin 配置支持域名，不是直接删除 `local_boundary()`。只信任明确配置的反向代理，不能根据任意客户端的 Forwarded/X-Forwarded-Host 决定可信 Origin。SameSite 是补充防线，不代替 CSRF。

本机模式继续要求 loopback 客户端；显式服务器模式才允许外部客户端，并使用部署配置中的 Host/Origin、HTTPS 与代理信任规则，不再把“客户端必须为 loopback”错误地当作远程账号的授权条件。两种模式都需要账号认证，不能因请求来自 localhost 或可信代理而跳过登录。

请求体、异常和审计日志都不得保存密码、Cookie、登录/CSRF/邀请令牌、模型密钥。认证和限流发生在 Agent 调用之前；密码永远不进入 SessionHost、任务来源、长期记忆、Embedding 或模型上下文。

## 5. 数据模型与 API 合同

以下为第一版已落地的认证表与接口合同。`expires_at` 使用 UTC Unix 秒；部署配置和操作入口见使用指南。

### 5.1 全局认证库

位置：`<base_data_dir>/auth/auth.sqlite3`。第一版使用 SQLite 外键、迁移版本和事务；生产多实例再迁移共享事务数据库。

| 表 | 主要字段与约束 |
| --- | --- |
| `users` | `id` 不可变 `usr_<32hex>` 主键；`username_key`、可空 `email_key` 分别唯一；`display_name`、`password_hash`、`status`、`auth_version`、创建/更新时间 |
| `auth_sessions` | `token_hash` 主键、可空 `user_id` 外键（匿名预会话为空）、`csrf_hash`、`auth_version`、`remember`、`created_at`、`last_seen_at`、`expires_at`、`revoked_at`；对用户及有效期建索引 |
| `invites` | 高熵邀请码摘要、可选绑定邮箱、到期时间、创建者、消费人和消费时间；消费与创建用户同一事务，单次使用 |
| `auth_audit` | 操作类型、操作者/目标账号、结果、request_id、时间；最小化来源信息，不含认证秘密和业务正文 |
| `schema_migrations` | 迁移版本、校验信息和执行时间 |

账号 ID 由服务端生成，不使用可变用户名或邮箱作为路径。普通用户不通过 API 获得管理员权限；账号维护由受控服务器管理命令执行，首版不开放“管理员可以随意浏览所有研究”的隐式能力。

### 5.2 认证 API

| 方法与路径 | 身份 / 输入 | 成功结果 |
| --- | --- | --- |
| `GET /api/v1/auth/csrf` | 登录或短期匿名预会话；严格同源 | `{csrf_token}`，必要时设置预会话 Cookie，`no-store` |
| `POST /api/v1/auth/login` | 匿名预会话 + CSRF；`identifier,password,remember` | 200，用户对象和新 CSRF，设置新的登录 Cookie |
| `GET /api/v1/auth/me` | 有效登录 Cookie | 200，`{user:{id,username,display_name},expires_at}` |
| `POST /api/v1/auth/logout` | 登录 + CSRF | 204，无正文，撤销会话并清 Cookie |
| `POST /api/v1/auth/register` | 仅邀请模式；预会话 + CSRF；账号信息、密码、邀请码 | 201，仅创建用户，跳转登录；邀请码不入日志 |

未登录或会话失效返回 JSON 401；CSRF/来源/功能权限不满足返回 403；已登录但资源不属于本人时，统一 404，避免暴露其他用户的资源存在性；限流 429；正常字段校验 422。错误体沿用 `{code,message,request_id}`，认证错误不能回显输入值。前端 HTTP 客户端需支持 204，不强行解析为业务 JSON。

`/health` 可匿名但仅返回最小健康信息；静态登录页和资源公开。`/settings`、工作区、聊天、研究、资料、记忆、事件、导出，以及未来调试接口均默认需要登录。生产 `/docs`、`/openapi.json` 应关闭或认证访问。认证接口不使用业务 Idempotency-Key Map，注册通过邀请码唯一消费及数据库唯一约束处理重复请求。

## 6. 用户数据与运行时隔离

### 6.1 第一版采用每用户独立根目录

```text
<base_data_dir>/
├── auth/auth.sqlite3
├── users/
│   ├── usr_<A>/
│   │   ├── web/web.sqlite3
│   │   ├── web/uploads/                  # 临时上传，用后清理
│   │   ├── v3/runtime.sqlite3
│   │   ├── v3/sessions/<chat_session_id>/
│   │   ├── v3/memory/users/usr_<A>/       # Qwen 向量缓存与记忆
│   │   ├── research.sqlite3
│   │   ├── research/<research_id>/
│   │   ├── library/
│   │   ├── evidence/
│   │   └── results/
│   └── usr_<B>/...
└── migration/                            # 迁移映射、状态和校验信息
```

请求经 `get_current_user()` 得到可信账号，再由 `UserRuntimeRegistry.for_user(user.id)` 取得 `UserContext(settings, store, manager, reports)`。派生设置必须使用深复制：`data_dir = base/users/user.id`、`memory.user_id = user.id`，项目 ID 按可信默认项目配置绑定；不修改全局 Settings，不接受请求体中的根目录或用户 ID。解析路径后检查仍位于预定用户根下，拒绝符号链接逃逸。

Registry 缓存键是不可变账号 ID。一个账号的 `SessionManager` 只能持有该账号 Store、RuntimeStore、Host 和 ReportAdapter；Manager 内部按聊天会话串行。显式把 `owner_user_id` 放入宿主可信元数据和后台工作资源，恢复时验证与当前根一致，避免复制目录后误认归属。

当前 Web 工具白名单继续保留，文件系统、Shell、MCP 不因登录而开放。授权工作目录不是隔离机制；模型和技能不能修改 owner、目录或登录身份。

### 6.2 每条读取与修改路径都绑定同一上下文

| 资源/动作 | 必须使用的范围 |
| --- | --- |
| 工作区、会话列表、消息、执行状态与取消 | 本人 WebStore 与 Manager，未知 ID 返回 404 |
| 研究发起、列表、状态、恢复、取消、追问 | 本人 Manager 查原会话归属；ResearchService 使用本人 Settings |
| 报告、导出、引用与原始片段 | 本人 ReportAdapter、ResearchStore、EvidenceStore；不能授权检查后回到全局路径读取 |
| 财务数据与证据 | 用户目录内的在线三表结果及 EvidenceStore；本地财报资料库与上传接口已移除 |
| 记忆写入、召回、删除与向量缓存 | 本人 memory.user_id + 可信 project_id；在 Embedding 和重排之前完成范围过滤 |
| SSE 与轮询兜底 | 本人事件表；指定 session_id 时也只检查本人会话；游标只影响位置，不能选择身份 |
| 缓存、错误、下载链接和诊断 | 不复用带私人正文的跨用户缓存；保持 no-store，不能在错误中泄露其他用户资源或磁盘路径 |

即使 A 把真实报告 ID、result_id、原始证据 ID 或 document_id 发给 B，B 的自然语言请求、工具调用和 HTTP 路由也不能读取 A 的数据。随机 ID 只是标识，不能当作访问凭证。

首版不自动把所有公开来源资料放入共享库；用户上传仍是私人数据。以后共享金融原文时引入显式 public corpus 和授予记录，不能通过“来源网址公开”自动提升权限。

### 6.3 后台任务、SSE 与资源配额

线程池工作项捕获提交时的不可变用户上下文，不读取可变全局“当前用户”。通知、回调、计划任务和恢复任务沿用任务 owner；浏览器登录另一账号不能改变旧研究的归属。

当前 Manager 各自创建四线程、三研究并发和宿主上限，不能在多用户改造时照搬成“每用户一套无限资源”。引入应用级共享执行器与全局研究信号量，并保留每用户队列/研究上限；Manager 关闭时不能误关闭共享执行器。可从全局四个前台工作线程、三份研究以及每用户一份研究开始压测，配额配置集中管理。

服务启动持有应用级进程租约，恢复所有已登记用户的排队/运行记录；不能只恢复最后登录用户。原进程丢失的研究标为 interrupted，不自动重放。注册表中无活跃任务的用户可清理空闲 Host/Manager；有执行中的研究不得因登出、LRU 或另一个账号登录被关闭。

SSE 建连时认证并捕获用户与登录会话；循环中定期检查撤销/到期/禁用，建议不超过 5 秒，并在每批发送前检查，失效后关闭连接。不能只在握手时检查，导致登出后旧连接持续泄露事件。跨标签页 Cookie 变化不会改变已有流身份；前端身份广播和服务端复查必须共同生效。自动重连失败后前端通过 `/auth/me` 判断是否回登录，不把令牌写进 SSE URL。

退出保留账号数据和已启动后台研究；服务端撤销登录凭证后，旧连接不能继续读取。用户重新登录后只恢复本人的游标和工作列表。

### 6.4 后续多实例与向量数据库

目录方案用于先落地清晰的隔离边界，不声称支持多进程横向扩容。规模增长后，用户/会话/权限/任务元数据迁移到 PostgreSQL，原文与产物迁移对象存储，执行器替换为独立 Worker/队列，再按真实负载选择 pgvector 或 Milvus。

Milvus 是检索索引，不接管认证。共享 collection 必须由服务端附加 `tenant_id/user_id/project_id/visibility/model_version` 过滤；BM25、向量召回和原文回读保持同一授权集合，不能全库 Top-K 后再简单丢弃越权命中。更强隔离需求可以使用独立 collection/database；partition key 本身不是认证。详见[记忆存储的后续设计](../features/MEMORY.md#多用户大语料的后续设计尚未实现)。

## 7. 旧数据迁移与回滚

1. 维护窗口停止写入，确认无运行中的研究，或先将其明确标为 interrupted；创建完整可恢复备份。数据库使用 SQLite backup API 取得包含已提交 WAL 的一致副本，不能只复制 `.sqlite3` 主文件。
2. 管理员显式选择唯一的历史数据接收账号，保存迁移 owner 映射。旧数据不得自动归属于第一个登录、注册或访问的用户。
3. 迁移 WebStore、RuntimeStore、研究库和产物、资料库、证据、规范结果及旧记忆；业务 ID 尽量保持不变。将旧 `memory.user_id=local` 和会话 memory_scope 映射到目标账号，并迁移记忆目录中的用户字段和索引，不能靠忽略 `MEMORY_SCOPE_MISMATCH` 恢复会话。
4. 保存旧项目映射；检查会话、研究、原文引用和归档中的绝对路径、工作目录与 root 字段，按结构化字段修复。不能把整段用户文本做盲目字符串替换。向量缓存可重建；需要保留的证据 ID 和时间戳不能重新生成。
5. 临时迁移目录通过 SQLite integrity/foreign-key 检查、数量核对、文件摘要、报告引用回读、记忆范围和两账号越权测试后，再切换配置。迁移步骤带版本、清单和完成标记，重复执行不得重复归属或覆盖用户新数据。
6. 上线前保留旧布局只读备份；失败时在停写状态回退到迁移前快照。已有新布局写入时先导出并核对新增数据，不能用回滚静默丢弃它们。旧匿名接口和旧共享根目录不能作为“兼容兜底”继续对 Web 开放。

CLI 默认仍是本机操作员入口，和 Web 账号权限分开。未来如需 CLI 操作某账号数据，应通过受控管理命令明确选择目标和认证身份，不能把任意 `--user-id` 视为授权。迁移工具先提供 dry-run、冲突清单和离线验证，实际执行另行安排。

## 8. 文件改造清单与实施顺序

| 阶段 | 新增/修改位置 | 完成条件 |
| --- | --- | --- |
| P0：认证基础 | 拟新增 `web/auth.py`、`web/auth_store.py`、`web/auth_schemas.py`；修改 `config.py`、CLI 和依赖 | 账号创建、Argon2id、会话撤销、CSRF、限流、邀请消费均有确定合同；不影响 CLI 普通聊天 |
| P1：用户上下文 | 拟新增 `web/user_runtime.py`；修改 `app.py`、`session_manager.py`、`reports.py` | 所有私有路由、工具、Store、Host、后台恢复和证据读取均绑定同一用户；全局资源配额仍有效 |
| P2：前端 | 拟新增 `features/auth/Login.tsx`、`Register.tsx`、`ForgotPassword.tsx`、`app/auth.tsx`、认证样式；修改 `main.tsx`、`client.ts`、`context.tsx`、`Shell.tsx` | 设计图布局、认证守卫、退出入口、401 清理、账号切换和 SSE 生命周期完整 |
| P3：迁移与验收 | 拟新增迁移脚本和认证/隔离测试，更新部署指南 | 旧数据可控归属，两用户完整隔离，可重复升级和回滚；生产 HTTPS/Origin 配置可验证 |

后端隔离验收必须先于把新 UI 宣称为“多用户系统”。可以分阶段开发，但正式启用认证模式时默认拒绝未登录业务访问，禁止保留隐藏匿名旁路。每阶段的数据库迁移和 API 变化单独记录。

拟新增配置需集中定义：`auth.enabled`、部署模式、公开 Origin/Host、Cookie 策略、会话 TTL、邀请注册开关、全局/每用户配额、认证库与用户根目录。示例不能包含真实密码或固定默认账号；生产启动应验证必需配置，首个账号由部署者通过交互式命令创建，密码不出现在 shell 参数和历史中。第一版的管理权限来自服务器操作权限，不把普通账号自动提升为可跨用户访问数据的管理员。

## 9. 验收矩阵

以下是登录功能的验收矩阵。已实现测试位于 `tests/test_auth.py`、更新后的 `tests/test_web.py`、`web/src/api/client.test.ts` 和 `web/e2e/auth.spec.ts`；矩阵是完整目标集合，不能把它的每一行都视为已完成生产环境验收。实际通过数量与限制在使用指南记录。

| 主题 | 必测场景与预期 |
| --- | --- |
| 登录与注册 | 正确/错误/不存在/禁用账号；重复用户名/邮箱；邀请过期、重放、并发消费；不产生账号枚举提示 |
| 会话 | 记住我、普通会话、空闲/绝对到期、服务重启、退出、改密、禁用；固定会话令牌失效 |
| CSRF/来源 | 登录、注册、退出、上传、聊天、研究取消、记忆删除均拒绝无效 CSRF；恶意 Origin、Host、代理头不能绕过 |
| A/B 资源隔离 | A 建会话、上传、启动研究、写记忆；B 将所有真实 ID 用于列表、详情、取消、恢复、追问、导出、引用、工具回读；均不能获得 A 的内容或改变 A 状态 |
| SSE | 无 session_id 也只返回本人事件；指定 A 的 session_id、使用 A 游标、连接后退出/到期/禁用均不泄露数据 |
| 前端切换 | A→退出→B；双标签页切换；旧 fetch/旧 SSE 延迟到达；后退缓存；不闪现 A 的页面、通知、文档或图表 |
| 后台任务 | A 研究进行中登录 B，通知仍回 A；全局/每用户额度不能相乘绕过；服务重启恢复所有用户且不自动重复运行 |
| 记忆与检索 | 同一项目名、同证券、同文档摘要或相似问题都不能跨用户；Embedding 请求和重排候选中没有外用户正文 |
| 迁移 | 未指定接收人拒绝执行；重复执行不覆盖；旧报告/引用/原文/记忆可恢复；失败可回滚且新增数据有处理方案 |
| 页面 | 对照设计图核查桌面、768px、360px；键盘、输入提示、密码管理器、加载态、错误态、真实辅助入口 |

后端使用两个独立 Cookie 客户端测试，不能只在同一 TestClient 内替换变量模拟两个用户。浏览器端用两个独立 context 验证隔离，再用同一 context 的多标签页验证账号切换。验证包括原有 Python 回归、前端单测/构建及认证 E2E；通过数以实施时实际结果为准。

## 10. 参考与关联文档

- [Web 工作台](../guides/WEB_APP.md)、[整体项目设计](../overview/PROJECT_DESIGN.md)、[长期记忆](../features/MEMORY.md)、[注入与泄露防护](PROMPT_INJECTION_DEFENSE.md)。
- [OWASP 密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)：Argon2id、盐与工作因子。
- [OWASP 会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)：Cookie、令牌、会话轮换与失效。
- [OWASP CSRF 防护](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)：同步令牌与来源校验。
- [OWASP 授权](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)：每次请求验证权限、默认拒绝和资源级检查。

这些资料用于确定安全机制，本文中的 TTL、配额、目录布局和开发阶段属于本项目设计选择，不是官方服务容量或合规结论。
