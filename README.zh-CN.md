# Grafinder 中文说明

Grafinder 是一个本地运行的关键词分析 Agent。用户在本地网页中输入关键词和展示意图后，系统会自动完成来源发现、网页抓取、结构化抽取、本地落库，并自动在本地 Grafana 中生成或更新可视化面板。

## 项目能力

- 本地输入页面，地址为 `http://localhost:8080`
- 自动搜索相关网页来源
- 使用 `Crawl4AI + Playwright` 抓取正文内容
- 使用可切换的 LLM Provider 做结构化抽取
- 将结果写入本地 PostgreSQL
- 自动调用 Grafana API 生成本地 Dashboard / Panel
- 如果首版图表不满意，可继续输入自然语言要求，基于现有数据重新生成图表

## 系统流程

```text
用户输入关键词与展示意图
  -> Agent 规划搜索与抽取重点
  -> 自动发现网页来源
  -> Crawl4AI 抓取网页内容
  -> LLM 抽取结构化数据
  -> PostgreSQL 本地落库
  -> LLM 设计 Grafana 面板
  -> Grafana API 创建或覆盖本地 Dashboard
  -> 浏览器自动打开本地 Grafana 页面
```

## 快速启动

1. 确认 Docker Desktop 已启动。
2. 复制环境变量模板。

```bash
cp .env.example .env
```

3. 在 `.env` 中填入至少一个可用的 LLM 配置。除了 Key，也建议把默认 `Base URL` 和 `Model` 一起配好，尤其是你走中转站或 OpenAI 兼容接口时，例如：

- `OPENAI_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_API_KEY`
- `DOUBAO_BASE_URL`
- `DOUBAO_MODEL`
- `DOUBAO_API_KEY`
- `CUSTOM_LLM_BASE_URL`
- `CUSTOM_LLM_MODEL`
- `CUSTOM_LLM_API_KEY`

4. 如有需要，修改 [config/llm_providers.example.json](/Users/xiaoyu/code/Grafinder/config/llm_providers.example.json) 中的 Provider 列表。

5. 启动本地整套服务。

```bash
docker compose up --build
```

如果 `app` 镜像因为网络或代理问题暂时构建不出来，也可以先只启动基础设施，再让 Agent 在宿主机 `.venv` 中运行：

```bash
docker compose up -d postgres grafana
./scripts/run_host.sh
```

6. 打开以下地址：

- Agent 页面：[http://localhost:8080](http://localhost:8080)
- Grafana 页面：[http://localhost:3001](http://localhost:3001)

Grafana 默认登录信息：

- 用户名：`admin`
- 密码：`grafinder_admin`

## LLM Provider 机制

Grafinder 没有把模型接口写死为 OpenAI，而是做成了 Provider Registry：

- 页面中可以直接切换 Provider
- Provider 默认值现在也支持直接从 `.env` 读取 `BASE_URL` 和 `MODEL`
- 可以临时覆盖 Base URL、Model、API Key
- 兼容 OpenAI Chat Completions 风格的接口都可以接入

如果你访问的是 OpenAI 兼容中转站，而不是官方原厂接口，最简单的做法就是：

- 仍然选择 `openai` 或 `custom` Provider
- 在 `.env` 中把 `OPENAI_BASE_URL` 或 `CUSTOM_LLM_BASE_URL` 改成你的中转站地址
- 把 `OPENAI_MODEL` 或 `CUSTOM_LLM_MODEL` 改成中转站实际支持的模型名
- 再填对应的 API Key

默认示例包括：

- OpenAI
- 豆包 / 火山引擎
- 自定义 OpenAI 兼容接口

## 自然语言二次改图

首版 Grafana 面板生成后，如果图表样式不是你想要的，可以回到 Agent 页面，在“继续用自然语言调整”输入框继续描述：

- “不要表格，改成按来源的柱状图”
- “趋势图按周统计”
- “加一个核心 KPI 卡片，再保留原始明细”

系统会：

- 复用当前任务已经入库的数据
- 不重新抓取网页
- 让大模型重新设计 Dashboard 结构
- 覆盖更新同一个本地 Grafana Dashboard

## 本地部署组成

- `app`: FastAPI Agent 服务
- `postgres`: 本地 PostgreSQL 数据库
- `grafana`: 本地 Grafana 展示层

抓取运行环境也已经包含在应用镜像中：

- `Crawl4AI`
- `Playwright Chromium`

## 主要数据表

- `ingestion_tasks`
- `source_candidates`
- `documents`
- `extracted_records`

Grafana 默认直接查询 `extracted_records` 这张表生成图表。

## 关键代码位置

- [app/main.py](/Users/xiaoyu/code/Grafinder/app/main.py)
- [app/services/task_runner.py](/Users/xiaoyu/code/Grafinder/app/services/task_runner.py)
- [app/services/dashboard_designer.py](/Users/xiaoyu/code/Grafinder/app/services/dashboard_designer.py)
- [app/services/crawl.py](/Users/xiaoyu/code/Grafinder/app/services/crawl.py)
- [app/services/extract.py](/Users/xiaoyu/code/Grafinder/app/services/extract.py)
- [app/services/grafana.py](/Users/xiaoyu/code/Grafinder/app/services/grafana.py)
- [app/services/llm_registry.py](/Users/xiaoyu/code/Grafinder/app/services/llm_registry.py)
- [scripts/run_host.sh](/Users/xiaoyu/code/Grafinder/scripts/run_host.sh)

## 常见问题

### 1. Docker 拉镜像失败

如果 `docker compose up --build` 拉镜像失败，请先检查 Docker Desktop 自身的代理设置，而不只是终端里的 `http_proxy/https_proxy`。Docker daemon 使用的代理要能真正连通 Docker Hub。

### 2. Grafana 的 3000 端口被占用

如果本机已有其他服务占用 `3000`，可以在 `.env` 中修改 `GRAFANA_PORT`，例如改成 `3001`。

### 3. 任务创建时报缺少 API Key

说明当前选中的 Provider 没有从环境变量或页面高级配置里读到 Key。补齐后重试即可。

### 4. 我走的是中转站，不是 OpenAI 官方

可以，系统支持这种方式。推荐直接在 `.env` 中配置：

- `OPENAI_BASE_URL` + `OPENAI_MODEL` + `OPENAI_API_KEY`
- 或 `CUSTOM_LLM_BASE_URL` + `CUSTOM_LLM_MODEL` + `CUSTOM_LLM_API_KEY`

页面里的高级配置也可以临时覆盖这三项。

### 5. 首版图表不理想

直接使用页面里的自然语言调整输入框继续描述，不需要重新做抓取和抽取。

## 后续可扩展方向

- 定时更新任务
- 多任务队列与历史记录
- 针对特定行业的抽取 Schema
- 更丰富的 Grafana 面板模板
