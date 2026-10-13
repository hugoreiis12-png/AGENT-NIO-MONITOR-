"""Templates de email.

Requisito do dono: **detalhado porem direto** -- sem enrolar sobre o que
aconteceu. Isso tem consequencia pratica: o primeiro paragrafo tem que dizer
O QUE QUEBROU e DESDE QUANDO, e o resto da vida so depois. O contrario (narrativa
com o problema no fim) e o que faz as pessoas ignorarem alerta.

Contexto e sempre o redigido.
"""

from __future__ import annotations

from datetime import datetime, UTC

SEV_LABEL = {
    "critical": "CRITICO",
    "high": "ALTO",
    "medium": "MEDIO",
    "low": "BAIXO",
    "info": "INFO",
}

SUBJECT = {
    "workflow_failing": "[{sev}] {nome} falhou {n}x em {min}min",
    "execution_failed": "[{sev}] {nome} falhou",
    "workflow_changed": "[{sev}] {nome} foi alterado",
    "workflow_deactivated": "[{sev}] {nome} foi DESATIVADO",
    "workflow_activated": "[{sev}] {nome} foi ativado",
    "credential_updated": "[{sev}] Credencial alterada: {nome}",
    "container_down": "[{sev}] Container parado: {nome}",
    "restart_loop": "[{sev}] {nome} em loop de reinicio",
    "reconciliation_gap": "[{sev}] Falha na coleta -- buraco de {min}min",
    "source_down": "[{sev}] {fonte} indisponivel",
    "generic": "[{sev}] {titulo}",
}

# Cabecalho: a resposta para "isso importa pra mim?" em uma linha.
_HEAD = """{titulo}

Projeto : {projeto}
Recurso : {recurso}
Quando  : {quando}
{sev_linha}"""

_BODY_EXEC = """
O que aconteceu
----------------
{workflow_nome} ({workflow_id}) falhou {n} vez(es).
Primeira falha : {primeira}
Ultima falha   : {ultima}
Modo de execucao: {modo}
{erro}

Contexto
--------
{contexto}

Acao sugerida
-------------
{acao}
{link_workflow}"""

_BODY_CHANGED = """
O que mudou
-----------
{workflow_nome} ({workflow_id})
Detectado em : {quando}
{autor}

{resumo_diff}

Acao sugerida
-------------
{acao}

Ver no n8n: {url_workflow}
"""

_BODY_SIMPLE = """
{detalhe}

Acao sugerida
-------------
{acao}
"""


# Acao padrao por template. Sem isso o render quebrava com KeyError -- o que
# significa que NENHUM email saía. Um template que estoura em producao e o mesmo
# que template inexistente.
ACAO_PADRAO = {
    "workflow_failing": (
        "Abrir o workflow no n8n e olhar o ultimo no executado. "
        "Se a falha se repete no mesmo horario, e job agendado quebrado, "
        "nao incidente."
    ),
    "execution_failed": "Abrir a execucao no n8n e ver o erro completo.",
    "workflow_changed": "Conferir se a mudanca era esperada e o que mais mudou junto.",
    "workflow_deactivated": "Confirmar com quem desativou e reativar se foi engano.",
    "workflow_activated": "Nada a fazer. Registro para auditoria.",
    "credential_updated": "Confirmar a rotacao e verificar se algum workflow depende do valor antigo.",
    "container_down": "Verificar os logs do container e o resource limit.",
    "restart_loop": "Checar memoria/CPU e a ultima alteracao no servico.",
    "reconciliation_gap": "O monitor perdeu eventos. Conferir se o n8n podou execucoes antigas.",
    "source_down": "Restaurar o servico. O monitor nao consegue notificar por email enquanto isso.",
    "generic": "Ver o detalhe do evento.",
}


def _padrao(d: dict, chave: str, fallback: str) -> None:
    """Preenche se ausente OU None.

    `dict.setdefault(k, v)` nao substitui um valor que ja existe e e None --
    e o contexto vem do banco, onde ausencia e NULL. Resultado sem isso: o
    alerta dizia "Erro: None".
    """
    if d.get(chave) in (None, ""):
        d[chave] = fallback


def _duracao_minutos(d: dict) -> str:
    """Janela real da falha, calculada das datas.

    O assunto dizia 'em ?min' porque so 'primeira'/'ultima' chegam do banco e o
    '?' expunha que o campo nao tinha sido derivado. '9x em 1440min' e pior que
    nao dizer nada: melhor omitir o sufixo do que mostrar interrogacao.
    """
    explicito = d.get("minutos")
    if explicito not in (None, "", "?"):
        return str(explicito)
    primeira, ultima = d.get("primeira"), d.get("ultima")
    if not (primeira and ultima):
        return ""
    try:
        p = datetime.fromisoformat(str(primeira).replace("Z", "+00:00"))
        u = datetime.fromisoformat(str(ultima).replace("Z", "+00:00"))
    except ValueError:
        return ""
    minutos = int((u - p).total_seconds() // 60)
    return str(minutos) if minutos > 0 else ""


def _assunto(template: str, event, d: dict) -> str:
    """Monta o assunto e limpa o que ficou vazio.

    'em ?min' ou 'em 0min' no assunto e pior que nao ter o sufixo: entrega ruido
    e denuncia que um campo nao foi derivado.
    """
    minutos = _duracao_minutos(d)
    texto = SUBJECT.get(template, SUBJECT["generic"]).format(
        sev=SEV_LABEL.get(event.severity, event.severity),
        nome=d.get("workflow_nome") or "-",
        n=event.streak_count,
        min=minutos,
        titulo=event.title,
        fonte=d.get("fonte") or "-",
    )
    if template == "workflow_failing" and not minutos:
        texto = SUBJECT["workflow_failing"].replace(
            " em {min}min", ""
        ).format(
            sev=SEV_LABEL.get(event.severity, event.severity),
            nome=d.get("workflow_nome") or "-",
            n=event.streak_count,
        )
    return texto


def render(
    template: str,
    *,
    event,  # noqa: ANN001 - EventRecord
    contexto: dict | None = None,
) -> tuple[str, str]:
    """Devolve (assunto, corpo). Sem HTML: o requisito e texto direto, e HTML
    num alerta que precisa ser lido no celular e' pior que texto."""
    d = dict(contexto or {})
    _padrao(d, "projeto", event.tenant_id or "(nao mapeado)")
    _padrao(d, "recurso", event.resource_id or "-")
    _padrao(d, "quando", event.ts.strftime("%Y-%m-%d %H:%M:%S %Z"))
    _padrao(d, "sev_linha", f"Severidade: {SEV_LABEL.get(event.severity, event.severity)}")
    _padrao(d, "workflow_nome", d.get("recurso") or "-")
    _padrao(d, "workflow_id", event.resource_id or "-")
    _padrao(d, "n", event.streak_count)
    _padrao(d, "modo", "?")
    _padrao(d, "erro", "(sem mensagem de erro)")
    _padrao(d, "contexto", "-")
    _padrao(d, "detalhe", "-")
    _padrao(d, "resumo_diff", "(sem diferenca registrada)")
    _padrao(d, "autor", "Autoria nao disponivel nesta versao do n8n")
    _padrao(d, "primeira", "-")
    _padrao(d, "ultima", "-")
    _padrao(d, "min", "?")
    _padrao(d, "titulo", event.title)
    _padrao(d, "url_workflow", "")
    _padrao(d, "acao", ACAO_PADRAO.get(template, ACAO_PADRAO["generic"]))

    # Espelho da linha "Acao sugerida", abaixo. Sem URL conhecida a linha
    # inteira some -- imprimir "Ver no n8n: -" e pior que nao imprimir nada.
    d["link_workflow"] = f"Ver no n8n: {d['url_workflow']}\n" if d["url_workflow"] else ""

    assunto = _assunto(template, event, d)

    if template in ("workflow_failing", "execution_failed"):
        corpo = _BODY_EXEC.format(**d)
    elif template == "workflow_changed":
        corpo = _BODY_CHANGED.format(**d)
    else:
        corpo = _BODY_SIMPLE.format(**d)

    return assunto, f"{_HEAD.format(**d)}\n{corpo}{_quem_mexeu(contexto or {}, template)}".strip()


def _quem_mexeu(contexto: dict, template: str) -> str:
    """Quem alterou o recurso por ultimo e quem e dono da stack, quando se sabe."""
    linhas = []
    if contexto.get("stack"):
        linhas.append(f"Stack: {contexto['stack']}")
    if contexto.get("responsavel_stack"):
        linhas.append(f"Responsavel pela stack: {contexto['responsavel_stack']}")
    if contexto.get("autor") and template != "workflow_changed":
        linhas.append(f"Ultima alteracao por: {contexto['autor']}")
    if not linhas:
        return ""
    return "\n\nQuem mexeu\n-----------\n" + "\n".join(linhas)


def formatar_horas(dt: datetime | None) -> str:
    """Instante em UTC COM o rotulo do fuso.

    A versao anterior imprimia `19:00:00` sem fuso, num servidor em `Hora oficial
    do Brasil`. Quem lê em UTC-3 entende 19:00 local e erra a janela em 6 horas --
    o alerta passa a dizer que a falha começou antes de realmente começar.

    O alert e sobre o que aconteceu num servidor em fusos diferentes de quem le,
    entao UTC rotulado e o honesto. Converter para o fuso do servidor seria
    igualmente enganoso.
    """
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d %H:%M:%S") + " (sem fuso)"
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")