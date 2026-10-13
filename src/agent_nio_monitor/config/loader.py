"""Carregamento de config. Segredos NUNCA ficam em YAML -- so o nome da env var
(ver §10.1 do doc original, que estava certo nisso)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import ValidationError

from .schema import AppConfig, PolicyConfig

log = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"


def achar_env() -> Path | None:
    """Raiz do projeto primeiro; `config/` como legado.

    Antes so olhava `config/.env`, que nunca existiu -- a CLI nao subia.
    """
    for candidato in (PROJECT_ROOT / ".env", CONFIG_DIR / ".env"):
        if candidato.exists():
            return candidato
    return None

class ConfigError(RuntimeError):
    pass

def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"arquivo de config ausente: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalido em {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} deveria conter um mapeio no nivel raiz")
    return data


def _resolve_secret(env_name: str, *, required: bool = True) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    if required:
        # Dizer ONDE procurou. A versao anterior apontava para .env.example sem
        # nunca ter aberto o .env -- e mandou o operador para o lado errado.
        procurados = "\n".join(
            f"    {p}  {'(nao existe)' if not p.exists() else '(existe, mas a variavel esta vazia)'}"
            for p in (PROJECT_ROOT / ".env", CONFIG_DIR / ".env")
        )
        raise ConfigError(
            f"variavel de ambiente ausente: {env_name}\n"
            f"Procurei em:\n{procurados}\n"
            f"Defina a variavel no ambiente ou crie um dos arquivos a partir de "
            f".env.example."
        )
    return ""


def load(config_dir: Path | None = None) -> AppConfig:
    cfg_dir = config_dir or CONFIG_DIR

    env_file = achar_env()
    if env_file is not None:
        _carregar_dotenv(env_file)
        log.debug("dotenv_carregado", arquivo=str(env_file))
    # config_dir customizado (testes) pode ter o .env proprio
    if config_dir is not None and (config_dir / ".env").exists():
        _carregar_dotenv(config_dir / ".env")

    environments = _read_yaml(cfg_dir / "environments.yaml")
    policy = _read_yaml(cfg_dir / "policy.yaml")

    merged = _merge(
        environments,
        {
            # a URL do webhook pode vir do .env; o YAML traz o resto
            "mail": {
                "webhook_url": os.environ.get(
                    "MAIL_WEBHOOK_URL",
                    (environments.get("mail") or {}).get("webhook_url", ""),
                )
            },
            "llm": environments.get("llm") or {},
            "database": environments.get("database") or {},
            "policy": policy,
        },
    )

    dsn = os.environ.get("DATABASE_DSN")
    if dsn:
        merged.setdefault("database", {})["dsn"] = dsn

    try:
        cfg = AppConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(f"config invalida: {exc}") from exc

    if "${" in cfg.database.dsn:
        raise ConfigError(
            "DATABASE_DSN nao resolvido. Defina a variavel de ambiente "
            "(veja .env.example) em vez de deixar o placeholder no YAML."
        )

    # resolve a key da fonte ativa a partir da env
    _resolve_secret(cfg.active.n8n.api_key_env)
    if cfg.active.portainer:
        _resolve_secret(cfg.active.portainer.api_key_env, required=False)
    return cfg


def load_policy(config_dir: Path | None = None) -> PolicyConfig:
    cfg_dir = config_dir or CONFIG_DIR
    try:
        return PolicyConfig.model_validate(_read_yaml(cfg_dir / "policy.yaml"))
    except ValidationError as exc:
        raise ConfigError(f"policy.yaml invalida: {exc}") from exc


def _carregar_dotenv(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _merge(*maps: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for m in maps:
        for k, v in m.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _merge(out[k], v)
            else:
                out[k] = v
    return out