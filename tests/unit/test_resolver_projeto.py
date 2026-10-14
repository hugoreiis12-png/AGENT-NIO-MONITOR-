"""Responsavel pelo projeto via API do n8n, e a degradacao quando a API nao ajuda."""

from __future__ import annotations

from agent_nio_monitor.notify.resolver import RecipientResolver

USUARIOS = [{"id": "u1", "email": "Ana@X.com"}, {"id": "u2", "email": "bia@x.com"}, {"id": "u3", "email": None}]


class _N8NFalso:
    def __init__(self, *, membros: list[dict] | None = None, usuarios_falham: bool = False,
                 membros_falham: bool = False) -> None:
        self.membros = membros or []
        self.usuarios_falham, self.membros_falham = usuarios_falham, membros_falham
        self.chamadas_de_usuarios = 0
        self.chamadas_de_membros = 0

    async def list_users(self) -> list[dict]:
        self.chamadas_de_usuarios += 1
        if self.usuarios_falham:
            raise RuntimeError("403: a key nao e owner")
        return USUARIOS

    async def list_project_users(self, project_id: str) -> list[dict]:
        self.chamadas_de_membros += 1
        if self.membros_falham:
            raise RuntimeError("timeout")
        return self.membros


def _resolver(cliente: _N8NFalso, **kw) -> RecipientResolver:  # noqa: ANN003
    return RecipientResolver(cliente, {"P1": ["yaml@x.com"]}, **kw)


class TestProjetoPelaApi:
    async def test_membros_do_projeto_viram_destinatarios(self) -> None:
        cliente = _N8NFalso(membros=[{"userId": "u1"}, {"id": "u2"}, {"userId": "u3"}, {"userId": "fantasma"}])
        r = await _resolver(cliente).resolve_for_project("P1")
        assert r.source == "n8n_project"
        assert r.emails == ["ana@x.com", "bia@x.com"], "sem e-mail ou id desconhecido nao vira destinatario"

    async def test_resultado_e_usuarios_ficam_em_cache(self) -> None:
        cliente = _N8NFalso(membros=[{"userId": "u1"}])
        resolver = _resolver(cliente)
        await resolver.resolve_for_project("P1")
        await resolver.resolve_for_project("P1")
        await resolver.resolve_for_project("P2")
        assert cliente.chamadas_de_membros == 2, "o mesmo projeto nao consulta a API de novo"
        assert cliente.chamadas_de_usuarios == 1, "a lista de usuarios e lida uma vez"


class TestDegradacao:
    async def test_key_sem_permissao_cai_no_people_yaml(self) -> None:
        r = await _resolver(_N8NFalso(usuarios_falham=True)).resolve_for_project("P1")
        assert (r.source, r.emails) == ("people_yaml", ["yaml@x.com"])

    async def test_falha_ao_listar_membros_cai_no_people_yaml(self) -> None:
        r = await _resolver(_N8NFalso(membros_falham=True)).resolve_for_project("P1")
        assert r.source == "people_yaml"

    async def test_projeto_fora_do_yaml_cai_no_global(self) -> None:
        r = await _resolver(_N8NFalso(), global_fallback=["plantao@x.com"]).resolve_for_project("P9")
        assert (r.source, r.emails) == ("global_fallback", ["plantao@x.com"])

    async def test_sem_nenhum_degrau_o_alerta_e_declarado_perdido(self) -> None:
        r = await _resolver(_N8NFalso()).resolve_for_project("P9")
        assert r.source == "nenhum" and not r.emails
        assert "ALERTA PERDIDO" in r.detail


class TestLimiteDeDestinatarios:
    async def test_lista_exclusiva_respeita_o_teto(self) -> None:
        resolver = _resolver(_N8NFalso(), exclusivos={"wf": [f"p{i}@x.com" for i in range(5)]}, max_recipients=3)
        r = await resolver.resolve_for_event(None, {"workflow_id": "wf"})
        assert r.emails == ["p0@x.com", "p1@x.com", "p2@x.com"]


class TestFonteExclusiva:
    async def test_fonte_exclusiva_ignora_dono_projeto_e_global(self) -> None:
        resolver = _resolver(_N8NFalso(), global_fallback=["plantao@x.com"],
                             por_fonte={"powerautomate": ["rafael@x.com", "carlos@x.com"]})
        r = await resolver.resolve_for_event("P1", {"responsavel_fluxo": "Dono <dono@x.com>"}, fonte="powerautomate")
        assert (r.source, r.emails) == ("fonte_exclusiva", ["rafael@x.com", "carlos@x.com"])

    async def test_outra_fonte_segue_o_caminho_normal(self) -> None:
        resolver = _resolver(_N8NFalso(), por_fonte={"powerautomate": ["rafael@x.com"]})
        r = await resolver.resolve_for_event("P1", {}, fonte="n8n")
        assert r.source == "people_yaml"
