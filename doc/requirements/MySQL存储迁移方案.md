# FinSight / FinAgent：SQLite 业务存储迁移至 MySQL

日期：2026-11-15。状态：存储代码、迁移工具、正式数据导入和服务切换均已完成并验证。实际交付状态以 [MySQL 运行与验收记录](../features/MYSQL_STORAGE.md) 为准。

[返回需求索引](README.md) · [当前项目设计](../overview/PROJECT_DESIGN.md) · [Redis 运行存储](../features/REDIS_RUNTIME.md)

## 1. 目标与范围

将当前由 SQLite 保存的账号认证、Web 元数据、研究账本和记忆向量缓存全部迁入 MySQL。迁移后，这四部分在正常运行中只读写 MySQL；SQLite 仅用于离线读取旧数据。已有用户、历史会话、研究预算、事件游标和记忆归属需要保留。

目标存储分工：

| 存储 | 负责的数据 |
| --- | --- |
| MySQL | 账号、登录会话、邀请与审计；Web 会话、执行记录、展示事件与游标；研究任务、预算、授权与执行选择；记忆向量缓存 |
| Redis | Agent 会话检查点、运行任务、有序事件和待确认通知，沿用现有运行机制 |
| 本地文件 | 记忆 JSON 正文与索引、transcript、证据、研究产物与报告、归档及本机租约 |

这次改造保留现有 API、研究规则和本地文件布局。跨 MySQL、Redis、文件及外部模型调用仍没有统一事务；部署仍遵守当前单进程 Web 与本机文件锁约束。接入 MySQL 后，需要另行设计才能支持多实例执行。

## 2. 代码现状与迁移清单

以下为本次源码核对结果，不代表已经完成改造。

| 当前数据库 | 现有表 | 拥有代码 |
| --- | --- | --- |
| `auth/auth.sqlite3` | `users`、`auth_sessions`、`invites`、`auth_audit`、`auth_limits`、`auth_meta`、`schema_migrations` | [web/auth_store.py](../../src/finagent/web/auth_store.py) |
| 每个工作区的 `web/web.sqlite3` | `sessions`、`runs`、`events`、`cursors` | [web/store.py](../../src/finagent/web/store.py) |
| 每个工作区的 `research.sqlite3` | `runs`、`tasks`、`reservations`、`grants`、`events`、`stage_states` | [research/store.py](../../src/finagent/research/store.py) |
| 同一个研究库中的延迟建表 | `execution_selections` | [research/selection.py](../../src/finagent/research/selection.py) |
| 记忆用户目录内的 `embeddings.sqlite3` | `vectors` | [harness/memory_search.py](../../src/finagent/harness/memory_search.py)、[harness/memory.py](../../src/finagent/harness/memory.py) |

需要一起处理的耦合：

- `AuthStore.db()`、`WebStore.db()` 和研究写事务使用 `BEGIN IMMEDIATE`。并发正确性不能依靠替换 SQL 字符串保留。
- [research/budget.py](../../src/finagent/research/budget.py)、`research/selection.py` 以及 [web/user_runtime.py](../../src/finagent/web/user_runtime.py) 直接访问 Store 的数据库连接。迁移时一起收口。
- `UserRuntimeRegistry.start()` 依赖 `web.sqlite3` 是否存在来发现需要恢复的用户工作区。
- 研究任务和阶段按 SQLite `rowid` 排序，需要显式保存旧顺序。
- [web/migration.py](../../src/finagent/web/migration.py) 同时处理账号归属、目录、SQLite 和 Redis；不能在切库后继续把数据库文件复制当作业务数据迁移。
- [harness/runtime_migration.py](../../src/finagent/harness/runtime_migration.py) 导入历史运行库时也会读取旧 Web 数据。此类历史读取保留为离线兼容，不进入新运行路径。
- 原测试中存在直接连接 SQLite、检查 `.sqlite3` 文件、使用临时目录代表独立数据库的假设。

## 3. 技术路线

采用 MySQL 8.4、InnoDB、`utf8mb4`，通过同步 SQLAlchemy Core + PyMySQL 访问，使用 Alembic 管理数据库结构版本。具体依赖版本在实施时固定并通过实际 MySQL 回归。

选择 Core 的原因是当前 Store 接口较薄，业务层大量使用字典；保留这种接口比同时引入完整 ORM、对象关系映射和异步数据库改造更容易核对行为。SQLAlchemy 提供 MySQL 方言及 PyMySQL 接入，参考[官方 MySQL 方言文档](https://docs.sqlalchemy.org/en/20/dialects/mysql.html#module-sqlalchemy.dialects.mysql.pymysql)。数据库结构变更使用 [Alembic 版本迁移](https://alembic.sqlalchemy.org/en/latest/tutorial.html)。

每个部署使用独立的 MySQL database，例如 `finagent`。开发、测试和正式部署使用不同 database 和账号。本方案不支持互不信任的多个部署共用同一组业务表。

### 3.1 新增的存储基础层

建议目录如下，名称为拟新增项：

```text
src/finagent/storage/
    database.py           Engine、连接池、读连接、事务和错误转换
    scope.py              工作区注册与服务端身份绑定
    schema.py             SQLAlchemy Core 表定义
    migration.py          旧 SQLite 导入、核对和迁移批次
migrations/
    env.py
    versions/             Alembic 结构版本
scripts/
    migrate_sqlite_to_mysql.py
```

全进程共用一个 Engine 和有上限的连接池；用户 Store 持有工作区范围，不单独创建 Engine。Web 退出、CLI 退出和测试结束时释放连接池。同步数据库操作继续在现有同步函数或工作线程中执行；对现有 `async` 路由逐项检查，避免把阻塞查询直接放进事件循环。

运行账号只获得业务读写权限；建表、索引与结构升级由维护账号在启动前执行。移除各 Store 构造函数和 `selection._table()` 中的运行时 DDL。MySQL 多种 DDL 会隐式提交，不能把运行时建表放进业务事务并期待整体回滚，见[官方说明](https://dev.mysql.com/doc/refman/8.4/en/implicit-commit.html)。

### 3.2 Store 接口

保留 `AuthStore`、`WebStore`、`ResearchStore` 和向量缓存对外的业务方法及返回格式；构造时显式注入 `Database` 与可信 `StorageScope`。`ResearchStore.root` 等路径仍用于定位文件，不再用于打开数据库。

将外部直接 SQL 移入相应仓储方法：

| 调用点 | 拟收口接口 |
| --- | --- |
| `UserRuntimeRegistry.enqueue()` | `WebStore.has_run()` / `create_run()` |
| `ResearchBudget.reserve()` / `settle()` | `ResearchStore.reserve_budget()` / `settle_reservation()` |
| `selection.py` 的建表、消费和绑定 | `create_selection()`、`consume_selection()`、`bind_selection()`、`get_selection()` |

不提供把 SQLite SQL 自动替换成 MySQL SQL 的通用包装器。参数使用 Core 绑定参数或 `text()` 命名参数；返回行统一转为 mapping / 字典，不让业务代码依赖 `sqlite3.Row`。

## 4. 用户与工作区隔离

### 4.1 将文件级隔离显式化

目前 Web 与研究库随用户目录分开，CLI 根目录也是独立的数据范围。统一入库后不能只按会话 ID 或研究 ID 查询。

新增 `storage_namespaces`：

| 字段 | 规则 |
| --- | --- |
| `id` | `CHAR(32)`，宿主生成的 UUID hex，作为 `namespace_id` |
| `kind` | `web_user` 或 `cli` |
| `owner_user_id` | Web 工作区关联认证用户；CLI 可为 NULL |
| `root_fingerprint` | `CHAR(64)`，规范化数据根目录的 SHA-256；唯一 |
| `status` | `importing`、`ready`、`disabled` |
| `created_at` | UTC 时间 |

Web 用户工作区要求 `owner_user_id` 唯一、非空；由服务端确认 `kind` 与 owner 的对应关系，并建立相应检查约束。CLI 工作区必须通过宿主配置显式注册，不能把未经登录的 Web 请求降级到 CLI。

服务端按当前登录用户取得其 namespace，构造绑定范围的 Store。用户业务表全部携带非空 `namespace_id`；所有读、写、恢复、批量操作均限定此字段。跨表外键使用 `(namespace_id, parent_id)`，同时防止误关联其他工作区的数据。

工作区目录增加 `.storage_namespace.json`，记录 ID 与格式版本。它用于定位；授权仍由认证用户和数据库的 owner 映射决定。现有 `.owner.json`、路径越界与符号链接校验继续执行。目录移动必须显式更新映射，并检查 Redis 命名空间和产物路径，不能因路径变化自动建立一套空历史。

需要恢复的用户工作区改为查询 `storage_namespaces` 和相应业务记录，不再检查 `web.sqlite3` 是否存在。备份文件或 marker 丢失时不能静默给现有工作区换 ID。

### 4.2 记忆范围

记忆原文与当前 `user_id` / `project_id` 筛选继续由 `MemoryStore` 管理。向量缓存键增加 namespace 和记忆用户范围：

```text
(namespace_id, memory_user_id, memory_id, fingerprint_hash)
```

`memory_user_id` 保留当前 `local` 或用户 ID 语义，不强制作为认证用户外键。项目范围仍由候选记忆记录决定，保留可跨项目的用户记忆。向量查询只能使用已经通过用户与项目筛选的候选 ID，不能先扫描所有用户向量再过滤。

## 5. 表设计与字段约定

### 5.1 目标表清单

认证表保留原名，Web 与研究表增加前缀，避免现有同名表合并时冲突。以下列出主键、重要唯一约束和索引；原有业务字段均需映射，实施时在 `schema.py` 与首个 Alembic revision 中给出完整 DDL。

| 目标表 | 关键约束或索引 |
| --- | --- |
| `users` | PK `id`；UNIQUE `username_key`、`email_key` |
| `auth_sessions` | PK `token_hash`；`user_id`、`expires_at` 索引；允许匿名预登录会话的 `user_id` 为 NULL |
| `invites` | PK `token_hash`；保留 `consumed_by`、`consumed_at` 与邮箱限制 |
| `auth_audit` | 全局 BIGINT 自增 PK；索引 `(user_id, occurred_at)` |
| `auth_limits` | PK `limit_key`；`started` 索引，保留计数与窗口 |
| `auth_meta` | PK `meta_key`；保留 `csrf_key` 等内部元数据 |
| `storage_namespaces` | 第 4 节定义 |
| `namespace_sequences` | PK `(namespace_id, sequence_name)`；保存 `last_value` |
| `web_sessions` | PK `(namespace_id, id)`；UNIQUE `(namespace_id, seq)`；用于原分页 |
| `web_runs` | PK `(namespace_id, id)`；UNIQUE `(namespace_id, session_id, idempotency_key)`；UNIQUE `(namespace_id, seq)`；索引 `(namespace_id, status)` 和 `(namespace_id, session_id, seq)` |
| `web_events` | PK `(namespace_id, seq)`；UNIQUE `(namespace_id, source)`；索引 `(namespace_id, session_id, seq)` |
| `web_cursors` | PK `(namespace_id, session_id)`；`runtime_seq` 保存 Redis 事件投影位置 |
| `research_runs` | PK `(namespace_id, id)`；索引 `(namespace_id, status)`；完整保留 owner、generation、指纹、预算时间及状态字段 |
| `research_tasks` | PK `(namespace_id, run_id, task_id, attempt)`；UNIQUE `(namespace_id, seq)`；索引 `(namespace_id, run_id, status)` |
| `research_reservations` | PK `(namespace_id, id)`；索引 `(namespace_id, run_id, kind)` 和 `(namespace_id, run_id, state)` |
| `research_grants` | PK `(namespace_id, run_id, task_id, result_id)`；保留 `tool` |
| `research_events` | PK `(namespace_id, seq)`；索引 `(namespace_id, run_id, seq)` |
| `research_stage_states` | PK `(namespace_id, run_id, task_id)`；UNIQUE `(namespace_id, seq)`，更新保留初始顺序 |
| `research_execution_selections` | PK `(namespace_id, id)`；保留 scope hash、source、consumed、关联研究 ID |
| `memory_vectors` | 第 4.2 节的复合 PK；保留 `text_hash`、完整 fingerprint、维度和向量内容 |
| `storage_import_batches` | 批次 ID、源快照摘要、目标范围、状态、计数和核对结果；约束同一份源不能重复接收 |
| Alembic 版本表 | 统一管理目标 schema；旧 `schema_migrations` 内容作为导入元数据留档 |

Web 子表引用本 namespace 的会话；研究子表引用本 namespace 的研究。邀请、审计等认证关系按原数据实际情况补齐外键；导入前先检查旧库孤儿数据，不能为建立外键默默丢行。

### 5.2 类型与兼容

- 不透明 ID、哈希和内部 key 采用显式长度与二进制比较语义，避免默认排序规则把不同 key 当作相同值。用户可见正文使用 `utf8mb4`。
- 现有用户 ID 是 `usr_` + 32 位 hex，Web 会话 ID 是 `web_` + 32 位 hex，执行 ID 是 `run_` + 32 位 hex；分配长度时包含前缀。
- 普通状态使用 `VARCHAR`，不使用难以扩展的数据库 ENUM。`key`、`run` 等字段重命名为 `idempotency_key`、`run_id`，在 Store 边界映射回旧返回字段。
- 原有 JSON 字符串首期保存为 `LONGTEXT`，维持当前序列化与返回接口；写入前仍执行合法 JSON 校验。向量也先以 JSON 文本保存，不增加数据库向量检索。
- 为索引字段逐项定义最大长度并预检旧数据。可能很长的 fingerprint 用 SHA-256 作为索引键，同时保存完整值用于核对；不得截断旧 key。发现超长或排序冲突时，预检失败并给出字段位置。
- 认证及研究计时的 Unix 秒暂存为 `DOUBLE`，保持现有秒与浮点运算。原 ISO 时间字段保留字符串与时区语义；本次不同时更改 API 的时间表示。
- 所有查询显式指定排序。研究 `tasks`、`stage_states` 迁移时按旧 `rowid` 写入显式 `seq`，之后按该字段排序。
- 需要判断状态变更是否成功的更新使用带旧状态/owner 的条件更新。对幂等 upsert 不依赖不同驱动的 affected rows 差异推断业务成功。

## 6. 顺序、事件投影与幂等

### 6.1 保留每个工作区的序号

多个旧 SQLite 库的 `seq` 会重复。因此事件序号不能直接导入为一个全局自增主键。`web_sessions`、`web_runs`、`web_events`、`research_events` 以及需要原插入顺序的研究表都使用 namespace 内序号。

`namespace_sequences` 为每个工作区及序列类型保存计数行；在创建工作区或导入时预先建立。新增记录的事务锁住对应计数行、分配序号并写记录，同一事务提交。事件写入持有计数行锁直到提交，以避免并发事务出现大序号先可见、客户端游标越过稍后才提交的小序号。

导入保留原数值、空洞及顺序；计数器至少取旧 `sqlite_sequence` 高水位与现存最大 seq 的较大值。`rowid` 表按旧 rowid 保留顺序和可用高水位；不能假设游标永远连续。

SSE 继续返回原 `sequence` / `event_id` 格式，查询由认证 namespace 限定。保持同一用户迁移前后的事件编号，浏览器已有 `Last-Event-ID` 无需因切库而清空。

### 6.2 Redis 到 Web 的投影

`WebStore.project()` 的事件插入和 `web_cursors.runtime_seq` 推进放在同一个 MySQL 事务内。目标事件以 `(namespace_id, source)` 去重；游标只前进。该游标是 Redis 投影位置，与 MySQL Web 事件的 SSE 序号分别保存。

重复投影不生成重复展示事件；写入失败则不推进游标。Redis 和 MySQL 之间允许重新读取并重复投递，通过幂等恢复，不宣称跨库恰好执行一次。若调用链需要确认 Redis 通知，确认只能发生在 MySQL 成功提交之后。

## 7. 事务与并发改造

数据库隔离级别拟采用 `READ COMMITTED`，配合明确的行锁和唯一约束，不依靠默认隔离级别解决业务竞争。需要读取后决定写入的流程使用 `SELECT ... FOR UPDATE`；锁定读需要处在有效事务内，参考 [MySQL 锁定读](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)。

| 场景 | MySQL 下的处理规则 |
| --- | --- |
| 注册与邀请码消费 | 锁定邀请码行，核对有效期/邮箱/未消费状态；插入用户与消费邀请同事务，唯一索引处理账号竞争 |
| 登录限流 | 先通过受控 upsert 确保限流行存在，再锁定读取、判断窗口并更新；并发首次创建也必须正确 |
| 会话签发与账号禁用 | 对用户状态与 auth_version 检查使用一致锁顺序；修改密码/禁用与撤销会话同事务，避免签发已失效版本 |
| Web 请求去重与队列额度 | 锁定工作区配额对应的 namespace 行，检查幂等键和 pending 数量后插入；唯一约束兜底，同键异内容仍返回 `IDEMPOTENCY_CONFLICT` |
| 研究执行权 | 按 namespace + run 锁定研究行，再核对进程身份、owner、generation 和状态；原本机进程检查仍保留 |
| 预算预留、任务次数 | 在同一研究父行锁内核对 owner、计时与额度，写入 reservation / task；所有竞争入口统一遵守该锁 |
| 预算结算 | 按同一父行锁顺序处理；pending 条件更新确保同一预留不重复结算；结果未知的预留继续计费 |
| 执行选择消费 | 锁定 selection 行或使用 `consumed=0` 条件更新；消费与核对原子完成，绑定研究时检查原关联 |
| 研究晚到结果 | 在检查当前 owner 的事务中更新 `status='running'` 的指定 attempt，拒绝旧 owner 或已终止任务 |
| 事件投影 | 按第 6 节同时更新去重事件、序号与投影游标 |

锁顺序统一为需要的 namespace 行 → 研究/会话父行 → 子记录 → 对应序号行；同层多行按稳定 key 排序。审计与事件若需和状态变更保持原子性，应通过事务内内部方法写入，避免方法另开事务导致嵌套锁等待。

向量 upsert、阶段更新和游标推进使用显式 MySQL upsert 或 Core 方言方法。不把 `INSERT OR REPLACE` 改成 MySQL `REPLACE`；逐项表达要更新的字段，避免替换行产生额外删除语义。重复键忽略只用于预期的幂等冲突，其他约束与类型错误必须报出。

模型、Embedding、HTTP、报告生成和较大文件写入均放在数据库事务外。研究产物继续先写不可变文件，再在短事务中接纳相应引用；失败可能留下未引用文件，通过后续清理处理，不假设 MySQL 可以回滚文件。

对已确认回滚的死锁或锁等待超时，只重试完整的纯数据库事务，最多 3 次并带短暂随机退避。事务中不得包含模型或工具调用。连接在提交时断开且提交结果未知时，不自动重放业务或外部调用，应凭幂等键/operation ID 查询恢复。MySQL 仍可能发生死锁，参考[官方死锁处理说明](https://dev.mysql.com/doc/refman/8.4/en/innodb-deadlocks-handling.html)。

## 8. 向量缓存改造与降级

`VectorCache` 接受数据库和记忆范围，不再接受 `.sqlite3` 路径。复用当前内容摘要、模型/服务 fingerprint、维度验证、缺失向量批量补算和 Python 相似度计算。

读取候选缓存后及时释放连接，再调用 Embedding；生成完成后批量 upsert。删除记忆时清理相同 namespace 和记忆用户范围内的缓存，不能只凭 memory ID 跨用户删除。

可选的向量缓存读写失败沿用现有关键词召回降级，并记录脱敏故障。认证、Web 和研究账本故障则返回明确错误，不能悄悄切回 SQLite 或内存。MySQL 作为共享服务故障时，向量降级不代表依赖 MySQL 的 Web 登录和业务仍然可用。

向量库导入默认包含有效缓存；记录损坏、维度不兼容或 fingerprint 不匹配时进入隔离清单。可以显式选择不导入缓存、以后重新计算，但不能将此选项应用到研究预算和账号等权威数据。

## 9. 配置、启动与运行

拟在 [config.py](../../src/finagent/config.py) 增加 `DatabaseSettings`，更新 `pyproject.toml`、配置示例和 `.env.example`。以下只是目标配置，当前程序尚不识别：

```toml
[database]
backend = "mysql"
pool_size = 5
max_overflow = 5
pool_timeout = 5
pool_recycle = 1800
connect_timeout = 3
read_timeout = 10
write_timeout = 10
```

连接串仅通过 `FINAGENT_MYSQL_URL` 提供，格式使用 `mysql+pymysql://.../finagent?charset=utf8mb4`，按 SecretStr 保存并从配置导出、Agent 上下文和错误输出中排除。支持 CA 与服务端身份验证的 TLS 配置；凭证中的保留字符使用规范编码，禁止手工拼接 URL。

启用 `pool_pre_ping` 并配置回收时间；这用于发现失效连接，不承诺修复已经中断的事务，见 [SQLAlchemy 连接池文档](https://docs.sqlalchemy.org/en/20/core/pooling.html#disconnect-handling-pessimistic)。初始池大小是开发默认值，需根据 worker 数和 MySQL 连接额度调整；不按用户数量扩张连接池。

启动前由维护流程完成 schema upgrade。Web 启动检查连接、schema revision 和导入状态；缺少配置、结构未升级、未完成导入或已有 SQLite 待迁移时给出明确错误，不自动创建空历史。新安装可显式初始化空部署。涉及业务存储的 CLI、认证管理命令和研究命令共用这套入口。

建议错误码：`DATABASE_UNAVAILABLE`、`DATABASE_SCHEMA_MISMATCH`、`DATABASE_MIGRATION_REQUIRED`、`DATABASE_IMPORT_INCOMPLETE`。对 Web 映射为适当的 503/维护响应，CLI 返回非零状态。现有业务错误语义继续保留。唯一约束冲突不能全部映射为“数据库不可用”。

对 [security.py](../../src/finagent/security.py) 增加数据库凭证与 URL 脱敏；SQLAlchemy 禁用参数日志并隐藏异常中的参数。数据库连接配置不进入工具返回、研究报告或模型提示词。

## 10. 旧数据迁移

采用维护停写后的单次切换；开发阶段可以逐个 Store 完成，正式写流量在四部分一起核对通过后切换。本期不实现双写或在线增量追赶。

### 10.1 预检与源数据发现

新增 `scripts/migrate_sqlite_to_mysql.py`，默认仅预览。报告至少包括：

- 全局认证库、每个 `users/usr_*/` 目录、CLI 根目录，以及显式指定的其他数据根；历史备份和实验目录不能被递归自动接管。
- 所有业务表、schema 版本、记录数量、seq 高水位、JSON 可解析性、字段长度、唯一键与外键冲突。
- 当前用户与目录 owner 的一致性，CLI 与 Web 数据的归属映射；无法判定的旧共享数据必须显式指定归属。
- 不同用户的相同 ID / 相同 seq 不视为冲突；同一目标 namespace 内冲突则拒绝导入。
- 在途 Web、研究任务、服务租约及未完成旧迁移。先结束或明确标记中断，保留未知调用的预算占用。
- 若存在尚未迁入 Redis 的 `runtime.sqlite3`，先按现有 Redis 迁移流程处理；本方案不会把历史运行检查点导入 MySQL。

### 10.2 停写与一致备份

停止 Web、CLI 研究和其他写入进程，取得现有服务/工作区维护租约。明确停止单机上的所有写入口，不能只关闭浏览器页面。

对每个 SQLite 使用 backup API 创建包含已提交 WAL 页的一致快照，执行完整性检查并记录摘要；不直接复制主 `.sqlite3` 文件。备份文件与路径引用、记忆正文、报告和证据一并留存。保存相关 Redis 原子快照与事件高水位，供联动核对和回退使用；本次不修改其会话键。

### 10.3 导入与可恢复性

1. 在独立、空的目标 MySQL database 完成 schema upgrade；维护账号负责 DDL，导入只执行 DML。
2. 先导入认证数据，包括密码哈希、会话 token hash、CSRF 根密钥和 auth_version，保留原用户 ID。
3. 建立 namespace 与 owner 映射，状态为 `importing`；创建序号计数行。
4. 按父子顺序导入 Web、研究及 selection 表；保留 seq、任务 attempt、owner/generation、reservation 状态、scope hash 和产物引用。
5. 导入向量缓存并检查范围及 fingerprint；原文件目录保持原位。
6. 分批事务写入，同一批次保存进度和快照摘要。重复执行只能继续同一快照、同一映射的未完成批次；已存在数据必须完全匹配，否则拒绝覆盖。
7. 核对全部成功后提交批次 `verified` 和 namespace `ready`，写入本地映射 marker。应用只有在数据库与本地映射共同完成时才能启动。

目标 database 不为空时，只有迁移工具登记的同批次续传可以继续；不能导入到已经接收新业务写入的目标。MySQL DDL 不参与数据导入事务；导入中断不声称整库自动回滚，未验证目标始终保持不可服务状态。

老密码哈希和 token 不重新生成。保留会话需要同时保留 CSRF 根密钥、原 URL/Cookie 配置和有效期；已过期或已撤销的会话不能恢复为有效。迁移报告和日志只记录摘要与统计，不输出认证秘密。

### 10.4 核对与切换

切换前至少完成：

| 数据 | 核对规则 |
| --- | --- |
| 全部权威表 | 按 namespace 的行数、主键集合、空值、规范化记录摘要一致；明确重命名及新增字段映射 |
| 认证 | 原账号可登录；旧有效 Cookie 在配置一致时可用；密码哈希、token hash、CSRF 和 auth_version 对应一致 |
| Web | 会话和请求内容一致；幂等键不变；事件 seq/source、SSE 续读和 Redis 投影游标一致 |
| 研究 | 任务顺序与 attempt、selection 消费状态、授权、预算计数、pending 预留及 owner/generation 一致 |
| 文件引用 | 对每个被接纳的产物和报告检查路径仍在原工作区内、文件存在、原有校验摘要一致 |
| 记忆 | 正文不变；有效向量内容和范围一致，重建或隔离项单独列出 |
| Redis | 检查点、任务、有序事件和待确认通知与迁移前一致，MySQL 游标不越过来源高水位 |

更新连接配置后先执行维护冒烟，再开放新写入。旧 SQLite 和快照只读保留；正常运行不得继续写旧库。记录恢复点、导入批次、源/目标 schema、代码版本、配置摘要和维护窗口。

## 11. 回退边界

必须区分“导入完成”与“已经产生新业务数据”：

| 时点 | 回退方式 |
| --- | --- |
| 尚未开放新写入 | 停服务，恢复迁移前代码和配置；核对文件与 Redis 是否发生变更，必要时一并恢复一致快照，再使用原 SQLite |
| 已经开放新写入 | 不能直接切回旧 SQLite。先停写，备份当前 MySQL、Redis 和文件；优先修复新版本。如必须回旧版，需额外开发反向导出与跨存储核对流程 |

实施首期不承诺切流后的无损自动回退。放量前必须演练“未开放写入”的回退；切流后的故障以停止写入、保留现状、前向修复为默认处理方式。

## 12. 分阶段实施与交付

| 阶段 | 工作 | 完成条件 |
| --- | --- | --- |
| P0：接口与数据盘点 | 冻结 Store 行为、字段映射、范围和锁顺序；提供脱敏迁移预检 | 所有现有表及直接 SQL 调用有归属；真实旧数据的兼容问题有明确处理 |
| P1：基础层 | DatabaseSettings、Engine、schema、Alembic、namespace、序号和错误处理 | 空 MySQL 可初始化，连接故障/结构版本不匹配可诊断，连接池可释放 |
| P2：认证与 Web | 改造 AuthStore、WebStore、用户运行注册与队列/SSE 投影 | 认证、幂等、限流、用户隔离和恢复合同在真实 MySQL 上通过 |
| P3：研究与向量 | 改造 ResearchStore、预算和 selection 直接 SQL、VectorCache | 研究额度与 owner 竞争、缓存降级和记忆范围验证通过 |
| P4：迁移工具 | 一致快照、导入、续传、核对及回退演练 | 带 WAL、重复 ID/seq、多用户、pending 预算的旧数据可完整迁移 |
| P5：联调与切换准备 | 全量回归、浏览器验收、运行指南与项目设计更新 | 验收表逐项有实际结果；明确切换与恢复步骤，随后进入维护切换 |

日常开发按以上顺序提交；每阶段使用独立测试数据。在 P5 前不对现有用户流量做部分切库。

主要改动位置：

| 模块 | 改动 |
| --- | --- |
| `config.py`、`security.py`、`pyproject.toml`、配置示例 | 数据库配置、依赖与脱敏 |
| 新增 `storage/`、`migrations/` | 连接池、表定义、范围、结构版本和导入 |
| `web/auth_store.py`、`web/auth_cli.py` | 全局认证仓储与管理命令 |
| `web/store.py`、`web/user_runtime.py`、`web/app.py` | 范围注入、队列/事件事务、启动恢复与故障响应 |
| `research/store.py`、`research/budget.py`、`research/selection.py` | 研究事务与直接 SQL 收口 |
| 研究 coordinator、report、delegation、CLI 等 Store 构造点 | 显式传入数据库和 namespace，继续校验文件路径 |
| `harness/host.py`、`harness/memory.py`、`harness/memory_search.py` | 向量缓存注入与统一异常降级 |
| `web/migration.py`、`harness/runtime_migration.py` | 区分新 MySQL 数据归属迁移与离线旧 SQLite 读取 |
| 测试 fixture 与文档 | MySQL 隔离环境、断言改造、验收记录和启动说明 |

## 13. 测试与验收标准

数据库集成与并发测试使用真实 MySQL 8.4。纯业务单测可以使用小型内存替身，但不能以 SQLite 通过作为 MySQL 兼容证据。并发用例使用独立连接和同步屏障；线程共享一个连接的测试不能证明数据库并发语义。

| 验收项 | 必须观察到的行为 |
| --- | --- |
| 功能兼容 | 原认证、Web、研究、记忆及 CLI 相关回归通过；公开 API 与 JSON 返回语义一致 |
| 用户隔离 | 两用户和 CLI 使用相同会话 ID、研究 ID、幂等 key、事件 seq、memory ID 时互不可见；伪造 owner/namespace 不能越权 |
| 认证竞争 | 同邀请码并发注册仅成功一次；并发首次限流计数正确；账号禁用与签发竞争不产生可用的过期 auth_version 会话 |
| Web 幂等与额度 | 同键同请求只创建一次，同键异请求冲突；不同连接并发入队不超过用户额度 |
| 研究预算 | 最后一个预算名额并发竞争只接纳一次；失败/断线后的 pending 占用不消失；晚到 attempt 和旧 owner 结果被拒绝 |
| 事件与恢复 | 并发事件写入不导致游标漏读；重复 Redis 投影不重复展示；投影中断不超前推进；SSE 迁移前后可续读 |
| 向量缓存 | 命中、文本/配置变更失效、维度不符重算、删除、用户/项目筛选与异常关键词降级行为正确 |
| 迁移异常 | WAL 数据可导入；导入中断可续传；重复运行幂等；非空目标和源摘要变化被拒绝；文件丢失/摘要不符不能通过核对 |
| MySQL 故障 | 启动连接失败、运行中断连、提交未知、死锁和池耗尽可区分；无 SQLite 回退，无凭证泄露，无重复外部调用 |
| 存储收口 | 运行操作不创建或修改任何业务 `.sqlite3`；SQLite 读取仅存在于标明的离线迁移路径 |
| 浏览器 | 登录、退出、多用户会话、流式回复、研究创建/查看、重启后的历史查看可用 |
| 运维恢复 | MySQL 备份能恢复到独立环境，并与对应 Redis/文件恢复点核对；切流前回退演练通过 |

补充执行现有 `test_auth.py`、`test_web.py`、`test_startup.py`、`test_memory*.py`、`test_research*.py`、Redis 合同测试和全量 Python 回归，更新测试 fixture。前端运行已有单测、生产构建和浏览器验收。只有实际执行后才能记录通过数量和耗时。

记录迁移行数与耗时、典型登录/列表/事件投影延迟、连接池等待、研究事务锁等待和死锁重试次数，并与同数据规模的现有版本比较。具体阈值在 P0 用当前环境基线确定；本方案不预设迁入 MySQL 后一定更快。

## 14. 交付状态说明

本文主体保留开发时的目标与验收要求。2026-11-15 已交付 MySQL 存储实现、结构迁移和离线导入工具，具体测试、数据演练及尚未完成的运行切换见 [交付记录](../features/MYSQL_STORAGE.md)。方案中的目标不自动代表全部部署验收已完成。
