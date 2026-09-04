# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/database/db_session.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

from pathlib import Path
from contextvars import ContextVar

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from contextlib import asynccontextmanager
from .models import Base
import config
from config.db_config import mysql_db_config, sqlite_db_config, postgres_db_config

# Keep a cache of engines
_engines = {}

# 镜像双写时由 DualWriteStore 在调用镜像 store 前设置，让 DbStoreImplement 内部的
# get_session() 拿到 sqlite 引擎（此时 config.SAVE_DATA_OPTION 仍为 jsonl 等文件模式）。
# contextvar 是 asyncio task 局部变量，MAX_CONCURRENCY_NUM > 1 的多任务并发下互不串扰。
_session_db_type_override: ContextVar[str] = ContextVar("db_session_override", default=None)


async def create_database_if_not_exists(db_type: str):
    if db_type == "mysql" or db_type == "db":
        # Connect to the server without a database
        server_url = f"mysql+asyncmy://{mysql_db_config['user']}:{mysql_db_config['password']}@{mysql_db_config['host']}:{mysql_db_config['port']}"
        engine = create_async_engine(server_url, echo=False)
        async with engine.connect() as conn:
            await conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {mysql_db_config['db_name']}"))
        await engine.dispose()
    elif db_type == "postgres":
        # Connect to the default 'postgres' database
        server_url = f"postgresql+asyncpg://{postgres_db_config['user']}:{postgres_db_config['password']}@{postgres_db_config['host']}:{postgres_db_config['port']}/postgres"
        print(f"[init_db] Connecting to Postgres: host={postgres_db_config['host']}, port={postgres_db_config['port']}, user={postgres_db_config['user']}, dbname=postgres")
        # Isolation level AUTOCOMMIT is required for CREATE DATABASE
        engine = create_async_engine(server_url, echo=False, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            # Check if database exists
            result = await conn.execute(text(f"SELECT 1 FROM pg_database WHERE datname = '{postgres_db_config['db_name']}'"))
            if not result.scalar():
                await conn.execute(text(f"CREATE DATABASE {postgres_db_config['db_name']}"))
        await engine.dispose()


def get_async_engine(db_type: str = None):
    if db_type is None:
        db_type = config.SAVE_DATA_OPTION

    if db_type in _engines:
        return _engines[db_type]

    if db_type in ["json", "jsonl", "csv"]:
        return None

    engine_kwargs: dict = {}
    if db_type == "sqlite":
        # 首次裸启动时保证 database/ 父目录存在
        Path(sqlite_db_config["db_path"]).parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite+aiosqlite:///{sqlite_db_config['db_path']}"
        # aiosqlite 连接绑定创建时的 event loop：NullPool 杜绝跨 loop 复用（TestClient 每请求新 loop），
        # timeout 即 sqlite3 busy_timeout（爬虫持写锁时读/删等待而非立即报错）
        engine_kwargs = {"poolclass": NullPool, "connect_args": {"timeout": 30.0}}
    elif db_type == "mysql" or db_type == "db":
        db_url = f"mysql+asyncmy://{mysql_db_config['user']}:{mysql_db_config['password']}@{mysql_db_config['host']}:{mysql_db_config['port']}/{mysql_db_config['db_name']}"
    elif db_type == "postgres":
        db_url = f"postgresql+asyncpg://{postgres_db_config['user']}:{postgres_db_config['password']}@{postgres_db_config['host']}:{postgres_db_config['port']}/{postgres_db_config['db_name']}"
    else:
        raise ValueError(f"Unsupported database type: {db_type}")

    engine = create_async_engine(db_url, echo=False, **engine_kwargs)

    if db_type == "sqlite":
        # 多进程并发（爬虫子进程写 + API 进程读删）：WAL 让读写不互斥，synchronous=NORMAL 降低写放大
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    _engines[db_type] = engine
    return engine


async def create_tables(db_type: str = None):
    if db_type is None:
        db_type = config.SAVE_DATA_OPTION
    await create_database_if_not_exists(db_type)
    engine = get_async_engine(db_type)
    if engine:
        async with engine.begin() as conn:
            await conn.run_sync(_create_and_upgrade_schema, db_type)


def _create_and_upgrade_schema(sync_conn, db_type: str):
    """Create tables and add columns introduced after an existing DB was created.

    SQLAlchemy's ``create_all`` intentionally does not alter existing tables.  The
    project has a long-lived SQLite file, so small additive migrations belong here
    and must also run for the normal crawler/API startup path.
    """
    Base.metadata.create_all(sync_conn)
    if db_type != "sqlite":
        return

    additive_columns = {
        "douyin_aweme": {
            "douyin_id": "TEXT",
        },
        "douyin_aweme_comment": {
            "douyin_id": "TEXT",
        },
        "xhs_note": {
            "red_id": "TEXT",
        },
        "xhs_note_comment": {
            "red_id": "TEXT",
        },
    }
    inspector = inspect(sync_conn)
    for table_name, columns in additive_columns.items():
        if not inspector.has_table(table_name):
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name, column_type in columns.items():
            if column_name not in existing:
                sync_conn.execute(
                    text(
                        f'ALTER TABLE "{table_name}" '
                        f'ADD COLUMN "{column_name}" {column_type}'
                    )
                )
                existing.add(column_name)
        # ``create_all`` creates model indexes only for new tables. Existing
        # SQLite databases need every additive column index created explicitly.
        existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        for column_name in columns:
            index_name = f"ix_{table_name}_{column_name}"
            if column_name in existing and index_name not in existing_indexes:
                sync_conn.execute(
                    text(
                        f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                        f'ON "{table_name}" ("{column_name}")'
                    )
                )


@asynccontextmanager
async def get_session(db_type: str = None) -> AsyncSession:
    # 引擎取值优先级：显式参数 > 镜像 contextvar > config.SAVE_DATA_OPTION
    effective = db_type if db_type is not None else (_session_db_type_override.get() or config.SAVE_DATA_OPTION)
    engine = get_async_engine(effective)
    if not engine:
        yield None
        return
    AsyncSessionFactory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = AsyncSessionFactory()
    try:
        yield session
        await session.commit()
    except Exception as e:
        await session.rollback()
        raise e
    finally:
        await session.close()
