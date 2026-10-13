"""Log do Docker sem TTY vem em quadros de 8 bytes de cabecalho."""

from __future__ import annotations

from agent_nio_monitor.sources.portainer.client import decode_docker_log


def _quadro(tipo: int, texto: bytes) -> bytes:
    return bytes([tipo, 0, 0, 0]) + len(texto).to_bytes(4, "big") + texto


def test_remove_cabecalhos_e_junta_stdout_e_stderr() -> None:
    raw = _quadro(1, b"ok\n") + _quadro(2, b"erro\n")
    assert decode_docker_log(raw) == "ok\nerro\n"


def test_log_com_tty_vem_texto_puro() -> None:
    assert decode_docker_log(b"linha 1\nlinha 2\n") == "linha 1\nlinha 2\n"


def test_bytes_invalidos_nao_levantam() -> None:
    assert "\ufffd" in decode_docker_log(b"\xff\xfe falhou")
