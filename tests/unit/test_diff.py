"""Diff estrutural de workflow (T5).

O teste que importa aqui e o de seguranca: o corpo do workflow tem credencial
de producao real. O diff precisa produzir informacao util SEM carregar o valor
do parametro.
"""

from __future__ import annotations


from agent_nio_monitor.sources.n8n.diff import (
    autoria_de,
    diff_workflows,
    projection,
    resumo,
    risco_para_score,
)


def _wf(nodes: list[dict], connections: dict | None = None, **extra) -> dict:
    base = {
        "id": "wf1",
        "name": "Teste",
        "active": True,
        "nodes": nodes,
        "connections": connections or {},
        "settings": {"executionOrder": "v1"},
    }
    base.update(extra)
    return base


def _node(nid: str, name: str, tipo: str = "n8n-nodes-base.set", **extra) -> dict:
    n = {"id": nid, "name": name, "type": tipo, "typeVersion": 3, "parameters": {}}
    n.update(extra)
    return n


class TestProjecao:
    def test_projecao_nao_carrega_valor_de_parametro(self) -> None:
        """O parametro pode ser senha (e ja foi). A projecao so guarda a CHAVE."""
        wf = _wf([_node("a", "A", parameters={"password": "jfc2025", "host": "x"})])
        proj = projection(wf)
        serial = repr(proj)
        assert "jfc2025" not in serial
        assert proj["nodes"][0]["param_keys"] == ["host", "password"]

    def test_position_nao_conta_como_mudanca(self) -> None:
        """Mover um node no canvas nao e evento."""
        antes = _wf([_node("a", "A", position=[0, 0])])
        depois = _wf([_node("a", "A", position=[500, 900])])
        assert diff_workflows(antes, depois) == []

    def test_param_keys_ignoradas(self) -> None:
        antes = _wf([_node("a", "A", position=[0, 0], color="#fff")])
        depois = _wf([_node("a", "A", position=[9, 9], color="#000")])
        assert diff_workflows(antes, depois) == []


class TestDeteccao:
    def test_node_removido(self) -> None:
        antes = _wf([_node("a", "A"), _node("b", "B")])
        depois = _wf([_node("a", "A")])
        m = diff_workflows(antes, depois)
        assert any(x.kind == "node_removed" and x.path == "b" for x in m)

    def test_node_adicionado(self) -> None:
        antes = _wf([_node("a", "A")])
        depois = _wf([_node("a", "A"), _node("b", "B")])
        m = diff_workflows(antes, depois)
        assert any(x.kind == "node_added" and x.path == "b" for x in m)

    def test_conexao_removida_pesa_mais_que_adicionada(self) -> None:
        antes = _wf([_node("a", "A"), _node("b", "B")],
                    {"A": {"main": [[{"node": "B", "type": "main", "index": 0}]]}})
        depois = _wf([_node("a", "A"), _node("b", "B")], {"A": {"main": [[]]}})
        m = diff_workflows(antes, depois)
        assert any(x.kind == "conn_removed" for x in m)
        pontos, _ = risco_para_score(m)
        assert pontos >= 4, "conexao removida e o sinal mais grave"

    def test_desativacao_do_workflow(self) -> None:
        antes = _wf([_node("a", "A")], active=True)
        depois = _wf([_node("a", "A")], active=False)
        m = diff_workflows(antes, depois)
        assert any(x.kind == "settings_changed" and x.path == "active" for x in m)

    def test_mudanca_de_parametro_e_marcada_como_risco(self) -> None:
        """Adicionar um parametro novo costuma significar 'coloquei uma senha
        hardcoded'. Isso tem que aparecer como risco, nao como 'node alterado'."""
        antes = _wf([_node("a", "A", parameters={"host": "x"})])
        depois = _wf([_node("a", "A", parameters={"host": "x", "password": "segredo"})])
        m = diff_workflows(antes, depois)
        assert any(x.kind == "node_changed_risky" for x in m)

    def test_node_desabilitado_e_risco(self) -> None:
        antes = _wf([_node("a", "A")])
        depois = _wf([_node("a", "A", disabled=True)])
        assert any(x.kind == "node_changed_risky" for x in diff_workflows(antes, depois))

    def test_sem_mudanca_nao_gera_evento(self) -> None:
        wf = _wf([_node("a", "A")])
        assert diff_workflows(wf, wf) == []

    def test_settings_alterado(self) -> None:
        antes = _wf([_node("a", "A")], settings={"executionOrder": "v1"})
        depois = _wf([_node("a", "A")], settings={"executionOrder": "v2"})
        assert any(x.kind == "settings_changed" and x.path == "settings"
                   for x in diff_workflows(antes, depois))


class TestResumo:
    def test_resumo_lista_removidos_por_nome(self) -> None:
        antes = _wf([_node("a", "A"), _node("b", "B")])
        depois = _wf([_node("a", "A")])
        texto = resumo(diff_workflows(antes, depois))
        assert "node REMOVIDO" in texto
        assert "B" in texto

    def test_resumo_vazio_nao_inventa(self) -> None:
        assert resumo([]) == "Nenhuma diferenca estrutural detectada."


class TestAutoria:
    def test_le_autoria_da_versao_ativa(self) -> None:
        """Confirmado no dump real: `activeVersion.authors` vem preenchido."""
        wf = {"activeVersion": {"authors": "nio@grupocampanha.com.br"}}
        assert autoria_de(wf) == "nio@grupocampanha.com.br"

    def test_sem_autoria_devolve_none_e_nao_inventa(self) -> None:
        assert autoria_de({}) is None
        assert autoria_de({"activeVersion": {}}) is None
        assert autoria_de({"activeVersion": {"authors": "  "}}) is None