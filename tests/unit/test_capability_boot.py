"""D2: a sonda de capacidade no boot.

O ADR D2 diz "nunca assumir versao: sonda, grava, adapta", e a docstring de
`capabilities.py` promete que "o boot imprime a versao real do n8n". Nenhuma das
duas era verdade: `detect()` so rodava em `doctor`, por mao, e
`repo.save_capabilities()` so era chamado de teste de integracao. A tabela
`capabilities` ficava vazia em producao e a docstring de `agent.py` prometia uma
ordem de boot que nao existia.

O que se trava:
1. o boot sonda e grava;
2. sonda quebrada NAO derruba o agente -- trocar alerta perdido por agente
   inteiro parado e perder o monitor;
3. a sonda e paralelo, e produz o mesmo conjunto que a versao sequencial.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from agent_nio_monitor.capabilities import ENDPOINTS, detect, fontes_do_config
from agent_nio_monitor.config.loader import load
from agent_nio_monitor.main import Agent
from agent_nio_monitor.store.memory_repository import InMemoryRepository

N8N = "http://n8n.test"
PTAI = "https://portainer.test"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Isola o `.env` REAL.

    `config.loader.load()` chama `_carregar_dotenv`, que usa `setdefault` -- e
    repovoa as variaveis que o teste acabou de apagar. Sem apontar
    `PROJECT_ROOT` para um diretorio vazio, `delenv` nao surte efeito e o teste
    verifica o oposto do que diz (o mesmo problema ja tratado em
    `test_config.py::TestErrosDeConfig`). O `.env` desta maquina tem
    credenciais reais de n8n e Portainer.
    """
    import agent_nio_monitor.config.loader as loader

    monkeypatch.setattr(loader, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("N8N_API_KEY", "k")
    monkeypatch.setenv("PORTAINER_API_KEY", "t")
    monkeypatch.setenv("DATABASE_DSN", "postgresql://u:s@localhost/h")
    monkeypatch.delenv("PORTAINER_URL", raising=False)


def _responde_tudo(base: str) -> None:
    """Responde 200 a qualquer rota desta fonte -- a sonda mede ROTA, nao conteudo."""
    respx.get(url__startswith=_rota(base)).mock(
        return_value=httpx.Response(200, json={"data": [], "nextCursor": None})
    )


def _rota(url: str) -> str:
    """Padrao de rota que identifica a FONTE.

    Nao usar `host=`: nesta rede o n8n (:5678) e o Portainer (:9443) dividem o
    mesmo `192.168.0.160`, e um padrao so de host deixa as duas rotas colidirem --
    o teste passaria medindo a fonte errada, que e pior do que falhar. O prefixo
    da URL base separa as duas sem ambiguidade.
    """
    return url.rstrip("/")


class TestMontagemDasFontes:
    def test_fontes_vem_da_config_ativa(self) -> None:
        cfg = load()
        fontes = fontes_do_config(cfg)
        assert fontes["n8n"]["url"] == cfg.active.n8n.url
        assert fontes["n8n"]["api_key"] == "k"
        assert "portainer" in fontes

    def test_sem_credencial_de_portainer_a_fonte_some(self, monkeypatch) -> None:
        monkeypatch.delenv("PORTAINER_API_KEY", raising=False)
        fontes = fontes_do_config(load())
        assert "portainer" not in fontes
        assert "n8n" in fontes

    def test_sem_nenhuma_credencial_o_loader_baralha_antes(self) -> None:
        """Sem a chave do n8n o `load()` nem chega a montar a config.

        Melhor documentar a verdade do que forjar um cenario: o agente NAO
        sobe sem `N8N_API_KEY`. `global_fallback` e o ultimo degrau, nao o
        primeiro -- sem chave nao existe agente para notificar ninguem.
        """
        from agent_nio_monitor.config.loader import ConfigError

        monkey = pytest.MonkeyPatch()
        monkey.delenv("N8N_API_KEY", raising=False)
        monkey.delenv("PORTAINER_API_KEY", raising=False)
        try:
            with pytest.raises(ConfigError, match="N8N_API_KEY"):
                load()
        finally:
            monkey.undo()

    def test_fontes_vazio_quando_so_a_variavel_some_depois(self, monkeypatch) -> None:
        cfg = load()
        monkeypatch.delenv("N8N_API_KEY", raising=False)
        monkeypatch.delenv("PORTAINER_API_KEY", raising=False)
        assert fontes_do_config(cfg) == {}


class TestSondaNoBoot:
    async def test_boot_sonda_e_grava_no_banco(self, respx_mock) -> None:
        """O primeiro uso real de `save_capabilities` em producao."""
        cfg = load()
        _responde_tudo(cfg.active.n8n.url)
        _responde_tudo(cfg.active.portainer.url)
        repo = InMemoryRepository()
        agent = Agent(cfg, repo)

        resumo = await agent.run_capabilities()

        assert resumo["fontes"] == 2, "n8n e Portainer, ambos com credencial"
        assert resumo["capacidades"] == len(ENDPOINTS)
        gravadas = repo.capabilities
        assert gravadas, "a sonda rodou e nada foi gravado"
        assert {c["feature"] for c in gravadas} >= {"core", "tenant", "health"}
        assert {c["source"] for c in gravadas} == {"n8n", "portainer"}

    async def test_falha_de_transporte_e_registrada_como_indisponivel(self, respx_mock) -> None:
        """`detect` nao levanta por erro de rede: `_probe_one` devolve
        `(0, "erro_transporte")` e a capacidade fica registrada como ausente.

        E o resultado certo -- "n8n nao respondeu" e informacao de primeira
        classe, e some-la seria o mesmo buraco de `global_fallback`."""
        cfg = load()
        respx.get(url__startswith=_rota(cfg.active.n8n.url)).mock(
            side_effect=httpx.ConnectError("recusou")
        )
        respx.get(url__startswith=_rota(cfg.active.portainer.url)).mock(
            side_effect=httpx.ConnectError("recusou")
        )
        repo = InMemoryRepository()
        resumo = await Agent(cfg, repo).run_capabilities()

        assert resumo["fontes"] == 2, "as fontes respondem a sonda, mesmo com erro"
        assert repo.capabilities, "indisponivel tambem e dado: precisa ser gravado"
        assert all(not c["available"] for c in repo.capabilities)

    async def test_sonda_que_ESTOURA_nao_derruba_o_boot(self, monkeypatch) -> None:
        """O ponto mais importante desta fase. O `except` largo em
        `run_capabilities` e deliberado: se a propria sonda explodir -- um bug
        novo, um tipo inesperado -- o agente tem que continuar coletando e dizer
        que nao sondou.

        O contrario converte "um alerta perdido" em "o monitor inteiro parado",
        que e o pior resultado possivel para um monitor.
        """
        import agent_nio_monitor.boot_probe as agent_mod

        async def _explode(fontes, **kw):  # noqa: ANN001, ANN003
            raise RuntimeError("defeito novo na sonda")

        monkeypatch.setattr(agent_mod, "detect", _explode)
        agent = Agent(load(), InMemoryRepository())

        resumo = await agent.run_capabilities()  # nao pode levantar

        assert resumo["capacidades"] == 0
        assert resumo["fontes"] == 2, "as fontes eram conhecidas; so a sonda falhou"


class TestEscuridaoDeUmaFonte:
    async def test_escuridao_de_uma_fonte_nao_apaga_a_outra(self, respx_mock) -> None:
        """So o n8n responde. As capacidades dele tem que ficar marcadas como
        disponiveis E as do Portainer como ausentes -- senao o registro vira
        tudo-indisponivel e perde a informacao de qual fonte caiu."""
        cfg = load()
        respx.get(url__startswith=_rota(cfg.active.n8n.url)).mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get(url__startswith=_rota(cfg.active.portainer.url)).mock(
            side_effect=httpx.ConnectError("recusou")
        )
        repo = InMemoryRepository()
        resumo = await Agent(cfg, repo).run_capabilities()

        assert resumo["fontes"] == 2
        por_fonte = {c["source"]: c["available"] for c in repo.capabilities}
        assert por_fonte["n8n"] is True, "o n8n respondeu; descartar seria perder dado"
        assert por_fonte["portainer"] is False, "o Portainer nao respondeu"


class TestParalelismo:
    @respx.mock
    async def test_paralelo_produz_o_mesmo_conjunto_do_sequencial(self) -> None:
        """A sonda virou `gather`; mudar a forma nao pode mudar o resultado.
        Sem este teste, uma rota esquecida no `gather` viraria capacidade
        'nao disponivel' em producao e ninguem notaria ate um alerta faltar."""
        async def _coleta(paralelo: bool) -> set[tuple[str, str, bool]]:
            async def _rota(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json={"data": []})

            async with respx.mock(assert_all_called=False) as router:
                router.route(method="GET").mock(side_effect=_rota)
                reports = await detect(
                    {"n8n": {"url": N8N, "api_key": "k"}}, probe_parallel=paralelo
                )
            return {(c.source, c.path, c.available) for c in reports["n8n"].items}

        assert await _coleta(True) == await _coleta(False)
        assert len(await _coleta(True)) == len([e for e in ENDPOINTS if e[0] == "n8n"])

    @respx.mock
    async def test_falha_de_transporte_vira_indisponivel_e_nao_levanta(self) -> None:
        async def _rota(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("recusou")

        async with respx.mock(assert_all_called=False) as router:
            router.route(method="GET").mock(side_effect=_rota)
            reports = await detect({"n8n": {"url": N8N, "api_key": "k"}})
        assert reports["n8n"].items
        assert all(not c.available for c in reports["n8n"].items)
        assert all("erro_transporte" in c.detail for c in reports["n8n"].items)


class TestOrdemDoBoot:
    def test_agenda_dispara_a_sonda(self) -> None:
        """A sonda precisa estarwired na agenda, e nao so existir como metodo.

        Este e o mesmo buraco de `global_fallback`: metodo novo sem quem o
        chama passa em toda suite e nao roda nunca."""
        import inspect

        from agent_nio_monitor import main as main_mod

        fonte = inspect.getsource(main_mod._agenda)  # noqa: SLF001
        assert "run_capabilities" in fonte, (
            "a sonda de capacidade nao esta na agenda -- D2 continua so no doctor"
        )

    def test_sonda_roda_depois_do_health(self) -> None:
        """A ordem importa e e verificavel: `HealthServer.start()` vem antes de
        `create_task(_agenda(...))` dentro de `main()`. Sondar antes deixaria o
        `/healthz` sem resposta durante a sonda -- e o cron externo veria queda,
        nao espera. `_run` e aninhada em `main()`, entao a fonte vem de `main`."""
        import inspect

        from agent_nio_monitor import main as main_mod

        fonte = inspect.getsource(main_mod.main)
        assert fonte.index("servidor.start()") < fonte.index("_agenda("), (
            "a sonda precisa ficar depois do health subir"
        )