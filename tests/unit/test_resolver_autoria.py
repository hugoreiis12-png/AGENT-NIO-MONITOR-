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
