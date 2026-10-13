"""Fumaca do config real do repositorio.

Nao mock: carrega config/environments.yaml e config/policy.yaml de verdade. Se
alguem renomear uma chave ou quebrar o YAML, falha aqui e nao as 3h da manha.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_nio_monitor.config.loader import CONFIG_DIR, ConfigError, load, load_policy
from agent_nio_monitor.config.schema import AppConfig

RAIZ = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_API_KEY", "chave-de-teste")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost:5432/hermes")


class TestPolicyReal:
    def test_policy_real_carrega(self) -> None:
        p = load_policy()
        assert len(p.rules) >= 8
        assert p.aggregation.re_notify_after_minutes > 0

    def test_toda_regra_tem_event_type_conhecido(self) -> None:
        """Um event_type sem regra correspondente = evento silenciosamente
        descartado. Aqui ele falha no boot, nao em producao."""
        permitidos = {
            "execution_failed", "workflow_changed", "workflow_activated",
            "workflow_deactivated", "credential_updated", "container_stopped",
            "container_unhealthy", "container_restart_loop", "reconciliation_gap",
            "source_down", "workflow_created", "stack_changed", "container_removed",
            "container_resource_high",
        }
        p = load_policy()
        for r in p.rules:
            for et in r.event_types:
                assert et in permitidos, f"event_type desconhecido na regra {r.name}: {et}"

    def test_existe_regra_para_cada_event_type_do_codigo(self) -> None:
        """O inverso: evento emitido sem regra = perdido."""
        cobertos: set[str] = set()
        for r in load_policy().rules:
            cobertos.update(r.event_types)
        emitidos = (
            "execution_failed", "source_down", "reconciliation_gap", "workflow_created",
            "workflow_changed", "workflow_activated", "workflow_deactivated", "stack_changed",
            "container_stopped", "container_unhealthy", "container_removed",
            "container_restart_loop", "container_resource_high",
        )
        for et in emitidos:
            assert et in cobertos, f"{et} e emitido pelo codigo mas nao tem regra"


class TestConfigReal:
    def test_environments_yaml_carrega(self) -> None:
        cfg = load()
        assert isinstance(cfg, AppConfig)
        assert cfg.active_environment == "production"
        assert cfg.active.n8n.url
        assert cfg.active.portainer is not None, "Portainer e a segunda zona monitorada"

    def test_segredo_nao_vem_do_yaml(self) -> None:
        """Segredo em YAML e o erro classico. O YAML so pode ter o NOME da env."""
        texto = (CONFIG_DIR / "environments.yaml").read_text(encoding="utf-8")
        assert "api_key:" not in texto
        assert "password:" not in texto
        assert "api_key_env" in texto

    def test_env_example_nao_tem_segredo_real(self) -> None:
        """Segredos do .env.example ficam VAZIOS. Valor de nao-segredo (URL, nome
        de modelo, LOG_LEVEL) pode vir preenchido -- o que nao pode e segredo."""
        texto = (RAIZ / ".env.example").read_text(encoding="utf-8")
        proibidos = ["jfc2025", "password=postgres", "napi_", "eyJ", "Bearer "]
        for p in proibidos:
            assert p not in texto, f"segredo real vazou para o .env.example: {p}"

        chaves_de_segredo = {"N8N_API_KEY", "PORTAINER_API_KEY"}
        for linha in texto.splitlines():
            if "=" not in linha or linha.strip().startswith("#"):
                continue
            chave, _, valor = linha.partition("=")
            if chave.strip() in chaves_de_segredo:
                assert valor.strip() == "", f"{chave.strip()} tem segredo no .env.example"

    def test_env_esta_no_gitignore(self) -> None:
        """`.env` com segredo commitado nao se desfaz com um `git rm` do log."""
        gitignore = RAIZ / ".gitignore"
        assert gitignore.exists(), ".gitignore ausente: .env seria commitado"
        entradas = {
            linha.strip()
            for linha in gitignore.read_text(encoding="utf-8").splitlines()
            if linha.strip() and not linha.strip().startswith("#")
        }
        assert ".env" in entradas

    def test_cadencia_de_workflow_respeita_o_custo_medido(self) -> None:
        """ADR D6: 11,2 MB por pagina. A 60 s seriam ~16 GB/dia. Piso de 3 min
        (decisao do dono: edicao de workflow precisa chegar rapido); abaixo
        disso o poll do corpo completo estoura a banda do n8n."""
        assert load().polling.workflow_full_seconds >= 180

    def test_ambiente_inexistente_e_rejeitado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config invalida tem que falhar no boot. Falhar em producao as 3h da
        manha e o preco de nao validar aqui."""
        from pydantic import ValidationError

        monkeypatch.setenv("N8N_API_KEY", "x")
        with pytest.raises(ValidationError, match="active_environment"):
            AppConfig.model_validate(
                {
                    "environments": {
                        "production": {"n8n": {"url": "http://x", "api_key_env": "K"}}
                    },
                    "mail": {"webhook_url": "http://x"},
                    "llm": {"base_url": "http://x", "model": "m"},
                    "database": {"dsn": "postgresql://x"},
                    "policy": {"rules": [{"name": "r", "event_types": ["execution_failed"]}]},
                    "active_environment": "producao_que_nao_existe",
                }
            )

    def test_cli_sobe_sem_variaveis_herdadas(self):
        """A CLI tem que subir num processo onde NADA foi herdado.

        Este teste e o portao que faltava. A suite inteira rodava com as variaveis
        ja populadas pelo `tests/conftest.py`, o que mascarava um defeito real: o
        loader procurava o `.env` em `config/`, onde ele nunca existiu. A CLI nao
        subia fora do pytest -- e o teste de `load()` continuava verde.

        Por isso subprocesso: e o unico jeito de provar que nada "ajudou".
        """
        limpo = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "DATABASE_DSN",
                "N8N_API_KEY",
                "PORTAINER_API_KEY",
                "MAIL_WEBHOOK_URL",
                "OLLAMA_URL",
                "OLLAMA_MODEL",
            }
        }
        r = subprocess.run(
            [sys.executable, "-m", "agent_nio_monitor.main", "doctor"],
            env=limpo,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(RAIZ),
        )
        assert "nao resolvido" not in r.stderr, (
            f"a CLI nao achou a configuracao sem variaveis herdadas:\n{r.stderr[:600]}"
        )
        # a versao e OPCIONAL nesta instalacao (verificado: /healthz devolve so
        # status). O que nao pode faltar e a tabela de capacidades.
        assert "capability" in r.stdout.lower() or "feature" in r.stdout.lower(), (
            f"doctor nao imprimiu a tabela de capacidades:\n{r.stdout[:600]}"
        )


class TestErrosDeConfig:
    @pytest.fixture(autouse=True)
    def _sem_dotenv_real(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Isola o `.env` REAL.

        Antes da correcao do `achar_env()`, `monkeypatch.delenv` bastava: o
        loader nem olhava o arquivo. Agora ele olha -- e volta a popular as
        variaveis, entao o `delenv` do teste nao surte efeito e o teste passa a
        verificar o opposite do que diz.

        Apontar `PROJECT_ROOT` para um diretorio vazio simula "nao ha .env em
        lugar nenhum", sem precisar mexer no arquivo de verdade.
        """
        import agent_nio_monitor.config.loader as loader

        monkeypatch.setattr(loader, "PROJECT_ROOT", tmp_path)

    def test_segredo_ausente_falha_com_instrucao(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("N8N_API_KEY", raising=False)
        monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost:5432/h")
        with pytest.raises(ConfigError, match="N8N_API_KEY"):
            load()

    def test_mensagem_diz_onde_procurou(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mensagem anterior apontava para .env.example sem nunca ter aberto o
        .env -- mandava o operador para o lado errado."""
        monkeypatch.delenv("N8N_API_KEY", raising=False)
        monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost:5432/h")
        with pytest.raises(ConfigError) as ei:
            load()
        texto = str(ei.value)
        assert "Procurei em:" in texto
        assert ".env" in texto

    def test_dsn_placeholder_e_rejeitado(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("N8N_API_KEY", "x")
        monkeypatch.delenv("DATABASE_DSN", raising=False)
        with pytest.raises(ConfigError, match="DATABASE_DSN"):
            load()

    def test_yaml_quebrado_falha(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("N8N_API_KEY", "x")
        (tmp_path / "environments.yaml").write_text("environments: [\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="YAML invalido"):
            load(tmp_path)