# Redis 短期会话记忆与恢复

更新日期：2026-11-04。短期会话运行存储已从 SQLite 改为 **Redis 主存储**，不是 Redis 缓存叠加 SQLite。`RuntimeStore` 保存对话检查点、任务、后台工作、定时记录、事件序号及通知确认状态；连接失败时不回退到 SQLite，也不静默创建空会话覆盖历史。

本次范围是短期会话运行层。账号认证、Web 提交/展示元数据、独立研究账本和长期记忆向量缓存仍使用各自的 SQLite；长期记忆原始记录仍为 JSON。transcript、压缩归档和单机租约仍保留在文件系统。完整会话主状态以 Redis 检查点为准，transcript 不是自动降级数据库。

## 数据与原子性

每个运行数据根目录按绝对路径 SHA-256 生成 Redis 命名空间：`finagent:runtime:v3:{<root_sha256>}:...`。CLI 的 `.finagent/v3/` 和各账号的 `.finagent/users/usr_<ID>/v3/` 使用不同前缀；同一账号的不同会话也有各自记录键。身份仍来自服务器认证，Redis key 名不能代替权限校验。

| 数据 | Redis 表示 | 行为 |
| --- | --- | --- |
| 完整会话检查点 | Hash：session ID → JSON | 保存 messages、state、context、范围和版本 |
| 任务/jobs/work | 每会话 Hash + 顺序 List | 保留插入顺序与会话隔离 |
| 有序事件 | 全局/会话 Sorted Set + 正文 Hash | 单调序号支持 Web 原游标续接 |
| 待确认通知 | 每会话 Sorted Set | ack 仅移除待投递项，历史事件保留 |
| 迁移与存储版本 | 元数据 Hash | 记录版本及导入来源，防止覆盖已有运行数据 |

事件 ID 去重、分配序号、保存正文和通知入队在单个 Lua 调用中完成；ack 同样原子执行。任务依赖变更、认领、完成和释放使用 WATCH/MULTI 乐观事务：冲突时重新读取和检查，因此并发认领仍只有一个 owner，同一执行者不能同时认领两个任务。回调失败不提交修改。

当前这些键**不设置 TTL**。短期指会话范围，不代表任意时刻可以丢弃。已有数据目录中的 `.runtime_redis.json` 只记录后端和命名空间；发现 Redis 元数据缺失或命名空间改变时拒绝静默重建。不要对业务 Redis 使用 `FLUSHDB` 或按活跃时间删除会话键。

## 配置与启动

安装项目 Python 依赖，并准备 Redis 服务。当前本机运行 Redis 7，Python 使用 redis-py；完整回归也需要一个可连接的 Redis：

```toml
[runtime]
redis_url = "redis://127.0.0.1:6379/0"
namespace = "finagent:runtime:v3"
connect_timeout = 3
socket_timeout = 10
```

`FINAGENT_REDIS_URL` 可在进程环境或配置目录 `.env` 覆盖，进程环境优先。连接凭证放 `.env`，不写入 TOML、前端或日志；远程受控服务可使用 `rediss://`，Unix socket 使用 `unix://`。URL 不进入 Settings 的公开序列化，已配置密码纳入安全脱敏。

默认本机启动脚本仅监听回环地址，启用 `appendonly yes`、`appendfsync always`、`maxmemory-policy noeviction`，不启用周期 RDB 快照。AOF 在 `.finagent/services/redis/`，不会写回旧会话 SQLite：

```bash
.venv/bin/python scripts/serve_runtime_redis.py
# 在另一终端检查就绪
.venv/bin/python scripts/check_runtime_redis.py --wait 10
```

脚本优先使用已安装的 `redis-server`；也可传 `--binary`，本机工作区还准备了 `.finagent/services/redis-dist/` 的发行版二进制及运行库。使用其他 Redis 服务时，持久化、内存策略和备份由该服务的部署者负责；脚本不更改远端服务器配置。

本机已部署并启用用户服务 `finsight-redis.service`。模板位于 [deploy/systemd/](../../deploy/systemd)，默认路径是 `%h/Agent/FinAgent`，其他部署路径需要先修改模板。Web 依赖配置见 `finsight-web-redis.conf`；应在首次旧数据导入完成后再重载/启动新版 Web。就绪检查只 PING，不打开运行命名空间。

## 从旧会话 SQLite 迁移

升级前停写 CLI，并完成或取消在途任务。迁移脚本先预览；`--apply` 检查新旧 Web 进程租约、会话宿主租约、队列和后台工作，备份后导入：

```bash
.venv/bin/python scripts/migrate_runtime_to_redis.py
# 停止 Web；确认其他 CLI 已退出
systemctl --user stop finsight-web.service
.venv/bin/python scripts/migrate_runtime_to_redis.py --apply
systemctl --user start finsight-web.service
```

脚本处理配置根目录的 `v3/`、历史 `web_server/` 以及 `users/usr_*/v3/`。其他归档/实验目录不自动接管，可用对应配置文件显式处理。使用 SQLite backup 读取包含已提交 WAL 的一致副本；Redis 导入在事务中完成，保留消息、任务、事件 ID、原序号和通知确认状态，因此 Web 既有游标不需要清零。完成后回读核对；再次运行跳过已导入的命名空间，非空目标拒绝覆盖。

备份、一致快照及 manifest 位于 `.finagent/migration/runtime_redis_<ID>/`。旧 `runtime.sqlite3` 保留作回退材料，运行路径不再读写它；只有离线迁移模块使用 SQLite 读取旧数据。不要在新版运行后重新启动旧代码向旧库继续写入，也不要用旧快照覆盖已新增的 Redis 会话。

账号之间的历史归属迁移仍使用 `finagent auth migrate-legacy --owner ...`：它额外备份 Redis 原子快照、改写结构化 owner/路径，并恢复到目标用户的新命名空间。未完成的维护应保留备份和目标目录核对，不能通过重新登录自动接管。

## 验证与限制

```bash
.venv/bin/python -m unittest tests.test_runtime_redis tests.test_harness_v3 tests.test_auth tests.test_web -q
.venv/bin/python -m unittest discover -s tests -t . -q
```

Redis 合同测试使用临时根目录对应的命名空间，检查无 SQLite 写入、跨账号/会话隔离、并发任务认领、事务回滚、事件去重与 ack、分页游标、迁移 WAL 和错误脱敏。迁移验证与完整回归结果在代码交付时单独记录，不把真实金融数据质量作为这些测试的结论。

Redis 重启恢复已做实际 AOF 冒烟：临时命名空间中的 1 个检查点、1 个任务和 1 条待确认通知全部恢复，随后清理探针数据。Redis 服务是新增运行依赖；不可用时返回明确错误，没有内存/SQLite 兜底。错误代码包括 `RUNTIME_UNAVAILABLE`、`RUNTIME_MIGRATION_REQUIRED`、`RUNTIME_DATA_MISSING` 与 `RUNTIME_NOT_EMPTY`。

2026-11-04 交付验证：Python 完整回归 **733 项通过，60.011 秒**；Chromium 浏览器验收 **11 项通过，9.0 秒**。本机 3 个旧运行库中的 **12 个会话、1343 条事件**完成一致备份、导入及回读核对；Web 恢复后健康接口 200、无 Cookie 的私有请求 401，原账号的 2 个历史会话可正常打开。迁移 manifest 位于 `.finagent/migration/runtime_redis_96e5554bafba43bda90697c7f3a4f30b/manifest.json`。原 Web 是临时用户服务，本次补充并启用了持久的 `finsight-web.service`，与 `finsight-redis.service` 建立启动依赖。上述验收没有调用真实金融模型或市场来源。

本次不把应用改成分布式执行集群：会话/服务租约仍是本机文件锁，Web 仍限制单进程；只有接入 Redis，不能认定已支持多实例并发接管同一会话。

实现：[state.py](../../src/finagent/harness/state.py)、[tasks.py](../../src/finagent/harness/tasks.py)、[runtime_migration.py](../../src/finagent/harness/runtime_migration.py)、[session_manager.py](../../src/finagent/web/session_manager.py)。
