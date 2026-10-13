"""Quanto mudou o compose -- e sem vazar segredo de variavel de ambiente."""

from __future__ import annotations

from agent_nio_monitor.sources.portainer.compose_diff import (
    normalizar,
    proporcao_alterada,
    resumo_mudancas,
)

BASE = """
services:
  api:
    image: app/api:1.0
    environment:
      DB_PASSWORD: hunter2
      MODE: prod
    ports: ["80:80"]
  worker:
    image: app/worker:1.0
  redis:
    image: redis:7
"""

REORDENADO = """
services:
  redis:
    image: redis:7
  worker:
    image: app/worker:1.0
  api:
    ports: ["80:80"]
    environment:
      MODE: prod
      DB_PASSWORD: hunter2
    image: app/api:1.0
"""


class TestNormalizar:
    def test_valor_de_variavel_vira_hash_mas_a_chave_fica(self) -> None:
        saida = normalizar(BASE)
        assert "hunter2" not in saida
        assert "DB_PASSWORD" in saida

    def test_variavel_em_formato_lista_tambem_e_ocultada(self) -> None:
        saida = normalizar("services:\n  a:\n    environment:\n      - TOKEN=segredo123\n")
        assert "segredo123" not in saida
        assert "TOKEN=" in saida

    def test_mudar_so_o_valor_do_segredo_ainda_aparece_no_diff(self) -> None:
        depois = BASE.replace("hunter2", "outrasenha")
        assert proporcao_alterada(normalizar(BASE), normalizar(depois)) > 0

    def test_reordenar_chaves_nao_conta_como_mudanca(self) -> None:
        assert proporcao_alterada(normalizar(BASE), normalizar(REORDENADO)) == 0.0

    def test_yaml_invalido_nao_levanta_e_redige_segredo(self) -> None:
        assert "hunter2" not in normalizar("::: nao e yaml\npassword=hunter2")


class TestProporcao:
    def test_identico_e_zero(self) -> None:
        assert proporcao_alterada("a\nb\n", "a\nb\n") == 0.0

    def test_ajuste_pequeno_fica_abaixo_de_20_por_cento(self) -> None:
        antes = normalizar(BASE)
        depois = normalizar(BASE.replace("app/api:1.0", "app/api:1.1"))
        assert 0 < proporcao_alterada(antes, depois) < 0.20

    def test_remover_servicos_inteiros_passa_de_20_por_cento(self) -> None:
        depois = "services:\n  api:\n    image: app/api:1.0\n"
        assert proporcao_alterada(normalizar(BASE), normalizar(depois)) > 0.20


class TestResumo:
    def test_lista_servicos_adicionados_removidos_e_alterados(self) -> None:
        depois = BASE.replace("redis:7", "redis:8").replace(
            "  worker:\n    image: app/worker:1.0\n", "  cache:\n    image: memcached\n"
        )
        texto = resumo_mudancas(normalizar(BASE), normalizar(depois))
        assert "adicionados: cache" in texto
        assert "removidos: worker" in texto
        assert "redis alterado (image)" in texto