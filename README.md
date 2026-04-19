# Grafinder

Chinese version: [README.zh-CN.md](./README.zh-CN.md)

Grafinder is a local keyword-driven analysis agent. A user enters a keyword and a display intent in a local web page, and the system automatically discovers sources, crawls pages with Crawl4AI, extracts structured data through a selectable LLM API, stores the results in local PostgreSQL, and generates dashboards and panels in local Grafana.

## What Is Included

- A local input page at `http://localhost:8080`
- An Agent backend for search, crawling, extraction, deduplication, storage, and Grafana dashboard generation
- A natural-language refinement loop for redesigning the same Grafana dashboard without re-crawling data
- Web UI controls for provider selection, model preset selection, manual model override, and direct API key paste
- Local PostgreSQL for tasks, sources, crawled documents, and extracted records
- Local Grafana as the fixed visualization layer
- `Crawl4AI + Playwright` included in the deployment path
- Multi-provider LLM support for OpenAI, Doubao, or any OpenAI-compatible endpoint

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
  -> Browser opens local Grafana page
```

## Quick Start

0. Make sure Docker is running.

- macOS: start Docker Desktop
- Linux: make sure the `docker` service is available and your user has permission

1. Copy the environment template.

```bash
cp .env.example .env
```

2. Fill in at least one provider configuration in `.env`.

Recommended fields:

- `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY`
- `IKUNCODE_BASE_URL`, `IKUNCODE_MODEL`, `IKUNCODE_API_KEY`
- `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_API_KEY`
- `DOUBAO_BASE_URL`, `DOUBAO_MODEL`, `DOUBAO_API_KEY`
- `QWEN_BASE_URL`, `QWEN_MODEL`, `QWEN_API_KEY`
- `CUSTOM_LLM_BASE_URL`, `CUSTOM_LLM_MODEL`, `CUSTOM_LLM_API_KEY`

3. Adjust [config/llm_providers.example.json](/Users/xiaoyu/code/Grafinder/config/llm_providers.example.json) if you want different provider labels or defaults.

4. Start the full local stack.

```bash
docker compose up --build
```

If the `app` image cannot be built because of network or proxy issues, you can start the infrastructure only and run the Agent on the host:

```bash
docker compose up -d postgres grafana
./scripts/run_host.sh
```

5. Open:

- Agent page: [http://localhost:8080](http://localhost:8080)
- Grafana: [http://localhost:3001](http://localhost:3001)

Grafana dashboards now open directly in anonymous viewer mode. Admin credentials are still available for configuration changes:

- Username: `admin`
- Password: `grafinder_admin`

## LLM Provider Switching

Grafinder uses a provider registry instead of hard-coding a single vendor:

- The UI loads provider options from the local provider config file
- The UI exposes both preset model choices and a free-text model field
- `.env` can override each provider's default `Base URL` and `Model`
- The task form can temporarily override `Base URL`, `Model`, and `API Key`
- Any endpoint compatible with OpenAI Chat Completions can be connected

Example providers included by default:

- OpenAI
- IKunCode relay
- DeepSeek
- Doubao / Volcano Engine
- Qwen / DashScope
- Custom OpenAI-compatible endpoint

If you use a relay or proxy endpoint instead of the original vendor API, point the provider's `BASE_URL` to that endpoint and set the matching model name in `MODEL`.

The advanced section in the web UI includes:

- a provider dropdown
- a preset model dropdown
- a manual model input
- an API key paste field

## Refinement Loop

After the first Grafana dashboard is generated, the user can continue describing changes in natural language, for example:

- "Replace the table with a bar chart grouped by source"
- "Use weekly buckets for the trend chart"
- "Keep a KPI card and add an entity ranking panel"

The system will:

- reuse the data already stored for the task
- skip crawling and extraction
- redesign the dashboard based on the current dataset
- update the same local Grafana dashboard

## Storage Model

Main tables:

- `ingestion_tasks`
- `source_candidates`
- `documents`
- `extracted_records`

Grafana queries `extracted_records` through PostgreSQL and builds panels such as:

- trend charts
- ranking charts
- raw detail tables

## Local Deployment Notes

- Local deployment uses `docker compose` to run `app + postgres + grafana`
- Crawling dependencies are installed in [Dockerfile](/Users/xiaoyu/code/Grafinder/Dockerfile)
- If Crawl4AI fails for a target page, the backend falls back to `httpx + BeautifulSoup`
- Grafana datasource provisioning is defined in [grafana/provisioning/datasources/datasource.yml](/Users/xiaoyu/code/Grafinder/grafana/provisioning/datasources/datasource.yml)
- Grafana is configured with anonymous viewer access so the auto-opened dashboard URL does not stop at the login page
- If Docker image pulls fail, check Docker Desktop proxy settings in addition to terminal proxy variables
- If port `3000` is already in use, set `GRAFANA_PORT` in `.env`
- Host-mode runs also honor `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, and `NO_PROXY`

## Key Backend Files

- [app/main.py](/Users/xiaoyu/code/Grafinder/app/main.py)
- [app/services/task_runner.py](/Users/xiaoyu/code/Grafinder/app/services/task_runner.py)
- [app/services/dashboard_designer.py](/Users/xiaoyu/code/Grafinder/app/services/dashboard_designer.py)
- [app/services/crawl.py](/Users/xiaoyu/code/Grafinder/app/services/crawl.py)
- [app/services/extract.py](/Users/xiaoyu/code/Grafinder/app/services/extract.py)
- [app/services/grafana.py](/Users/xiaoyu/code/Grafinder/app/services/grafana.py)
- [app/services/llm_registry.py](/Users/xiaoyu/code/Grafinder/app/services/llm_registry.py)
- [scripts/run_host.sh](/Users/xiaoyu/code/Grafinder/scripts/run_host.sh)

## Possible Next Extensions

- Scheduled refresh jobs
- Multi-task queue and task history
- Richer chart templates and extraction schemas
- Better source scoring and site-specific crawl strategies
