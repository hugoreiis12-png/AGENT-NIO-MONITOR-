"""Coletor de credenciais: a capacidade que faltava desde sempre.

`credencial_alterada` esta em `policy.yaml` e o template `credential_updated`
esta em `notify/templates.py` -- ha anos. Nenhum modulo emitia o evento. Era um
dos nomes mortos que a auditoria da Fase 1 encontrou.

O formato da resposta (`GET /api/v1/credentials`) foi verificado na instancia
real antes de escrever este codigo: HTTP 200, campos `id`, `name`, `type`,
`createdAt`, `updatedAt`, `shared`. Nenhum campo aqui foi suposto.
"""

from __future__ import annotations

import httpx
import respx

from agent_nio_monitor.sources.n8n.client import N8NClient
from agent_nio_monitor.sources.n8n.credentials import CredentialCollector
from agent_nio_monitor.store.memory_repository import InMemoryRepository

N8N = "http://n8n.test"


def _cred(cid: str = "c1", nome: str = "DB-RELATORIOS", tipo: str = "postgres",
          atualizada: str = "2026-09-15T18:15:45.398Z", **extra) -> dict:
    return {
        "id": cid,
        "name": nome,
        "type": tipo,
        "createdAt": "2026-09-15T18:13:41.577Z",
        "updatedAt": atualizada,
        "shared": [
            {"id": "u1", "name": "nio@grupocampanha.com.br", "role": "credential:owner"},
        ],
        **extra,
    }


def _rota(credenciais: list[dict]) -> None:
    respx.get(f"{N8N}/api/v1/credentials").mock(
        return_value=httpx.Response(200, json={"data": credenciais, "nextCursor": None})
    )


async def _coletor(repo: InMemoryRepository) -> tuple[CredentialCollector, N8NClient]:
    client = N8NClient(N8N, "k")
    return CredentialCollector(client, repo), client


class TestLinhaDeBase:
    @respx.mock
    async def test_primeira_coleta_nao_avisa(self) -> None:
        """39 credenciais no primeiro ciclo = 39 alertas. Seria o pior arranque.

        Mesma regra do `WorkflowMonitor`: sem historico nao ha o que comparar.
        """
        _rota([_cred("c1"), _cred("c2", nome="SMTP")])
        repo = InMemoryRepository()
        col, client = await _coletor(repo)
        try:
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert r.ok is True
        assert r.collected == 2
        assert r.extra["eventos"] == [], "a primeira coleta forma base e nao alerta"
        assert len(repo.list_resources("n8n", "credential")) == 2

    @respx.mock
    async def test_a_segunda_coleta_inalterada_tambem_nao_avisa(self) -> None:
        _rota([_cred("c1"), _cred("c2", nome="SMTP")])
        repo = InMemoryRepository()
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert r.extra["eventos"] == [], "nada mudou entre as coletas"


class TestDeteccao:
    @respx.mock
    async def test_credencial_alterada_avisa(self) -> None:
        repo = InMemoryRepository()
        _rota([_cred("c1")])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()

            _rota([_cred("c1", atualizada="2026-10-14T12:00:00.000Z")])
            r = await col.check_credentials()
        finally:
            await client.aclose()

        eventos = r.extra["eventos"]
        assert len(eventos) == 1, f"esperado 1 evento, veio {len(eventos)}"
        e = eventos[0]
        assert e.event_type == "credential_updated"
        assert e.severity == "high"
        assert e.resource_id == "c1"
        assert e.detail["tipo_mudanca"] == "alterada"
        assert e.detail["atualizada_anteriormente"] == "2026-09-15T18:15:45.398Z"
        assert e.detail["atualizada_em"] == "2026-10-14T12:00:00.000Z"
        assert e.detail["credencial_nome"] == "DB-RELATORIOS"
        assert e.detail["credencial_tipo"] == "postgres"

    @respx.mock
    async def test_credencial_nova_avisa(self) -> None:
        repo = InMemoryRepository()
        _rota([_cred("c1")])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()

            _rota([_cred("c1"), _cred("c2", nome="SMTP-PROD", tipo="smtp")])
            r = await col.check_credentials()
        finally:
            await client.aclose()

        eventos = r.extra["eventos"]
        assert len(eventos) == 1
        assert eventos[0].detail["tipo_mudanca"] == "nova"
        assert eventos[0].detail["credencial_nome"] == "SMTP-PROD"

    @respx.mock
    async def test_apenas_a_que_mudou_avisa(self) -> None:
        """Com 39 credenciais, o alerta tem de ser do que mudou -- nao da lista."""
        repo = InMemoryRepository()
        antes = [_cred(f"c{i}", nome=f"CRED-{i}") for i in range(10)]
        _rota(antes)
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()

            depois = [dict(c) for c in antes]
            depois[7]["updatedAt"] = "2026-10-14T12:00:00.000Z"
            _rota(depois)
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert len(r.extra["eventos"]) == 1
        assert r.extra["eventos"][0].resource_id == "c7"

    @respx.mock
    async def test_rotacao_de_varias_avisa_uma_por_uma(self) -> None:
        repo = InMemoryRepository()
        _rota([_cred(f"c{i}", nome=f"CRED-{i}") for i in range(4)])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()
            _rota([
                _cred("c0", atualizada="2026-10-14T00:00:00.000Z"),
                _cred("c1", atualizada="2026-10-14T00:00:01.000Z"),
                _cred("c2", atualizada="2026-10-14T00:00:02.000Z"),
            ])
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert len(r.extra["eventos"]) == 3, "tres rotacoes, tres alertas"

    @respx.mock
    async def test_rotacao_repetida_nao_duplica_alerta(self) -> None:
        """Apos avisar, o novo estado ja e o atual: a proxima coleta silencia."""
        repo = InMemoryRepository()
        _rota([_cred("c1")])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()
            _rota([_cred("c1", atualizada="2026-10-14T12:00:00.000Z")])
            assert len((await col.check_credentials()).extra["eventos"]) == 1
            assert (await col.check_credentials()).extra["eventos"] == []
        finally:
            await client.aclose()


# `shared` traz `nio@grupocampanha.com.br` com papel de acesso.
# Tudo nesta classe existe para o dado pessoal NAO virar alerta nem registro.


class TestDadoSensivel:
    @respx.mock
    async def test_o_email_de_quem_tem_acesso_nao_e_gravado(self) -> None:
        repo = InMemoryRepository()
        _rota([_cred("c1")])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()
            _rota([_cred("c1", atualizada="2026-10-14T12:00:00.000Z")])
            r = await col.check_credentials()
        finally:
            await client.aclose()

        # no banco
        for recurso in repo.list_resources("n8n", "credential"):
            assert "nio@" not in str(recurso), f"email vazou para o banco: {recurso}"

        # e nem no alerta
        assert "nio@" not in str(r.extra["eventos"]), "email de pessoa no alerta"

    @respx.mock
    async def test_o_alerta_conta_quantas_pessoas_sem_dizer_quem(self) -> None:
        repo = InMemoryRepository()
        _rota([_cred("c1")])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()
            _rota([_cred("c1", atualizada="2026-10-14T12:00:00.000Z")])
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert r.extra["eventos"][0].detail["compartilhada_com"] == 1

    @respx.mock
    async def test_o_alerta_nao_promete_saber_quem_alterou(self) -> None:
        """A API nao tem campo de autoria. Um texto que promete "Quem alterou"
        vazio seria pior do que nao ter a linha."""
        repo = InMemoryRepository()
        _rota([_cred("c1")])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()
            _rota([_cred("c1", atualizada="2026-10-14T12:00:00.000Z")])
            r = await col.check_credentials()
        finally:
            await client.aclose()

        detalhe = r.extra["eventos"][0].detail["detalhe"]
        assert "QUEM alterou" in detalhe, "o texto precisa avisar que nao sabe quem"
        assert "autor" not in r.extra["eventos"][0].detail


class TestRobustez:
    @respx.mock
    async def test_erro_na_api_vira_erro_do_coletor(self) -> None:
        respx.get(f"{N8N}/api/v1/credentials").mock(return_value=httpx.Response(500))
        col, client = await _coletor(InMemoryRepository())
        try:
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert r.ok is False, "erro de API tem que marcar a fonte como nao-ok"

    @respx.mock
    async def test_403_nao_e_tratado_como_linha_de_base(self) -> None:
        """403 (licenca sem o feature) nao pode virar "zero credenciais".

        Seria registrar estado vazio e, na coleta seguinte, tratar 39 credenciais
        como NOVAS: 39 alertas de uma vez, por um erro de permissao.
        """
        respx.get(f"{N8N}/api/v1/credentials").mock(
            return_value=httpx.Response(403, json={"message": "license does not allow"})
        )
        repo = InMemoryRepository()
        col, client = await _coletor(repo)
        try:
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert r.ok is False
        assert repo.list_resources("n8n", "credential") == [], (
            "erro de permissao nao pode gravar linha de base"
        )

    @respx.mock
    async def test_credencial_sem_id_e_ignorada(self) -> None:
        _rota([{"name": "SEM-ID", "type": "postgres", "updatedAt": "x"}])
        repo = InMemoryRepository()
        col, client = await _coletor(repo)
        try:
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert r.collected == 1
        assert repo.list_resources("n8n", "credential") == []

    @respx.mock
    async def test_mesma_credencial_repetida_na_paginacao_nao_duplica(self) -> None:
        repo = InMemoryRepository()
        _rota([_cred("c1"), _cred("c1")])
        col, client = await _coletor(repo)
        try:
            await col.check_credentials()
            _rota([_cred("c1"), _cred("c1", atualizada="2026-10-14T12:00:00.000Z")])
            r = await col.check_credentials()
        finally:
            await client.aclose()

        assert len(r.extra["eventos"]) == 1, "o mesmo id nao pode gerar dois alertas"