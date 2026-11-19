# MySQL 业务存储、迁移与交付记录

日期：2026-11-15。[开发方案](../requirements/MySQL存储迁移方案.md) · [返回功能索引](README.md)

## 当前交付状态

已完成正式迁移和服务切换。认证、Web 元数据、研究账本和长期记忆向量缓存使用 MySQL，通过 SQLAlchemy Core + PyMySQL 访问，结构版本由 Alembic 管理。Redis 继续保存 Agent 运行状态，文件继续保存记忆正文、对话与研究产物。

项目使用独立的 MySQL 8.4.2 实例，仅通过 `.finagent/services/mysql/run/mysql.sock` 访问。`finsight-mysql.service` 已安装、启用并运行；`finsight-web.service` 已恢复，并依赖 MySQL 与 Redis。

运行账号 `finagent_app` 只拥有 `finagent.*` 的 SELECT/INSERT/UPDATE/DELETE 权限。维护账号负责结构迁移。账号凭证保存在权限 0600 的 `.finagent/services/mysql/accounts.json`，运行 URL 通过权限 0600 的 `.env` 加载；凭证未输出到交付日志。

正式批次：`6add854b11ce31c2cff4a865995f9998`。备份目录：`.finagent/migration/mysql_6add854b11ce31c2cff4a865995f9998/`。原 SQLite 保留，包含 WAL 的一致快照、原文件、Redis 快照、原代码及配置都可在备份中追溯。正式应用已验证使用 MySQL，检查期间旧 SQLite 文件和逻辑摘要均未变化。

## 存储机制

- [storage/schema.py](../../src/finagent/storage/schema.py) 定义认证、工作区、Web、研究、向量和导入批次表；Store 构造时不建表。
- [storage/database.py](../../src/finagent/storage/database.py) 提供全进程连接池、命名参数、工作区限定的 Core 操作、事务及脱敏故障。
- [storage/scope.py](../../src/finagent/storage/scope.py) 将可信用户与 `storage_namespaces` 绑定。原文件目录继续存在，业务查询必须限定 namespace；目录 marker 不能自行授予用户访问权。
- Web 和研究保留各 namespace 的原序号；序号行及 namespace 写锁覆盖事务提交，避免并发写入产生游标越过未提交事件。内部 key 使用 `utf8mb4_0900_bin`，保留大小写与末尾空格区别，见 [MySQL 排序规则说明](https://dev.mysql.com/doc/refman/8.4/en/charset-binary-collations.html)。
- 幂等请求、邀请码、限流初次建行、研究 owner、任务尝试和预算竞争通过锁与约束处理。研究写事务继续以工作区为单位串行，保持原 SQLite 写事务语义；这次未追求同一工作区的最大并发吞吐。
- Redis 事件投影及 MySQL 游标在同一事务提交，可重复读取投影；不把它描述为跨数据库事务。
- 向量保留 JSON 格式和原 fingerprint/内容摘要，继续在 Python 计算相似度。数据库故障时可降级到关键词；认证、Web 和研究不回退 SQLite。

## 初始化与启动

依赖通过项目安装：

```bash
.venv/bin/python -m pip install -c deploy/mysql-constraints.txt -e '.[web]'
```

配置 `FINAGENT_MYSQL_URL`，运行连接使用只拥有业务 DML 权限的账号；维护连接用于 Alembic 和离线导入。URL 支持 TCP 或 Unix socket。密码需要 URL 编码，完整 URL 不写进 TOML、报告或日志。配置字段见 `finagent.toml` 的 `[database]` 与 `.env.example`。

在已经配置维护连接的终端中执行：

```bash
.venv/bin/python scripts/mysql/upgrade.py --create-database
```

`--create-database` 只创建 URL 指定的 database；不创建用户或授权。维护账号已可访问现有 database 时，可省略该选项。启动发现 schema 版本不匹配、未完成导入或旧 SQLite 未迁移时会明确失败。

[serve_runtime_mysql.py](../../scripts/serve_runtime_mysql.py) 可以启动已经初始化的项目本地实例，通过 `--mysqld` 指定 MySQL 8.4 程序。本地目录为 `.finagent/services/mysql/`，仅监听其 Unix socket；该目录权限为 0700。模板 [finsight-mysql.service](../../deploy/systemd/finsight-mysql.service) 中的程序路径需按部署环境调整，模板已安装并启用，启动后通过运行账号进行连接与结构检查。

[setup_accounts.py](../../scripts/mysql/setup_accounts.py) 已在用户确认继续后执行。它只针对本项目独立实例，创建 `finagent_app` 和 `finagent_maintenance`，分别赋予 `finagent.*` 的业务 DML 与 schema 维护权限，不修改 root 密码。随机凭证写入权限 0600 的本地文件，不输出到终端。脚本要求明确的 `FINAGENT_MYSQL_ADMIN_URL`；再次执行会拒绝覆盖已有凭证。`activate_local.py --apply` 在已验证导入后配置本机服务；`verify_live.py` 进行实机检查，不调用金融模型。

## 正式迁移步骤

1. 完成运行/维护账号配置，准备空的目标 database，并执行 Alembic 升级。
2. 预览：`.venv/bin/python scripts/migrate_sqlite_to_mysql.py`。预览不要求连接 MySQL；非空的未知旧表、在途工作、超长字段或异常 JSON 会阻止迁移。
3. 停止本项目 Web、CLI 研究及其他写入入口，保持现有数据路径不变。
4. 在维护连接下运行 `.venv/bin/python scripts/migrate_sqlite_to_mysql.py --apply`。
5. 核对生成的 `verified.json` 与备份目录。SQLite 通过 backup API 保留已提交 WAL，文件与 Redis 快照同时留存，目标逐表核对行数、键和字段。
6. 将运行账号 URL 写入配置目录 `.env`，按模板安装/调整 MySQL 服务依赖，再启动 Web。
7. 在开放新写入前检查旧有效会话、历史报告、SSE 续读与用户隔离；此时仍可核对后回到迁移前恢复点。

工具按表、每 250 条记录提交，只有同一源摘要及同一批次能续传；目标冲突或源变化会拒绝覆盖。导入中工作区和批次保持不可服务状态，全部验证后才标记 ready/verified。已完成批次再次运行返回 `already_migrated`，不会覆盖之后的新数据。

旧 SQLite、JSON/JSONL 和 Redis 不删除。开放新业务写入后，旧 SQLite 不含新增数据，不能直接切回。需停止写入并前向修复，或另做反向导出和跨存储核对。

## 验证记录

测试使用独立 MySQL 8.4.2，与其他项目服务无关。模型与金融来源沿用合成 fixture，没有新增真实金融质量结论。

- 最终全量 Python 回归（含测试库保护）：892 项通过，231.631 秒。
- 随后增加 CLI 维护租约、空事件表序号高水位与认证保留期边界，认证/MySQL 专项回归 30 项通过，27.141 秒。
- 前端单测：8 项通过；生产构建通过，已有大 chunk 提示仍存在。
- 最终浏览器验收：11 项通过，12.1 秒。
- 正式实机：健康接口 200、匿名私有接口 401；临时认证探针读到 3 个用户历史会话，随后已删除该探针会话。
- 研究实机核对：4 个研究、200 条预算预留、6 份可接纳产物摘要通过。
- MySQL/Web 重启后再次执行相同检查通过。Redis 原有 13 个会话与 1540 条事件逐项对照迁移快照一致。
- 正式 MySQL 逻辑备份包含 1184 条记录，保存为备份目录中的 `mysql-post-import.json`，权限 0600。

正式迁移的原始业务数据计数（独立副本演练同样通过）：

| 数据 | 演练记录数 |
| --- | ---: |
| 用户 | 1 |
| 认证会话 / 审计 / 限流 | 3 / 5 / 3 |
| Web 会话 / 执行 / 展示事件 / 投影游标 | 12 / 21 / 559 / 12 |
| 研究 / 任务尝试 / 预算预留 | 4 / 12 / 200 |
| 研究授权 / 事件 / 执行选择 | 302 / 32 / 1 |

所有被导入字段逐行核对，研究产物摘要与报告路径检查通过。随后将演练库的 1180 条记录进行一致逻辑备份，在独立 `finagent_test_restore` 库恢复并逐行核对通过。逻辑备份命令为 `scripts/mysql/backup_database.py`，导出使用 `--output`，恢复先预览再 `--restore FILE --apply`；恢复目标必须是已经升级且业务表为空的数据库。MySQL 备份一致性不替代 Redis/文件的协调恢复点。正式备份目录另保留 1184 条记录的切换后备份。演练根目录为 `/tmp/finagent-mysql-rehearsal-p9562i51`，对应备份和 `verified.json` 位于其中的 `migration/mysql_c5029d8b98f563a49e7b989af603c99a/`；包含实际数据副本，目录权限 0700，不能当作可公开的测试样本。演练只复制数据库及研究产物，没有声称在运行服务不停写时获得了完整跨存储恢复点。

测试入口：

```bash
# FINAGENT_MYSQL_URL 必须指向可丢弃的 finagent_test* database。
.venv/bin/python scripts/mysql/run_tests.py
.venv/bin/python scripts/mysql/run_tests.py tests.test_mysql_storage -q
# 同一隔离测试环境下，在 web/ 运行 npm run test:e2e。
```

测试包禁止回落到正式 `.env` 的数据库；未明确提供 `finagent_test*` URL 时，数据库操作直接失败。认证与浏览器 fixture 会创建独立临时测试 database，并清理它们。测试账号需要测试库建库权限；不要使用正式运行账号执行这类 fixture。普通运行账号仍只需要指定业务库的 DML 权限。

`validate_snapshot.py` 用于对现有数据副本演练，要求空的 `finagent_test*` database。它不停止现有 Web、不修改原库；正式切换仍要按上面的维护流程执行。

## 限制与剩余工作

账号准备、正式停写导入、运行配置切换和服务重启检查均已完成。脱敏验收记录位于正式备份目录的 `activation/verification.json`。未交付切流后的自动反向迁移、MySQL 主从/集群、跨主机调度，也没有性能优于原 SQLite 的结论。数据库、Redis 和文件需要保留对应恢复点；后续部署备份策略应覆盖三者。
