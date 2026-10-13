"""Link do botao do e-mail. A base vem SEMPRE da configuracao; o conteudo do evento
so entra como identificador (escapado), nunca como URL."""

from __future__ import annotations

from urllib.parse import quote

from ..config.schema import AppConfig


def link_do_evento(detail: dict, cfg: AppConfig) -> tuple[str, str] | None:
    """(texto do botao, url) ou None quando nao ha destino conhecido."""
    ambiente = cfg.active
    workflow_id = detail.get("workflow_id")
    if workflow_id and ambiente.n8n.url:
        return "Abrir no n8n", f"{ambiente.n8n.url.rstrip('/')}/workflow/{quote(str(workflow_id), safe='')}"
    portainer = ambiente.portainer
    endpoint = detail.get("endpoint_id")
    if portainer is None or endpoint is None:
        return None
    base = f"{portainer.url.rstrip('/')}/#!/{quote(str(endpoint), safe='')}/docker"
    if detail.get("stack_id") is not None:
        stack = quote(str(detail.get("stack")), safe="")
        return "Abrir no Portainer", f"{base}/stacks/{stack}?id={quote(str(detail['stack_id']), safe='')}&type=2&regular=true"
    if detail.get("recurso"):
        return "Abrir no Portainer", f"{base}/containers/{quote(str(detail['recurso']), safe='')}"
    return None
