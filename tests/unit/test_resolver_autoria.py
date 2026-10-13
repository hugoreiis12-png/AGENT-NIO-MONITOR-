"""Quem recebe o alerta: responsavel do projeto/stack + quem mexeu no recurso."""

from __future__ import annotations

from agent_nio_monitor.notify.resolver import RecipientResolver


def _resolver(**kw) -> RecipientResolver:
    return RecipientResolver(None, {"P1": ["dono@x.com"]}, **kw)


class TestPessoas:
    def test_email_embutido_no_texto_do_autor_n8n(self) -> None:
        r = _resolver().resolve_pessoas(["Ana Souza <ana@x.com>"])
        assert r.emails == ["ana@x.com"]

    def test_usuario_do_portainer_vira_email_pelo_mapa(self) -> None:
        r = _resolver(usuarios={"Carlos": "carlos@x.com"}).resolve_pessoas(["carlos"])
        assert r.emails == ["carlos@x.com"]

    def test_import_e_usuario_sem_mapa_nao_geram_destinatario(self) -> None:
        assert _resolver().resolve_pessoas(["import", "fulano", None]).emails == []


class TestUniaoComOResponsavel:
    async def test_autor_se_soma_ao_responsavel_do_projeto(self) -> None:
        r = await _resolver().resolve_for_event("P1", {"autor": "Ana <ana@x.com>"})
        assert r.emails == ["dono@x.com", "ana@x.com"]

    async def test_sem_autor_vale_so_o_responsavel(self) -> None:
        r = await _resolver().resolve_for_event("P1", {"autor": None})
        assert r.emails == ["dono@x.com"]

    async def test_autor_repetido_nao_duplica(self) -> None:
        r = await _resolver().resolve_for_event("P1", {"autor": "dono@x.com"})
        assert r.emails == ["dono@x.com"]

    async def test_stack_sem_projeto_notifica_dono_e_ultimo_autor(self) -> None:
        r = await _resolver(usuarios={"admin": "a@x.com", "carlos": "c@x.com"}).resolve_for_event(
            None, {"autor": "carlos", "responsavel_stack": "admin"}
        )
        assert sorted(r.emails) == ["a@x.com", "c@x.com"]


class TestWorkflowExclusivo:
    async def test_lista_exclusiva_ignora_projeto_e_autor(self) -> None:
        r = RecipientResolver(None, {"P1": ["dono@x.com"]}, exclusivos={"wf1": ["so@x.com"]})
        res = await r.resolve_for_event("P1", {"workflow_id": "wf1", "autor": "Ana <ana@x.com>"})
        assert res.emails == ["so@x.com"]

    async def test_outro_workflow_segue_a_regra_normal(self) -> None:
        r = RecipientResolver(None, {"P1": ["dono@x.com"]}, exclusivos={"wf1": ["so@x.com"]})
        res = await r.resolve_for_event("P1", {"workflow_id": "wf2"})
        assert res.emails == ["dono@x.com"]


class TestPoliticaDoPeopleYaml:
    """Regras combinadas com o dono: Rafael sai de 3 fluxos e entra onde e dono/autor."""

    def _resolver(self) -> RecipientResolver:
        from agent_nio_monitor.notify.people import (
            exclusive_by_workflow,
            people_by_project,
            portainer_users,
        )

        return RecipientResolver(None, people_by_project(), usuarios=portainer_users(),
                                 exclusivos=exclusive_by_workflow())

    async def test_tres_fluxos_vao_so_para_o_hugo(self) -> None:
        for wid in ("nATlDqPBknIcPaW3", "STfrlbTnTeuBDntf", "jCWyCWHK1XMhckeF"):
            r = await self._resolver().resolve_for_event(
                "wGiMpmFFLBhiwtFm", {"workflow_id": wid, "autor": "Rafael Barboza"})
            assert r.emails == ["hugo.reis@grupocampanha.com.br"], wid

    async def test_workflow_do_rafael_notifica_dono_do_projeto_e_o_autor(self) -> None:
        r = await self._resolver().resolve_for_event(
            "wGiMpmFFLBhiwtFm", {"workflow_id": "outro", "autor": "Rafael Barboza"})
        assert "rafael.barboza@grupocampanha.com.br" in r.emails

    async def test_stack_do_portainer_notifica_dono_e_ultimo_autor(self) -> None:
        r = await self._resolver().resolve_for_event(
            None, {"autor": "felipe", "responsavel_stack": "carlos"})
        assert sorted(r.emails) == ["carlos.henrique@grupocampanha.com.br",
                                    "felipe.chaves@grupocampanha.com.br"]
