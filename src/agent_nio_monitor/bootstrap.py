"""Monta o `Agent` com o banco real: abre o pool, migra e garante as particoes."""

from __future__ import annotations

from .agent import Agent
from .config.loader import ConfigError
from .config.schema import AppConfig
from .store.db import Database
from .store.maintenance import PartitionMaintenance
from .store.postgres_queue import PostgresQueue
from .store.postgres_repository import PostgresRepository
from .store.repository import Repository


def build(cfg: AppConfig, repo: Repository | None = None) -> Agent:
    if repo is not None:
        return Agent(cfg, repo)
    db = make_db(cfg)
    db.open()
    db.migrate()
    db.ensure_partitions()
    manutencao = PartitionMaintenance(db, cfg.database.retention_days)
    return Agent(cfg, PostgresRepository(db), PostgresQueue(db), maintenance=manutencao)


def make_db(cfg: AppConfig) -> Database:
    dsn = cfg.database.dsn.replace("${DATABASE_DSN}", "")
    if not dsn:
        raise ConfigError("DATABASE_DSN ausente (ver .env.example)")
    return Database(dsn, cfg.database.db_schema, cfg.database.pool_min, cfg.database.pool_max)
