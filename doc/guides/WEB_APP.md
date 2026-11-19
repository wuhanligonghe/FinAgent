# FinSight Web 工作台

2026-11-15 存储更新：当前源码的认证、Web 元数据、研究账本和向量缓存已改用 MySQL，Redis 与文件继续保留。正式迁移和服务切换已完成；初始化、旧库迁移和实测状态见 [MySQL 存储交付记录](../features/MYSQL_STORAGE.md)。下文历史 SQLite 路径按迁移前版本理解。

更新日期：2026-11-05。工作台与报告基于 `image/` 原设计实现，现增加登录页与独立用户目录。

## 定位与边界

React 独立前端 + FastAPI 应用层，直接复用现有 SessionHost、AgentKernel、ResearchService 和 ResearchStore。CLI 入口和研究协议未被替换。

当前支持**账号登录与独立用户工作区**，默认绑定 `127.0.0.1`，保留本机 Host、客户端与 Origin 限制。外部部署需显式配置服务器模式、HTTPS 同源站点和受控反向代理；当前仍为单进程，不代表已完成公网生产验收。首次账号创建、邀请注册、旧数据迁移和部署方法见[登录指南](LOGIN.md)。

## 启动

在项目根目录安装可选后端依赖，已有的 `finagent.toml` 与 `.env` 继续作为模型和数据配置来源：

```bash
.venv/bin/python -m pip install -e '.[web]'
cd web
npm ci
npm run build
cd ..
PYTHONPATH=src .venv/bin/python -m finagent.web --port 8000
```

启动前使用 `python -m finagent auth create-user yourname` 交互创建首个账号。浏览器打开 `http://127.0.0.1:8000` 后登录；公开 `/docs` 和 OpenAPI 已关闭。安装 editable package 后也可以使用 `finsight-web --port 8000`。使用其他配置文件：`--config /path/to/finagent.toml`。

财务分析在线查询三表；本地财报资料库、文件上传和 `/documents` API 已移除。`pdf` 可选依赖仅用于独立的政策材料导入。

开发时分别启动 FastAPI 和前端：

```bash
PYTHONPATH=src .venv/bin/python -m finagent.web
```

另一个终端：

```bash
cd web
npm run dev
```

访问 `http://127.0.0.1:5173`。Vite 代理 `/api` 到8000端口，前端不接触模型密钥。旧代理配置可能仍列出 `/docs` 和 `/openapi.json`，但后端不再开放这些文档接口。自定义后端端口时同步修改 Vite 代理。

### 模型连接与代理

如果出现 `MODEL_UNAVAILABLE / ConnectError`，先检查**后端进程**的网络，而不是浏览器网络。终端或桌面软件可以上网不代表 Python 后端继承了代理变量。

可以在项目 `.env` 中显式配置仅用于模型请求的代理（端口以本机实际代理为准）：

```dotenv
FINAGENT_MODEL_PROXY=http://127.0.0.1:17897
```

或者在 `[model]` 下设置 `proxy_url`。进程环境中的 `FINAGENT_MODEL_PROXY` 优先于 `.env`，再优先于 TOML；未配置专用代理时沿用 HTTPX 默认环境代理行为。修改后必须重启后端，已创建的模型客户端不会自动刷新。

该配置只影响模型适配器，不自动修改金融数据层的网络策略。代理服务自身必须在运行；不能用切换代理来绕过沙箱或服务商访问权限。

金融数据需要独立设置 `FINAGENT_DATA_PROXY`（或 TOML 顶层 `data_proxy_url`），同样需要重启后端；只配置模型代理不会让新浪、腾讯等来源自动使用代理。行业参考排序能力及其行情时间缺口见[行业排名与数据连接修复](../data/INDUSTRY_RANKING.md)。

失败回复会保存脱敏的错误类型、HTTP 状态、网络异常类别和建议，网页可展开“故障诊断”，不再要求在网页输入 CLI 的 `/details`。旧失败记录可能没有结构化诊断，不补造历史错误原因。模型重试次数不等于成功调用次数。

2026-11-02 连接故障实测：原 Web 进程没有代理环境变量，直连返回 `ConnectError`；通过本机代理查询模型列表成功，显式代理配置下实际 Messages 短回复返回“连接正常。”。这是连接验证，不是行情或报告质量验证。原前台进程已保留数据并重启为本机临时用户服务 `finsight-web.service`；运行期间可用 `systemctl --user restart finsight-web.service` 重新加载配置，`journalctl --user -u finsight-web.service -n 50` 查看日志。普通源码启动命令本身不会自动创建此服务。

前端工具链锁定在兼容本机 Node 18 的 Vite 6、Playwright 1.56；新环境建议使用维护中的 Node LTS。依赖版本以 `web/package-lock.json` 为准。

## 页面

| 路由 | 功能 |
| --- | --- |
| `/login`、`/register` | 登录与邀请注册 |
| `/forgot-password`、`/privacy`、`/terms` | 管理员重置说明、隐私与使用说明 |
| `/` | 登录后的研究工作台、问题输入、建议入口、继续研究 |
| `/chat/:sessionId` | 持续对话、真实工具事件、最终答复、独立研究关联 |
| `/research` | 研究列表、筛选、启动、阶段查看、指定研究取消/恢复 |
| `/research/:researchId` | 真实报告、目录、证据抽屉、Markdown 导出、报告追问 |
| `/settings` | 脱敏的运行状态与边界，只读 |

视觉使用暖白、墨绿和统一设计变量，不将生图结果作为整页图片。没有真实资料时展示空状态，不在默认页面注入示例财务数字或研究结论。图表只有示意稿、没有稳定的证据绑定序列合同时暂不渲染。

## 工程结构

```text
web/src/
  app/           路由布局、SSE 与全局提示
  api/           请求封装、展示类型、URL 校验
  components/    输入框、状态、抽屉与错误状态
  features/      工作台、对话、研究、报告、设置
  styles/        设计变量与响应式样式

src/finagent/web/
  app.py              HTTP 路由、本机安全边界、SSE、静态文件
  schemas.py          提交输入合同
  session_manager.py  有界执行队列、会话串行、内核适配、事件投影
  reports.py          报告及证据清单的确定性展示适配
  store.py            Web 元数据、请求幂等、运行记录与事件游标
```

## 执行与数据所有权

- MySQL 认证表保存账号与登录状态；`web_*` 表按认证用户绑定的 namespace 保存会话归属、幂等、执行和事件。短期 runtime 使用按用户数据根目录划分的 Redis 命名空间，research/证据/长期记忆仍置于该用户根目录；旧共享目录不自动向新账号开放。
- Redis 是短期会话检查点、任务和通知的主存储；MySQL `research_*` 表保存研究范围与预算，报告目录保存研究产物。Redis 配置、旧库导入和 AOF 恢复见 [Redis 会话存储](../features/REDIS_RUNTIME.md)。
- Web 不自动暴露 CLI 历史会话和报告，事件展示游标与内核待确认通知分别管理，浏览器已读不代表通知已交付主 Agent。
- 一个进程共享默认 4 个执行线程，待完成提交全局 32/每用户 8，研究全局 3/每用户 1；每用户最多 32 个常驻宿主，应用用户运行上下文上限 64。同会话串行，跨会话受共享配额约束。
- 使用服务器进程租约拒绝第二个 Web worker；使用现有会话租约防止 Web 与 CLI 同时执行同一会话。
- 进程重启后未完成的提交标记为 interrupted，不自动重试结果未知的模型或工具操作。原后台线程不假装跨进程存活。
- 浏览器关闭不取消后台研究；服务进程退出会请求取消并保留已有记录。恢复需要用户明确操作。
- 长期记忆与 CLI 一样遵循 `memory_enabled` / `auto_memory` 配置；按配置用户和项目召回，可通过会话的 `/memories` API 查看和删除。Embedding 配置、存储和故障回退见 [长期记忆与召回](../features/MEMORY.md)。
- “只停止当前回复”只设置当前会话执行的取消信号，不调用会取消全部后台工作的 `SessionHost.cancel()`。
- 网页工具集合限定为金融研究、上下文和报告能力，不开放 Shell、任意文件、子 Agent 的通用工具入口或 MCP。当前没有需要网页审批的写操作，因此尚未实现 `/approvals`。

## 事件与接口

接口前缀 `/api/v1`。提交消息、研究和报告追问必须提供 `Idempotency-Key`；同一会话、同一键重放返回原运行记录，改变请求内容返回409。网络结果未知时，前端保留该键以供重试。

`POST /sessions/{id}/messages` 和 `POST /research` 返回202及 `run_id` 对应的 `id`，不能把202理解为研究已经完成。

主要事件：`run.queued`、`run.started`、`tool.started`、`tool.completed`、`research.started`、`research.progress`、`research.finished`、`message.completed`、`run.failed`。当前取消请求另外发送 `run.cancel_requested`。

`GET /events` 使用持久化递增游标，可传 `after` 或 `Last-Event-ID`，可按 `session_id` 过滤。每15秒发送心跳。事件投影与源游标在同一个 Web 数据库事务中写入，重复扫描不重复插入。源事件是已登记 Web 会话的 RuntimeStore 事件，不能直接把完整模型响应或工具结果推送给浏览器。

现有模型适配器 `stream=False`：当前实时能力是**工具/研究阶段事件**，不是 token 流。最终答复完成后展示；不做假逐字输出、不伪造百分比或预计剩余时间。断线时保留页面数据，自动重连；对话与研究列表还有低频轮询兜底。

报告适配同时支持原始研究报告与 v2.1/v2.2 决策报告的 artifact 标识。保持执行状态、内容完整度、审阅状态和 `outcome_verified` 分离。核心发现摘自已接纳主张，不进行另一次模型摘要；引用使用证据清单的原始定位，无页码时不编造页码。没有精确片段时提供原文入口。

报告追问先校验 Web 归属，在原会话读取授权报告与证据，使用历史范围；该轮禁用独立研究启动工具，避免追问误开第二份研究。超过当前历史截止的报告仍由原内核拒绝采用。

列表会话与消息支持游标分页；当前前端只展示最近会话和最近50轮，研究列表为当前工作区全量。不提供财报上传解析。

## 验证

后端合成测试与既有回归：

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.test_web -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .
```

前端：

```bash
cd web
npm test
npm run build
npx playwright install chromium
npm run test:e2e
```

浏览器测试自动运行 `tests/serve_web_fixture.py`，创建独立临时数据，合成模型不访问真实模型或金融来源。截图保存到被忽略的 `web/test-results/`。生产启动不会加载此测试数据。

沙箱环境可能阻止 TestClient 跨线程事件循环和浏览器监听端口；应在有本机网络权限的环境运行上述集成测试，不应将启动卡住视为测试通过。

本次验证结果：后端完整离线回归591项通过（包含23项 Web 测试），前端4项单元测试通过，7项 Chromium 浏览器测试通过，TypeScript 检查与生产构建通过。浏览器覆盖对话提交和刷新恢复、报告引用及追问、手机导航、报告目录滚动、历史资料上传及连接失败诊断（旧版本记录；资料上传已于2026-11-05移除）；截图检查后将报告与对话输入区改为独立布局，避免覆盖正文。自动回归中的金融内容均为合成测试数据；额外的真实行业问答和银行指数问答只用于验证名称、工具、数据与时间限制链路，没有进行真实研究报告或投资表现验收，见[行业能力说明](../data/INDUSTRY_RANKING.md)和[银行指数口径](../data/INDEX_DATA.md)。

## 后续阶段

尚未实现：多用户身份与存储隔离、独立执行进程、任务队列与横向扩容、真实 token 流、证据绑定图表、PDF 导出、OCR、完整 TypeScript 类型生成。

当前是可运行的本地工作台版本，不是以上能力全部交付；离线和浏览器合成测试也不证明真实报告质量或供应商可用性。
