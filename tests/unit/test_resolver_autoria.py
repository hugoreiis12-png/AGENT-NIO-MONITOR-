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


class TestFallbackGlobal:
    """Ultimo degrau da cadeia: `people.yaml: global`.

    Sem este degrau, um projeto fora de `people.yaml:projects` termina em zero
    destinatario, o `mailer` devolve `failed` e o `dispatcher` marca o item
    como `failed` em definitivo. O alerta e gravado e nunca sai.
    """

    def test_people_yaml_expoe_a_chave_global(self) -> None:
        from agent_nio_monitor.notify.people import global_fallback

        assert isinstance(global_fallback(), list)

    def test_o_fallback_global_nao_esta_vazio(self) -> None:
        """A fiação do `Agent` existir nao entrega nada se a lista for vazia.

        Foi a condicao que deixou a Fase 1 pronta e inerte ao mesmo tempo. Com
        a lista vazia, `Resolution(emails=[])` derrubava todo alerta de projeto
        desconhecido -- e o `reconciliation_gap`, que nao tem `tenant_id`.
        """
        from agent_nio_monitor.notify.people import global_fallback

        fallback = global_fallback()
        assert fallback, (
            "people.yaml: global esta vazio -- todo alerta sem projeto conhecido "
            "e nenhum evento sem tenant_id (como reconciliation_gap) sera perdido"
        )

    def test_o_fallback_global_sao_enderecos_validos(self) -> None:
        from agent_nio_monitor.notify.people import global_fallback

        invalidos = [e for e in global_fallback() if "@" not in e or e.startswith("@")]
        assert not invalidos, f"endereco invalido no fallback global: {invalidos}"

    async def test_o_fallback_nao_vaza_quando_o_projeto_resolve(self) -> None:
        """O fallback e o MESMO time que e dono dos projetos -- de proposito.

        Se ele vazasse para o caminho do projeto, todo alerta iria para todo
        mundo e o `source` perderia o sentido. O que precisa valer e a
        separacao: projeto conhecido resolve pelo projeto, projeto
        desconhecido cai no global, e `source` diz qual dos dois aconteceu.
        """
        from agent_nio_monitor.notify.people import global_fallback, people_by_project

        do_projeto = {e for lista in people_by_project().values() for e in lista}
        assert set(global_fallback()) & do_projeto, (
            "o fallback global e o mesmo time responsavel pelos projetos -- "
            "esperado. Este teste existe para garantir que ele NAO vaze para o "
            "caminho do projeto, nao para exigir que sejam diferentes."
        )

        # Projeto conhecido: o fallback nao pode entrar
        r = RecipientResolver(
            None, {"P1": ["dono@x.com"]}, global_fallback=global_fallback()
        )
        res = await r.resolve_for_event("P1", {"autor": None})
        assert res.emails == ["dono@x.com"], (
            f"o fallback global vazou no projeto conhecido: {res.emails}"
        )
        assert res.source == "people_yaml"

        # Projeto desconhecido: ai sim
        res2 = await r.resolve_for_event("PROJETO_DESCONHECIDO", {"autor": None})
        assert res2.source == "global_fallback"
        assert set(res2.emails) == set(global_fallback())

    async def test_projeto_desconhecido_cai_no_fallback_global(self) -> None:
        r = _resolver(global_fallback=["plantao@x.com"])
        res = await r.resolve_for_event("PROJETO_QUE_NAO_EXISTE", {})
        assert res.emails == ["plantao@x.com"]
        assert res.source == "global_fallback"

    async def test_fallback_global_tem_precedencia_sobre_o_vazio(self) -> None:
        """Com o fallback global preenchido, NENHUM caminho pode terminar vazio --
        que era exatamente o buraco: a branch existia mas nunca era alimentada."""
        r = _resolver(global_fallback=["plantao@x.com"])
        for projeto in (None, "", "P1", "PROJETO_DESCONHECIDO"):
            res = await r.resolve_for_event(projeto, {"autor": None})
            assert res.emails, f"projeto={projeto!r} terminou com zero destinatario"

    async def test_sem_fallback_configurado_permite_o_alerta_perder(self) -> None:
        """Fixa o comportamento de hoje, para a mudanca ser visivel se regredir.

        `source="nenhum"` e a condicao que o `Agent` agora conta em
        `health.alertas_sem_destinatario`. Enquanto `people.yaml: global` estiver
        vazio, este e o cenario real de um projeto fora do mapa.
        """
        r = _resolver(global_fallback=[])
        res = await r.resolve_for_event("PROJETO_QUE_NAO_EXISTE", {})
        assert res.emails == []
        assert res.source == "nenhum"
