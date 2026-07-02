"""Register config-defined basic-memory projects into the Postgres DB.

basic-memory's Postgres backend skips `reconcile_projects_with_config()` at
startup (it assumes a cloud platform owns the schema/projects). On a self-hosted
DB that means projects defined in ~/.basic-memory/config.json never get inserted
into the `project` table, so `list_memory_projects` is empty and `write_note`
cannot route — even though `create_memory_project` reports "already exists"
(it checks the config file).

This runner calls basic-memory's own reconcile function to sync config -> DB.
It is idempotent. Run AFTER bm_migrate.py (the schema must exist first).

Self-contained: derives the connection from DB__DATABASE_URI and forces the
asyncpg driver; the role must have CREATE/DML on schema public (e.g. jtpgmr).

Run:  uvx --from basic-memory python scripts/bm_bootstrap.py
"""

import asyncio
import os
import sys

from basic_memory.config import BasicMemoryConfig, ConfigManager, DatabaseBackend
from basic_memory.services.initialization import reconcile_projects_with_config
from loguru import logger  # ships with basic-memory

_src = os.environ.get("DB__BASIC_MEMORY_ADMIN_URI")
if not _src:
    raise SystemExit(
        "Set DB__BASIC_MEMORY_ADMIN_URI as a Postgres DSN, e.g. postgresql://USER:PASS@localhost:5432/DB"
    )

if _src.startswith("postgresql+asyncpg://"):
    _url = _src
elif _src.startswith("postgresql://"):
    _url = "postgresql+asyncpg://" + _src.removeprefix("postgresql://")
else:
    raise SystemExit(
        "DB__BASIC_MEMORY_ADMIN_URI must be a postgresql:// or postgresql+asyncpg:// DSN"
    )

os.environ["BASIC_MEMORY_DATABASE_URL"] = _url


async def main() -> None:
    cfg: BasicMemoryConfig = ConfigManager().config
    cfg.database_backend = DatabaseBackend.POSTGRES
    print(f"backend={cfg.database_backend} projects_in_config={list(cfg.projects)}")
    await reconcile_projects_with_config(cfg)


if __name__ == "__main__":
    logger.remove()
    logger.add(sink=sys.stderr, level="INFO")

    asyncio.run(main())
