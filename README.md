# Grafinder

中文说明见 [README.zh-CN.md](/Users/xiaoyu/code/Grafinder/README.zh-CN.md)。

Grafinder 是一个本地运行的关键词分析 Agent。用户在本地网页输入关键词和展示意图后，系统会自动搜索来源、用 Crawl4AI 抓取页面、通过可切换的 LLM API 抽取结构化数据、写入本地 PostgreSQL，并自动在本地 Grafana 里生成 Dashboard / Panel。

## What Is Included

- 本地输入页面，运行在 `http://localhost:8080`
- Agent 后端，负责搜索、抓取、抽取、去重、落库和 Grafana 面板生成
- 自然语言二次改图，首版结果不满意时可直接输入新的调整要求并重建同一个 Grafana Dashboard
- 本地 PostgreSQL，保存任务、来源、页面和抽取结果
- 本地 Grafana，固定作为展示层
- `Crawl4AI + Playwright Chromium`，直接包含在本地部署方案中
- 多 LLM provider 预留能力，支持 OpenAI、Doubao 或任意 OpenAI 兼容接口

## Architecture

```text
Browser Form
  -> FastAPI Agent
  -> Search discovery
  -> Crawl4AI / fallback crawler
  -> LLM extraction
  -> PostgreSQL
  -> LLM dashboard redesign (optional refinement loop)
  -> Grafana Dashboard API
  -> Browser auto-opens local Grafana page
```

## Quick Start

0. 先确认本机 Docker daemon 已启动。

- macOS: 先打开 Docker Desktop
- Linux: 先确认 `docker` 服务和当前用户权限可用

1. 复制环境变量模板。

```bash
cp .env.example .env
```

2. 在 `.env` 中填好至少一个 provider 的完整默认配置。除了 API key，也建议把默认 `Base URL` 和 `Model` 一起填好, especially if you use a relay / proxy endpoint instead of the original vendor API.

Examples:

- `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`
- `DOUBAO_BASE_URL`, `DOUBAO_MODEL`, `DOUBAO_API_KEY`
- `CUSTOM_LLM_BASE_URL`, `CUSTOM_LLM_MODEL`, `CUSTOM_LLM_API_KEY`

3. 按需修改 [config/llm_providers.example.json](/Users/xiaoyu/code/Grafinder/config/llm_providers.example.json)。
   这里定义了 provider 名称、默认 Base URL、默认模型以及对应的 API key 环境变量。

4. 启动本地整套服务。

```bash
docker compose up --build
```

如果 `app` 镜像因为网络或代理问题暂时构建不出来，也可以先只起基础设施，再让 Agent 在宿主机 `.venv` 里运行：

```bash
docker compose up -d postgres grafana
./scripts/run_host.sh
```

5. 打开：

- Agent 输入页：[http://localhost:8080](http://localhost:8080)
- Grafana：[http://localhost:3001](http://localhost:3001)

默认 Grafana 账号密码：

- 用户名：`admin`
- 密码：`grafinder_admin`

## LLM Provider Switching

Grafinder 的 LLM 调用层是 provider registry 设计：

- 页面默认从 provider 配置文件读取可选项
- `.env` 也可以直接覆盖 provider 的默认 `Base URL` 和 `Model`
- 用户可以在提交任务时切换 provider
- 用户可以临时覆盖 `Base URL`、`Model`、`API Key`
- 只要接口兼容 OpenAI Chat Completions，就能接入

If you are using an OpenAI-compatible relay instead of the official OpenAI endpoint, you do not need to pretend it is the original vendor. Set the provider you want, then point `OPENAI_BASE_URL` or `CUSTOM_LLM_BASE_URL` to your relay, and set the matching model name in `OPENAI_MODEL` or `CUSTOM_LLM_MODEL`.

示例 provider 已预置：

- OpenAI
- Doubao / Volcano Engine
- Custom OpenAI Compatible

## Refinement Loop

首版 Grafana 图表生成后，如果不符合你的预期，可以直接回到 Agent 输入页，在“图表不合适时，继续用自然语言调整”输入框里继续描述，例如：

- “不要表格，改成按来源的柱状图”
- “时间趋势改成按周统计”
- “保留一个核心 KPI 卡片，再加一个实体排行”

系统会：

- 复用当前任务已经落库的数据
- 不重新抓取网页
- 让大模型基于当前数据结构重新设计面板
- 覆盖更新同一个本地 Grafana Dashboard

这意味着你可以围绕同一批数据持续调整展示方式，而不需要重复跑整条采集链路。

## Storage Model

主要表：

- `ingestion_tasks`
- `source_candidates`
- `documents`
- `extracted_records`

Grafana 默认通过 PostgreSQL datasource 查询 `extracted_records`，自动生成：

- 趋势图
- 排行图
- 原始明细表

并根据用户展示意图调整面板排序。

## Local Deployment Notes

- 本地部署由 `docker compose` 统一拉起 `app + postgres + grafana`
- 抓取工具 `Crawl4AI` 和 `Playwright Chromium` 在 [Dockerfile](/Users/xiaoyu/code/Grafinder/Dockerfile) 中直接安装
- 如果 `Crawl4AI` 运行失败，后端会自动退回 `httpx + BeautifulSoup` 作为兜底抓取
- Grafana datasource 在 [grafana/provisioning/datasources/datasource.yml](/Users/xiaoyu/code/Grafinder/grafana/provisioning/datasources/datasource.yml) 中自动预配置
- 如果 `docker compose up --build` 拉镜像失败，请检查 Docker Desktop 的代理设置；当前环境里出现过 `overrideProxyHttp/overrideProxyHttps -> 127.0.0.1:7890` 导致 Docker daemon 无法访问 Docker Hub 的情况
- 如果本机 `3000` 已被其他服务占用，可通过 `.env` 中的 `GRAFANA_PORT` 调整 Grafana 对外端口

## Key Backend Files

- [app/main.py](/Users/xiaoyu/code/Grafinder/app/main.py)
- [app/services/task_runner.py](/Users/xiaoyu/code/Grafinder/app/services/task_runner.py)
- [app/services/dashboard_designer.py](/Users/xiaoyu/code/Grafinder/app/services/dashboard_designer.py)
- [app/services/crawl.py](/Users/xiaoyu/code/Grafinder/app/services/crawl.py)
- [app/services/extract.py](/Users/xiaoyu/code/Grafinder/app/services/extract.py)
- [app/services/grafana.py](/Users/xiaoyu/code/Grafinder/app/services/grafana.py)
- [app/services/llm_registry.py](/Users/xiaoyu/code/Grafinder/app/services/llm_registry.py)
- [scripts/run_host.sh](/Users/xiaoyu/code/Grafinder/scripts/run_host.sh)

## Next Extensions

- 定时更新
- 多任务队列和历史任务看板
- 更丰富的图表模板与领域抽取 schema
- 针对特定行业站点的来源评分和抓取策略
