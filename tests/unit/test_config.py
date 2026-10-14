"""Fumaca do config real do repositorio.

Nao mock: carrega config/environments.yaml e config/policy.yaml de verdade. Se
alguem renomear uma chave ou quebrar o YAML, falha aqui e nao as 3h da manha.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

from agent_nio_monitor.config.loader import CONFIG_DIR, ConfigError, load, load_policy
from agent_nio_monitor.config.schema import AppConfig

RAIZ = Path(__file__).resolve().parents[2]
SRC = RAIZ / "src" / "agent_nio_monitor"

# Onde um `event_type` pode ser EMITIDO. `notify/` renderiza, `store/` persiste e
# remonta -- nenhum dos dois decide que algo aconteceu.
CAMADAS_EMISSORAS = (SRC / "sources", SRC / "agent.py")

VOCABULARIO = re.compile(r"^[a-z][a-z0-9_]*_[a-z0-9_]+$")

# Regra sem emissor e uma promessa nao cumprida: o dono configurou alerta para um
# evento que nenhum codigo produz. E o bug que PASSOU durante auditorias, porque
# `reconciliation_gap` estava na regra, no template e no ADR -- e em nenhum
# emissor. Cada excecao aqui e decisao consciente e precisa dizer o porque.
# VAZIA. Todo `event_type` de `policy.yaml` tem emissor em `src/`.
#
# A allowlist existiu com dois nomes -- `credential_updated` e `source_down` --
# e cada um saiu quando o emissor correspondente foi ligado (Fase 1 e D5).
# Ela continua aqui de proposito: e o lugar onde uma excecao DEVE ser escrita,
# com o porque. Sem ela, nao ha onde registrar uma decisao consciente, e o
# proximo buraco vira literal de lista -- que e como `reconciliation_gap`
# sobreviveu a auditorias inteiras.
SEM_EMISSOR: dict[str, str] = {}


def _strings_de(no: ast.AST) -> set[str]:
    return {
        n.value
        for n in ast.walk(no)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _event_record_calls(arvore: ast.AST):
    """Todo `EventRecord(...)` do modulo."""
    return [
        no
        for no in ast.walk(arvore)
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Name) and no.func.id == "EventRecord"
    ]


def _chave_event_type(chamada: ast.Call) -> ast.expr | None:
    for kw in chamada.keywords:
        if kw.arg == "event_type":
            return kw.value
    return None


def _parametros_que_viram_event_type(arvore: ast.AST) -> set[str]:
    """Metodos que repassam um PARAMETRO para `EventRecord(event_type=...)`.

    Portainer faz assim (`sources/portainer/collector.py:_evento(tipo, ...)`) e os
    literais estao nos CHAMADORES. Sem resolver isso, os quatro eventos de
    container seriam invisiveis -- e o teste passaria vazio.
    """
    nomes: set[str] = set()
    for funcao in ast.walk(arvore):
        if not isinstance(funcao, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in funcao.args.args}
        for chamada in _event_record_calls(funcao):
            valor = _chave_event_type(chamada)
            if isinstance(valor, ast.Name) and valor.id in params:
                nomes.add(funcao.name)
    return nomes


@lru_cache(maxsize=1)
def _emissores() -> set[str]:
    """`event_type` que o codigo REALMENTE pode produzir.

    Nao e varrer literais: `coleta_execucoes_falhou` (nome de log do structlog),
    `workflow_id` (chave de dict) e `node_added` (kind de diff) tambem sao
    literais com underscore. O que emite um evento e a CONSTRUCAO do
    `EventRecord`, entao e ela que e rastreada.
    """
    arquivos = [p for p in CAMADAS_EMISSORAS[0].rglob("*.py") if "__pycache__" not in p.parts]
    arquivos.append(CAMADAS_EMISSORAS[1])

    emissores: set[str] = set()
    for caminho in arquivos:
        arvore = ast.parse(caminho.read_text(encoding="utf-8"))
        delegadores = _parametros_que_viram_event_type(arvore)

        # (a) literal direto: `event_type="workflow_changed"`
        for chamada in _event_record_calls(arvore):
            valor = _chave_event_type(chamada)
            if isinstance(valor, ast.Constant) and isinstance(valor.value, str):
                emissores.add(valor.value)

        # (b) variavel local: `tipo = "a" if ativo else "b"` (workflow_monitor.py)
        for no in ast.walk(arvore):
            if isinstance(no, ast.Assign) and isinstance(no.value, (ast.Constant, ast.IfExp)):
                for alvo in no.targets:
                    if isinstance(alvo, ast.Name):
                        emissores |= _strings_de(no.value)

        # (c) parametro repassado: literais nos chamadores do metodo delegador
        for no in ast.walk(arvore):
            if (
                isinstance(no, ast.Call)
                and isinstance(no.func, ast.Attribute)
                and no.func.attr in delegadores
                and no.args
                and isinstance(no.args[0], ast.Constant)
                and isinstance(no.args[0].value, str)
            ):
                emissores.add(no.args[0].value)

    # Docstrings nao entram: so a CONSTRUCAO do EventRecord emite. Foi por um
    # docstring que `reconciliation_gap` sobreviveu sem emissor algum.
    return {t for t in emissores if VOCABULARIO.match(t)}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N8N_API_KEY", "chave-de-teste")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost:5432/hermes")


class TestPolicyReal:
    def test_policy_real_carrega(self) -> None:
        p = load_policy()
        assert len(p.rules) >= 8
        assert p.aggregation.re_notify_after_minutes > 0

    def test_regra_tem_emissor_ou_excecao_documentada(self) -> None:
        """O portao que faltava: regra configurada SEM emissor = alerta que nunca sai.

        Direcao silenciosa. O dono le `policy.yaml`, ve a regra, e acredita que
        esta protegido. `reconciliation_gap` passou assim -- presente na regra,
        no template (`notify/templates.py`) e no ADR, e emitido em lugar nenhum
        durante auditorias inteiras. O unico jeito de pegar e cruzar a policy com
        o CODIGO, e essa lista de emissores nunca foi derivada do codigo: era
        um literal escrito a mao, que so confirmava a si mesmo.

        Emissor = literal de codigo nas camadas emissoras. Docstring nao conta:
        `sources/n8n/collector.py:6` cita `reconciliation_gap` no texto e nao o
        produz -- foi assim que ele sobreviveu.
        """
        declarados = {et for r in load_policy().rules for et in r.event_types}
        sem_emissor = sorted(declarados - _emissores())
        nao_documentados = [et for et in sem_emissor if et not in SEM_EMISSOR]
        assert not nao_documentados, (
            f"regra em policy.yaml sem nenhum emissor no codigo: {nao_documentados}\n"
            "Ou o evento foi esquecido de implementar -- e a alerta esta "
            "configurado para nunca sair --, ou o evento saiu do codigo e a "
            "regra sobrou. Se a excecao for intencional, registre em "
            "SEM_EMISSOR com o motivo."
        )
        obsoletos = sorted(set(SEM_EMISSOR) & _emissores())
        assert not obsoletos, (
            f"SEM_EMISSOR lista {obsoletos}, que JA tem emissor no codigo. "
            "A excecao ficou desatualizada -- remova da allowlist."
        )

    def test_scanner_encontra_os_emissores_reais(self) -> None:
        """O scanner pode degradar para "nao acha nada" e passar vazio, porque
        as duas direcoes acima ficam satisfatas com conjuntos vazios.

        Este e o contra-teste: fixa a lista completa de tipos que o codigo emite
        hoje. Se uma mudanca quebrar o rastreamento da indirecao (o parametro
        repassado do Portainer, o `IfExp` do toggle), a lista aqui cai e acusa --
        em vez do portao virar decoracao silenciosa.

        Os tres formatos que precisam de resolucao:
          - indireto por parametro  : container_stopped/unhealthy/removed/restart_loop
          - condicional em variavel : workflow_activated / workflow_deactivated
- literal direto          : workflow_changed e o resto
        """
        esperados = {
            "execution_failed", "reconciliation_gap", "source_down", "credential_updated",
            "desktop_flow_failed",
            "workflow_created", "workflow_changed", "workflow_activated", "workflow_deactivated",
            "stack_changed",
            "container_stopped", "container_unhealthy", "container_removed",
            "container_restart_loop", "container_resource_high",
        }
        assert _emissores() == esperados, (
            "a lista de emissores derivados mudou de forma inesperada. "
            f"Faltando: {sorted(esperados - _emissores())}. "
            f"Sobrando: {sorted(_emissores() - esperados)}. "
            "Se um evento foi REMOVIDO do codigo, apague a regra dele em policy.yaml. "
            "Se foi ADICIONADO, acrescente a lista e a regra."
        )

    def test_excecao_sem_emissor_tem_motivo_escrito(self) -> None:
        """A allowlist e onde um buraco pode se esconder de novo. Exigir motivo
        escrito e o que impede que ela vire a proxima lista literal que mente."""
        vazias = [k for k, v in SEM_EMISSOR.items() if not v.strip()]
        assert not vazias, f"excecao sem motivo escrito: {vazias}"

    def test_evento_emitido_tem_regra(self) -> None:
        """Direcao inversa: o codigo produz um evento que a politica nao cobre.

        `rules/engine.py:63` responde a isso com `log.debug` e `notify=False` --
        o evento e gravado em `event` e NINGUEM e avisado. O alerta some sem erro.

        Compara contra o conjunto BRUTO de tokens das camadas emissoras, nao so
        contra os que a politica conhece: um evento novo e sem regra precisa
        quebrar o build, e nao passar por cima da propria palavra.
        """
        declarados = {et for r in load_policy().rules for et in r.event_types}
        sem_regra = sorted(_emissores() - declarados - set(SEM_EMISSOR))
        assert not sem_regra, (
            f"evento emitido sem regra em policy.yaml: {sem_regra}\n"
            "Crie a regra -- sem ela o evento e registrado e descartado em silencio."
        )


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

    def test_env_example_nao_traz_host_de_uma_rede_especifica(self) -> None:
        """Divergencia real: o `.env.example` apontava para 192.168.0.140, que o
        proprio `config/environments.yaml` registra como FECHADO por sonda TCP.

        Host nao e segredo, entao `test_env_example_nao_tem_segredo_real` nao
        pegaria. E quem copia o arquivo novo e comeca com o n8n inacessivel --
        o defeito e invisivel ate a primeira coleta falhar."""
        texto = (RAIZ / ".env.example").read_text(encoding="utf-8")
        # So linhas ATIVAS: um comentario pode citar o host antigo para explicar
        # a divergencia -- e e util que explique.
        ativos = "\n".join(
            l for l in texto.splitlines() if not l.strip().startswith("#")
        )
        hosts = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", ativos)
        assert not hosts, (
            f"`.env.example` traz host de rede especifica: {sorted(set(hosts))}. "
            "Use `EXEMPLO` -- o host real esta em config/environments.yaml, que "
            "e versionado e nao tem segredo."
        )

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

    def test_todo_ciclo_de_coleta_fica_entre_25s_e_1min(self) -> None:
        """Decisao do dono: coleta entre 25 s e 1 min. O piso protege as fontes
        (o diff de workflows le 11,2 MB por ciclo); o teto garante o alerta rapido."""
        p = load().polling
        ciclos = {
            "erros": p.execution_errors_seconds, "workflows": p.workflow_full_seconds,
            "containers": p.container_state_seconds, "consumo": p.container_stats_seconds,
            "stacks": p.stack_changes_seconds,
        }
        fora = {nome: valor for nome, valor in ciclos.items() if not 25 <= valor <= 60}
        assert not fora, f"fora de 25-60 s: {fora}"

    def test_alerta_sai_da_fila_em_ate_50s(self) -> None:
        p = load().polling
        assert p.queue_max_delay_seconds == 50, "decisao do dono: alerta em ate 50 s"
        assert 5 <= p.queue_seconds <= 25

    @pytest.mark.parametrize("campo", ["execution_errors_seconds", "workflow_full_seconds"])
    @pytest.mark.parametrize("valor", [10, 24, 61, 300])
    def test_coleta_fora_de_25_a_60_s_e_recusada_no_boot(self, campo: str, valor: int) -> None:
        from pydantic import ValidationError

        from agent_nio_monitor.config.schema import PollingConfig

        with pytest.raises(ValidationError):
            PollingConfig(**{campo: valor})

    @pytest.mark.parametrize("valor", [4, 26, 60])
    def test_fila_fora_de_5_a_25_s_e_recusada_no_boot(self, valor: int) -> None:
        from pydantic import ValidationError

        from agent_nio_monitor.config.schema import PollingConfig

        with pytest.raises(ValidationError):
            PollingConfig(queue_seconds=valor)

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
