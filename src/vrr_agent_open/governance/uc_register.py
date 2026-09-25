"""Register the VRR Postgres schemas/tables/functions in Unity Catalog OSS as the
governance catalog-of-record (RBAC + lineage metadata + credential vending).

Feasibility (see docs/design.md): UC OSS is a CATALOG, not a query engine. It does
not intercept Postgres queries in OSS (Lakehouse Federation is Databricks-only). So
UC here is the *catalog-of-record*: it holds the governed schema/table/function
metadata + access grants; the agent resolves names + permission from UC, then
executes against Postgres. This gives governance = catalog + access decisions +
lineage, without query interception.

Uses the Unity Catalog OSS REST API (http://localhost:8080 by default).
"""
from __future__ import annotations

import httpx

from ..config import load_config

CFG = load_config()

SCHEMAS = ["vrr_raw", "vrr_curated", "vrr_agent"]
# governed asset inventory (mirrors the Databricks "declared resources" model)
TABLES = {
    "vrr_curated": ["completion_contrib", "pattern_vrr_monthly"],
    "vrr_agent": ["pattern_memory", "adjustment_history", "action_queue",
                  "safety_limits", "reservoir_knowledge", "knowledge_registry"],
}


def _uc(method: str, path: str, body: dict | None = None) -> dict:
    r = httpx.request(method, f"{CFG.uc_url}/api/2.1/unity-catalog/{path}",
                      json=body, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else {}


def register() -> None:
    _uc("POST", "catalogs", {"name": CFG.uc_catalog,
                             "comment": "VRR governance catalog-of-record (Postgres-backed)"})
    for sch in SCHEMAS:
        _uc("POST", "schemas", {"name": sch, "catalog_name": CFG.uc_catalog})
    for sch, tbls in TABLES.items():
        for t in tbls:
            _uc("POST", "tables", {
                "name": t, "catalog_name": CFG.uc_catalog, "schema_name": sch,
                "table_type": "EXTERNAL", "data_source_format": "POSTGRESQL",
                "storage_location": f"{CFG.pg_dsn}#{sch}.{t}",
                "columns": [],   # populate from information_schema for full lineage
            })
    print(f"registered {CFG.uc_catalog} + {len(SCHEMAS)} schemas in Unity Catalog OSS")


if __name__ == "__main__":
    register()
