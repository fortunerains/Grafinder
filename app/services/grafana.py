from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, UTC
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.schemas import DashboardDesign, DashboardPanelSpec


def _slugify(value: str) -> str:
    collapsed = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value).strip("-").lower()
    return collapsed or "grafinder-dashboard"


class GrafanaService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def publish_dashboard(
        self,
        task_id: int,
        keyword: str,
        intent: str,
        design: DashboardDesign,
        time_range: tuple[datetime | None, datetime | None] | None = None,
    ) -> tuple[str, str]:
        await self._wait_until_ready()
        uid = f"grafinder-task-{task_id}"
        title = design.title or f"{keyword} | {intent}"

        dashboard = {
            "id": None,
            "uid": uid,
            "title": title,
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "style": "dark",
            "tags": ["grafinder", keyword, intent],
            "refresh": "30s",
            "description": design.description or "",
            "panels": self._build_panels(task_id, design),
        }
        dashboard["time"] = self._dashboard_time(time_range)

        payload = {
            "dashboard": dashboard,
            "folderId": 0,
            "message": f"Grafinder task {task_id}",
            "overwrite": True,
        }

        async with httpx.AsyncClient(
            base_url=self.settings.grafana_api_url,
            auth=(self.settings.grafana_username, self.settings.grafana_password),
            timeout=30.0,
        ) as client:
            response = await client.post("/api/dashboards/db", json=payload)
            response.raise_for_status()

        query = urlencode(self._time_query_params(time_range) | {"orgId": 1})
        url = f"{self.settings.grafana_public_url}/d/{uid}/{_slugify(title)}?{query}"
        return uid, url

    async def _wait_until_ready(self, attempts: int = 20, delay_seconds: float = 2.0) -> None:
        async with httpx.AsyncClient(
            base_url=self.settings.grafana_api_url,
            auth=(self.settings.grafana_username, self.settings.grafana_password),
            timeout=10.0,
        ) as client:
            for _ in range(attempts):
                try:
                    response = await client.get("/api/health")
                    if response.is_success:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(delay_seconds)
        raise RuntimeError("Local Grafana is not reachable. Start Grafana and retry.")

    def _build_panels(self, task_id: int, design: DashboardDesign) -> list[dict]:
        panels: list[dict] = []
        for index, panel_spec in enumerate(design.panels, start=1):
            panel_builder = getattr(self, f"_build_{panel_spec.panel_type}_panel", None)
            if panel_builder is None:
                continue
            panels.append(panel_builder(task_id, index, panel_spec))
        return panels

    def _datasource(self) -> dict[str, str]:
        return {"type": "postgres", "uid": self.settings.grafana_datasource_uid}

    def _build_timeseries_panel(self, task_id: int, panel_id: int, spec: DashboardPanelSpec) -> dict:
        time_sql = f"date_trunc('{spec.time_grain}', COALESCE({spec.time_field}, created_at))"
        aggregate_sql = self._aggregate_sql(spec.metric_operation, spec.metric_field)
        group_sql = self._dimension_sql(spec.group_by)
        where_sql = self._where_sql(task_id, spec)
        if spec.group_by != "none":
            select_metric = ',\n  ' + group_sql + ' AS "metric"'
            group_clause = ", 2"
        else:
            series_name = self._sql_string(spec.series_name or self._default_series_name(spec))
            select_metric = f',\n  {series_name} AS "metric"'
            group_clause = ", 2"
        return {
            "id": panel_id,
            "title": spec.title,
            "description": spec.description or "",
            "type": "timeseries",
            "datasource": self._datasource(),
            "gridPos": self._grid_position(spec.panel_type, panel_id),
            "targets": [
                {
                    "refId": "A",
                    "datasource": self._datasource(),
                    "editorMode": "code",
                    "format": "time_series",
                    "rawQuery": True,
                    "rawSql": f"""
SELECT
  {time_sql} AS "time"{select_metric},
  {aggregate_sql} AS "value"
FROM extracted_records
WHERE {where_sql}
GROUP BY 1{group_clause}
ORDER BY 1
""".strip(),
                }
            ],
        }

    def _build_barchart_panel(self, task_id: int, panel_id: int, spec: DashboardPanelSpec) -> dict:
        dimension_sql = self._dimension_sql(spec.group_by if spec.group_by != "none" else "entity")
        aggregate_sql = self._aggregate_sql(spec.metric_operation, spec.metric_field)
        where_sql = self._where_sql(task_id, spec)
        return {
            "id": panel_id,
            "title": spec.title,
            "description": spec.description or "",
            "type": "barchart",
            "datasource": self._datasource(),
            "gridPos": self._grid_position(spec.panel_type, panel_id),
            "targets": [
                {
                    "refId": "A",
                    "datasource": self._datasource(),
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "rawSql": f"""
SELECT
  {dimension_sql} AS label,
  {aggregate_sql} AS value
FROM extracted_records
WHERE {where_sql}
GROUP BY 1
ORDER BY 2 {spec.sort_direction.upper()}
LIMIT {spec.limit}
""".strip(),
                }
            ],
            "options": {
                "orientation": "horizontal",
                "legend": {"displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "single"},
            },
        }

    def _build_table_panel(self, task_id: int, panel_id: int, spec: DashboardPanelSpec) -> dict:
        selected_columns = spec.columns or [
            "published_at",
            "title",
            "entity",
            "metric_name",
            "metric_value",
            "metric_unit",
            "source_name",
            "summary",
        ]
        column_sql = ",\n  ".join(self._table_column_sql(column) for column in selected_columns)
        where_sql = self._where_sql(task_id, spec)
        return {
            "id": panel_id,
            "title": spec.title,
            "description": spec.description or "",
            "type": "table",
            "datasource": self._datasource(),
            "gridPos": self._grid_position(spec.panel_type, panel_id),
            "targets": [
                {
                    "refId": "A",
                    "datasource": self._datasource(),
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "rawSql": f"""
SELECT
  {column_sql}
FROM extracted_records
WHERE {where_sql}
ORDER BY COALESCE(published_at, created_at) {spec.sort_direction.upper()} NULLS LAST, created_at DESC
LIMIT {spec.limit}
""".strip(),
                }
            ],
        }

    def _build_stat_panel(self, task_id: int, panel_id: int, spec: DashboardPanelSpec) -> dict:
        where_sql = self._where_sql(task_id, spec)
        if spec.value_mode == "latest" and spec.metric_field == "metric_value":
            raw_sql = f"""
SELECT
  metric_value AS value
FROM extracted_records
WHERE {where_sql}
ORDER BY COALESCE(published_at, created_at) DESC NULLS LAST, created_at DESC
LIMIT 1
""".strip()
        else:
            aggregate_sql = self._aggregate_sql(spec.metric_operation, spec.metric_field)
            raw_sql = f"""
SELECT
  {aggregate_sql} AS value
FROM extracted_records
WHERE {where_sql}
""".strip()
        return {
            "id": panel_id,
            "title": spec.title,
            "description": spec.description or "",
            "type": "stat",
            "datasource": self._datasource(),
            "gridPos": self._grid_position(spec.panel_type, panel_id),
            "targets": [
                {
                    "refId": "A",
                    "datasource": self._datasource(),
                    "editorMode": "code",
                    "format": "table",
                    "rawQuery": True,
                    "rawSql": raw_sql,
                }
            ],
            "options": {
                "colorMode": "value",
                "graphMode": "area",
                "justifyMode": "auto",
                "orientation": "auto",
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            },
        }

    @staticmethod
    def _aggregate_sql(operation: str, metric_field: str) -> str:
        if operation == "count" or metric_field == "*":
            return "COUNT(*)::double precision"
        mapping = {
            "sum": "SUM(metric_value)::double precision",
            "avg": "AVG(metric_value)::double precision",
            "max": "MAX(metric_value)::double precision",
            "min": "MIN(metric_value)::double precision",
        }
        return mapping.get(operation, "COUNT(*)::double precision")

    @staticmethod
    def _dimension_sql(group_by: str) -> str:
        mapping = {
            "entity": "COALESCE(NULLIF(entity, ''), 'unknown')",
            "source_name": "COALESCE(NULLIF(source_name, ''), 'unknown')",
            "metric_name": "COALESCE(NULLIF(metric_name, ''), 'unknown')",
            "title": "COALESCE(NULLIF(title, ''), 'unknown')",
            "metric_unit": "COALESCE(NULLIF(metric_unit, ''), 'unknown')",
        }
        return mapping.get(group_by, "COALESCE(NULLIF(entity, ''), 'unknown')")

    @staticmethod
    def _table_column_sql(column: str) -> str:
        mapping = {
            "published_at": 'COALESCE(published_at, created_at) AS published_at',
            "created_at": "created_at",
            "title": "title",
            "entity": "COALESCE(entity, '-') AS entity",
            "metric_name": "COALESCE(metric_name, '-') AS metric_name",
            "metric_value": "metric_value",
            "metric_unit": "COALESCE(metric_unit, '-') AS metric_unit",
            "source_name": "COALESCE(source_name, '-') AS source_name",
            "summary": "summary",
            "source_url": "source_url",
        }
        return mapping[column]

    @classmethod
    def _where_sql(cls, task_id: int, spec: DashboardPanelSpec) -> str:
        clauses = [f"task_id = {task_id}"]
        if spec.require_numeric:
            clauses.append("metric_value IS NOT NULL")

        metric_clause = cls._match_any_sql(["COALESCE(metric_name, '')"], spec.record_metric_names)
        if metric_clause:
            clauses.append(metric_clause)

        entity_clause = cls._match_any_sql(["COALESCE(entity, '')"], spec.record_entities)
        if entity_clause:
            clauses.append(entity_clause)

        unit_clause = cls._match_any_sql(["COALESCE(metric_unit, '')"], spec.record_units)
        if unit_clause:
            clauses.append(unit_clause)

        # When the dashboard already has explicit metric/entity/unit filters, free-form
        # keywords often over-constrain the query and produce empty panels.
        has_structured_filters = bool(metric_clause or entity_clause or unit_clause)
        keyword_clause = cls._match_any_sql(
            ["title", "COALESCE(entity, '')", "COALESCE(metric_name, '')", "summary"],
            spec.record_keywords,
        )
        if keyword_clause and not has_structured_filters:
            clauses.append(keyword_clause)

        return "\n  AND ".join(clauses)

    @classmethod
    def _match_any_sql(cls, columns: list[str], values: list[str]) -> str | None:
        normalized_values = [value.strip() for value in values if value and value.strip()]
        if not normalized_values:
            return None

        comparisons: list[str] = []
        for value in normalized_values:
            pattern = cls._sql_like_pattern(value)
            for column in columns:
                comparisons.append(f"{column} ILIKE {pattern}")
        return "(" + " OR ".join(comparisons) + ")"

    @staticmethod
    def _sql_like_pattern(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
        return f"'%{escaped}%'"

    @staticmethod
    def _sql_string(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _default_series_name(spec: DashboardPanelSpec) -> str:
        if spec.metric_field == "metric_value":
            return "数值"
        return "记录数"

    @staticmethod
    def _dashboard_time(time_range: tuple[datetime | None, datetime | None] | None) -> dict[str, str]:
        if not time_range:
            return {"from": "now-365d", "to": "now"}

        start, end = time_range
        if start is None and end is None:
            return {"from": "now-365d", "to": "now"}

        if start is None:
            start = (end or datetime.now(UTC)) - timedelta(days=365)
        if end is None:
            end = datetime.now(UTC)
        if start >= end:
            end = start + timedelta(days=30)
        return {"from": start.astimezone(UTC).isoformat(), "to": end.astimezone(UTC).isoformat()}

    @classmethod
    def _time_query_params(cls, time_range: tuple[datetime | None, datetime | None] | None) -> dict[str, str]:
        time_config = cls._dashboard_time(time_range)
        return {"from": time_config["from"], "to": time_config["to"]}

    @staticmethod
    def _grid_position(panel_type: str, panel_id: int) -> dict[str, int]:
        index = panel_id - 1
        if panel_type == "timeseries":
            return {"h": 8, "w": 24, "x": 0, "y": index * 8}
        if panel_type == "stat":
            return {"h": 5, "w": 8, "x": (index % 3) * 8, "y": (index // 3) * 5}
        row = max(index - 1, 0)
        return {"h": 8, "w": 12, "x": (row % 2) * 12, "y": 8 + (row // 2) * 8}
