"""Recorta o triptico da arte JFC nas imagens usadas pelo e-mail HTML.

Entrada: assets/Triptico JFC_ Saude do Campo.png (3 cartoes lado a lado).
Saida: assets/email/logo.png e hero_{critico,medio,baixo}.jpg.
Pillow e dependencia de desenvolvimento: o agente em producao so serve os arquivos.
Rodar: python scripts/recortar_arte.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

RAIZ = Path(__file__).resolve().parents[1]
ORIGEM = next((RAIZ / "assets").glob("Tr*ptico JFC*.png"))
DESTINO = RAIZ / "assets" / "email"

# (nome, x inicial, x final) de cada cartao no triptico de 1536x1024.
CARTOES = (("critico", 14, 502), ("medio", 525, 1013), ("baixo", 1037, 1525))
HERO_Y = (305, 600)
LOGO_BOX = (190, 25, 320, 100)  # relativo ao primeiro cartao (o logo e igual nos tres)
QUALIDADE_JPEG = 85
TOLERANCIA_FUNDO = 40  # diferenca de cor (0-255) a partir da qual o pixel e totalmente opaco


def fundo_transparente(imagem: Image.Image) -> Image.Image:
    """Troca o fundo creme do recorte por transparencia (borda suave), para o logo
    nao aparecer numa caixa quando o cartao do e-mail tem um creme ligeiramente diferente."""
    rgba = imagem.convert("RGBA")
    pixels = rgba.load()
    fundo = pixels[0, 0]
    for y in range(rgba.height):
        for x in range(rgba.width):
            r, g, b, _ = pixels[x, y]
            diferenca = max(abs(r - fundo[0]), abs(g - fundo[1]), abs(b - fundo[2]))
            pixels[x, y] = (r, g, b, min(255, diferenca * 255 // TOLERANCIA_FUNDO))
    return rgba


def main() -> None:
    DESTINO.mkdir(parents=True, exist_ok=True)
    imagem = Image.open(ORIGEM).convert("RGB")
    for nome, x0, x1 in CARTOES:
        hero = imagem.crop((x0, HERO_Y[0], x1, HERO_Y[1]))
        hero.save(DESTINO / f"hero_{nome}.jpg", "JPEG", quality=QUALIDADE_JPEG, optimize=True)
    logo = fundo_transparente(imagem.crop(LOGO_BOX))
    logo.save(DESTINO / "logo.png", "PNG", optimize=True)
    for arquivo in sorted(DESTINO.iterdir()):
        print(f"{arquivo.name}: {arquivo.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
