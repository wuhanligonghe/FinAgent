# FinSight（FinAgent）

FinSight 是一个面向 A 股的 AI 金融研究助手，提供 Web 工作台和命令行入口。你可以用自然语言查询行情和财报，在同一会话中追问，也可以发起后台深度研究，生成带证据引用的 Markdown 报告。

> 试试问「分析贵州茅台最近三年的经营现金流，列出数据来源和报告期。」

## 主要功能

- **金融问答**：按需调用行情、财报和新闻工具，展示工具调用进度，并保留数据来源和财务计算依据。
- **深度研究**：后台执行分阶段研究，支持查看进度、查阅报告证据、导出 Markdown 和继续追问。
- **Agent Harness**：支持按需加载工具和 Skills、任务清单、上下文压缩、大结果归档回读和长期记忆。

后端使用 Python 和 FastAPI，前端使用 React、TypeScript 和 Vite。Redis 保存会话运行状态，MySQL 保存账号、Web 元数据和研究任务记录。

## 本地启动

准备 Python 3.11+、Node.js（含 npm）、Redis、MySQL 8.4，以及支持工具调用的模型 API。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -c deploy/mysql-constraints.txt -e '.[web]'
test -f .env || cp .env.example .env
```

在 `.env` 中填写 `FINAGENT_BASE_URL`、`FINAGENT_MODEL`、`FINAGENT_API_KEY` 和运行账号的 `FINAGENT_MYSQL_URL`。Redis 地址可通过 `FINAGENT_REDIS_URL` 修改，默认连接 `redis://127.0.0.1:6379/0`。首次运行前请使用维护账号[初始化 MySQL 表结构](doc/features/MYSQL_STORAGE.md#初始化与启动)。

```bash
finagent auth create-user researcher
(cd web && npm ci && npm run build)
finsight-web --port 8000
```

打开 `http://127.0.0.1:8000` 并使用刚创建的账号登录。也可以运行 `finagent` 进入命令行会话。

## 文档与许可

- [运行指南](doc/guides/RUNNING.md) · [Web 工作台](doc/guides/WEB_APP.md) · [项目架构](doc/overview/ARCHITECTURE.md)
- [Apache 2.0 许可证](LICENSE) · [项目来源与致谢](NOTICE) · [第三方声明](THIRD_PARTY_NOTICES)

本项目用于学习和研究，不构成投资建议。
