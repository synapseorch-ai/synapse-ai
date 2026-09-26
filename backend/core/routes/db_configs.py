"""
DB configuration management endpoints (CRUD + schema refresh).
"""
import os
from datetime import datetime
from fastapi import APIRouter, HTTPException
from core.models import DBConfig

router = APIRouter()

#: The drivers that take `connect_timeout` (psycopg2, psycopg, pymysql). SQLite's
#: has no such argument and refused every refresh (F208); the rest name theirs
#: differently, so they get none rather than a wrong one.
_TIMEOUT_BACKENDS = {"postgresql", "mysql", "mariadb"}


def _connect_args(connection_string: str) -> dict:
    from sqlalchemy.engine import make_url
    if make_url(connection_string).get_backend_name() in _TIMEOUT_BACKENDS:
        return {"connect_timeout": 10}
    return {}


async def load_db_configs() -> list[dict]:
    from core.store import collections
    return await collections.load("db_configs")


async def save_db_configs(configs: list[dict]):
    from core.store import collections
    await collections.save("db_configs", configs)


@router.get("/api/db-configs")
async def get_db_configs():
    return await load_db_configs()


@router.post("/api/db-configs")
async def create_db_config(config: DBConfig):
    configs = await load_db_configs()
    for i, c in enumerate(configs):
        if c["id"] == config.id:
            configs[i] = config.dict()
            await save_db_configs(configs)
            return config
    configs.append(config.dict())
    await save_db_configs(configs)
    return config


@router.delete("/api/db-configs/{config_id}")
async def delete_db_config(config_id: str):
    configs = await load_db_configs()
    configs = [c for c in configs if c["id"] != config_id]
    await save_db_configs(configs)
    return {"status": "success"}


@router.post("/api/db-configs/{config_id}/refresh-schema")
async def refresh_db_schema(config_id: str):
    configs = await load_db_configs()
    config = next((c for c in configs if c["id"] == config_id), None)
    if not config:
        raise HTTPException(status_code=404, detail="DB config not found")

    connection_string = config.get("connection_string", "")
    if not connection_string:
        raise HTTPException(status_code=400, detail="No connection string configured")

    try:
        from sqlalchemy import create_engine, inspect, text

        engine = create_engine(connection_string, connect_args=_connect_args(connection_string))
        inspector = inspect(engine)

        schema_lines = []
        table_names = inspector.get_table_names()

        for table_name in table_names:
            columns = inspector.get_columns(table_name)
            col_defs = ", ".join(
                f"{col['name']} ({str(col['type'])})" for col in columns
            )
            schema_lines.append(f"  {table_name}({col_defs})")

        schema_info = "Tables:\n" + "\n".join(schema_lines) if schema_lines else "No tables found."

        engine.dispose()

        config["schema_info"] = schema_info
        config["status"] = "connected"
        config["error_message"] = None
        config["last_tested"] = datetime.utcnow().isoformat()

        for i, c in enumerate(configs):
            if c["id"] == config_id:
                configs[i] = config
                break
        await save_db_configs(configs)

        return {"status": "connected", "schema_info": schema_info}

    except Exception as e:
        config["status"] = "error"
        config["error_message"] = str(e)
        config["last_tested"] = datetime.utcnow().isoformat()

        for i, c in enumerate(configs):
            if c["id"] == config_id:
                configs[i] = config
                break
        await save_db_configs(configs)

        raise HTTPException(status_code=400, detail=str(e))
