"""Caminho normal de cada workflow: os nos que rodam em quase toda execucao.

"No que nao rodou" so e defeito quando ele COSTUMA rodar -- ramo de IF e de erro fica
fora do caminho o tempo todo (medido: o unico no ausente nas execucoes de sucesso da
instancia era um `stopAndError`). Janela das ultimas execucoes por workflow, guardada em
`resource` (kind `node-baseline`) para sobreviver a reinicio sem migration.
"""

from __future__ import annotations

from collections import Counter

from ...store.repository import Repository

KIND = "node-baseline"
JANELA = 50
MIN_AMOSTRAS = 20
FREQUENCIA_MINIMA = 0.95


class NodeBaseline:
    def __init__(self, repo: Repository, source: str = "n8n") -> None:
        self.repo, self.source = repo, source
        self._janelas: dict[str, list[list[str]]] | None = None

    def _carregar(self) -> dict[str, list[list[str]]]:
        if self._janelas is None:
            self._janelas = {
                r["external_id"]: list((r.get("meta") or {}).get("janela") or [])
                for r in self.repo.list_resources(self.source, KIND)
            }
        return self._janelas

    def esperados(self, workflow_id: str, existentes: set[str]) -> set[str]:
        """Nos do caminho normal que ainda existem no workflow. Vazio enquanto aprende."""
        janela = self._carregar().get(workflow_id, [])
        if len(janela) < MIN_AMOSTRAS:
            return set()
        contagem = Counter(no for execucao in janela for no in set(execucao))
        return {no for no, n in contagem.items() if n / len(janela) >= FREQUENCIA_MINIMA} & existentes

    def registrar(self, workflow_id: str, nome: str | None, rodaram: set[str]) -> None:
        janela = self._carregar().setdefault(workflow_id, [])
        janela.append(sorted(rodaram))
        del janela[:-JANELA]
        self.repo.upsert_resource(
            source=self.source, kind=KIND, external_id=workflow_id, name=nome, meta={"janela": janela},
        )

    def amostras(self, workflow_id: str) -> int:
        return len(self._carregar().get(workflow_id, []))
