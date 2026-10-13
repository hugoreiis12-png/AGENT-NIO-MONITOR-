"""Prova que a redacao e total contra os segredos REAIS encontrados na instancia.

Os tres fixtures abaixo foram copiados de `nodes.parameters` de
`GET /workflows`. Nao sao inventados -- se a redacao regredir, este teste falha.
"""

from __future__ import annotations

import pytest

from agent_nio_monitor.redaction import REDACTED, contains_secret, redact, redact_text

# --- fixtures literais da instancia -------------------------------------------

SAP_LOGIN_NODE = {
    "parameters": {
        "method": "POST",
        "url": "https://piwfbhdb.b1cloud.com.br:50089/b1s/v2/Login",
        "sendHeaders": True,
        "headerParameters": {"parameters": [{}]},
        "sendBody": True,
        "bodyParameters": {
            "parameters": [
                {"name": "CompanyDB", "value": "SBO_WFB_PRD"},
                {"name": "Password", "value": "jfc2025"},
                {"name": "UserName", "value": "desenvolvimento"},
            ]
        },
        "options": {},
    },
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "name": "HTTP Request",
}

DBLINK_QUERY = (
    "SELECT  c.chave_de_acesso\nFROM chaves_de_acesso_para_comparativo c\n\n"
    "LEFT JOIN dblink(\n"
    "    'host=192.168.0.142 dbname=DB-ANEXOS user=postgres password=postgres',\n"
    "    'SELECT chave_de_acesso FROM notas_entrada_header'\n"
    ") AS n(chave_de_acesso varchar)\n"
    "ON n.chave_de_acesso = c.chave_de_acesso\n"
)


class TestSegredosReais:
    def test_senha_sap_nao_sobrevive(self):
        out = redact(SAP_LOGIN_NODE)
        flat = repr(out)
        assert "jfc2025" not in flat, "senha SAP vazou para a saida"
        assert REDACTED in flat

    def test_credencial_nao_derruba_o_irmao_util(self):
        """CompanyDB/UserName sao config, nao segredo. Sobre-redigir tambem e bug."""
        out = redact(SAP_LOGIN_NODE)
        params = out["parameters"]["bodyParameters"]["parameters"]
        by_name = {p["name"]: p["value"] for p in params}
        assert by_name["CompanyDB"] == "SBO_WFB_PRD"
        assert by_name["UserName"] == "desenvolvimento"
        assert by_name["Password"] == REDACTED

    def test_dblink_redige_a_senha_dentro_da_query(self):
        out = redact_text(DBLINK_QUERY)
        assert "password=postgres" not in out
        # o resto da query precisa sobreviver intacto, senao o diagnostico e inutil
        assert "chaves_de_acesso_para_comparativo" in out
        assert "notas_entrada_header" in out
        assert "192.168.0.142" in out


class TestPadroes:
    @pytest.mark.parametrize(
        ("entrada", "nao_pode_vazar"),
        [
            ({"password": "abc"}, "abc"),
            ({"Password": "abc"}, "abc"),
            ({"smtpPassword": "abc"}, "abc"),
            ({"apiKey": "sk-1234567890"}, "sk-1234567890"),
            ({"X-N8N-API-KEY": "napi_abc123"}, "napi_abc123"),
            ({"auth": "Bearer eyJhbGciOi"}, "eyJhbGciOi"),
        ],
    )
    def test_chaves_sensiveis(self, entrada, nao_pode_vazar):
        assert nao_pode_vazar not in repr(redact(entrada))

    def test_segredo_em_texto_real(self):
        for texto in [
            "postgresql://app:sup3rs3cret@db.prod:5432/app",
            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
            "password=hunter2;host=x",
            "api_key=AKIAIOSFODNN7EXAMPLE",
        ]:
            assert contains_secret(texto), f"redact_text nao detectou segredo em: {texto}"
            out = redact_text(texto)
            for seg in ["sup3rs3cret", "eyJhbGciOiJIUzI1NiJ9", "hunter2", "AKIAIOSFODNN7EXAMPLE"]:
                assert seg not in out, f"'{seg}' sobreviveu em: {out}"


class TestEstrutura:
    def test_idempotente(self):
        once = redact(SAP_LOGIN_NODE)
        assert redact(once) == once

    def test_nao_altera_tipo_nem_chave(self):
        out = redact(SAP_LOGIN_NODE)
        assert isinstance(out, dict)
        assert out["type"] == "n8n-nodes-base.httpRequest"
        assert len(out["parameters"]["bodyParameters"]["parameters"]) == 3

    def test_lista_e_preservada(self):
        assert redact([1, "a", {"token": "z"}]) == [1, "a", {"token": REDACTED}]

    def test_profundidade_excessiva_redige(self):
        """Acima de MAX_DEPTH redige em vez de recursar ate estourar a pilha."""
        deep = cur = {}
        for _ in range(40):
            cur["filho"] = {}
            cur = cur["filho"]
        cur["senha"] = "nao_deve_vazar"
        assert "nao_deve_vazar" not in repr(redact(deep))

    def test_none_e_vazio_preservados(self):
        assert redact(None) is None
        assert redact({"a": None})["a"] is None
        assert redact("") == ""