"""Seleção de texto em fluxo (letra a letra, linha a linha), como nos leitores de PDF.

Trabalha nas coordenadas da página sem rotação (as mesmas do PyMuPDF). Não depende
de Qt, para poder ser testado isoladamente.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Linha:
    x0: float
    y0: float
    x1: float
    y1: float
    ini: int   # índice do 1º caractere da linha
    fim: int   # índice após o último caractere


class CaracteresPagina:
    """Lista ordenada de caracteres com caixa. Posições de "cursor" vão de 0 a n
    (entre caracteres); uma seleção é o intervalo [a, b)."""

    def __init__(self):
        self.chars: list[tuple[float, float, float, float, str]] = []
        self.linha_de: list[int] = []
        self.linhas: list[Linha] = []

    # ---------------- construção ----------------
    def _nova_linha(self, itens):
        itens = [c for c in itens if c[4] != ""]
        if not itens:
            return
        ini = len(self.chars)
        idx = len(self.linhas)
        for c in itens:
            self.chars.append(c)
            self.linha_de.append(idx)
        self.linhas.append(Linha(min(c[0] for c in itens), min(c[1] for c in itens),
                                 max(c[2] for c in itens), max(c[3] for c in itens),
                                 ini, len(self.chars)))

    @classmethod
    def de_rawdict(cls, raw: dict) -> "CaracteresPagina":
        """A partir de ``page.get_text("rawdict")``."""
        cp = cls()
        for b in raw.get("blocks", []):
            if b.get("type", 0) != 0:
                continue
            for ln in b.get("lines", []):
                itens = []
                for sp in ln.get("spans", []):
                    for ch in sp.get("chars", []):
                        x0, y0, x1, y1 = ch["bbox"]
                        itens.append((x0, y0, x1, y1, ch["c"]))
                cp._nova_linha(itens)
        return cp

    @classmethod
    def de_palavras(cls, palavras) -> "CaracteresPagina":
        """A partir de palavras (x0, y0, x1, y1, texto, ...) — usado nas páginas de OCR.
        A largura de cada letra é estimada dividindo a palavra igualmente."""
        cp = cls()
        itens: list = []
        y_ant = None
        for w in palavras:
            x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
            if not txt:
                continue
            cy = (y0 + y1) / 2
            if y_ant is not None and abs(cy - y_ant) > max(2.0, (y1 - y0) * 0.5):
                cp._nova_linha(itens)
                itens = []
            if itens:  # espaço entre palavras
                px1 = itens[-1][2]
                itens.append((px1, y0, max(px1, x0), y1, " "))
            larg = (x1 - x0) / len(txt)
            for k, c in enumerate(txt):
                itens.append((x0 + k * larg, y0, x0 + (k + 1) * larg, y1, c))
            y_ant = cy
        cp._nova_linha(itens)
        return cp

    # ---------------- consulta ----------------
    def __len__(self):
        return len(self.chars)

    def posicao(self, x: float, y: float) -> int | None:
        """Posição de cursor mais próxima do ponto (x, y)."""
        if not self.linhas:
            return None
        melhor, dmin = None, None
        for i, l in enumerate(self.linhas):
            dy = 0.0 if l.y0 <= y <= l.y1 else min(abs(y - l.y0), abs(y - l.y1))
            dx = 0.0 if l.x0 <= x <= l.x1 else min(abs(x - l.x0), abs(x - l.x1))
            d = dy * 4 + dx  # prioriza a linha na altura do cursor
            if dmin is None or d < dmin:
                melhor, dmin = i, d
        l = self.linhas[melhor]
        for k in range(l.ini, l.fim):
            c = self.chars[k]
            if x < (c[0] + c[2]) / 2:
                return k
        return l.fim

    _PONTUACAO = ".,;:!?()[]{}\"'\u201c\u201d\u2018\u2019\u00ab\u00bb"

    def palavra_em(self, pos: int) -> tuple[int, int]:
        """Intervalo da "palavra" sob o cursor (duplo clique): sequência sem espaços,
        sem a pontuação das pontas — CPF, placas e números de processo vêm inteiros."""
        n = len(self.chars)
        if not n:
            return 0, 0
        pos = min(max(pos, 0), n - 1)
        l = self.linhas[self.linha_de[pos]]
        if self.chars[pos][4].isspace():
            return pos, pos + 1
        a = pos
        while a > l.ini and not self.chars[a - 1][4].isspace():
            a -= 1
        b = pos
        while b < l.fim and not self.chars[b][4].isspace():
            b += 1
        while a < b - 1 and self.chars[a][4] in self._PONTUACAO:
            a += 1
        while b - 1 > a and self.chars[b - 1][4] in self._PONTUACAO:
            b -= 1
        return a, b

    def linha_em(self, pos: int) -> tuple[int, int]:
        n = len(self.chars)
        if not n:
            return 0, 0
        l = self.linhas[self.linha_de[min(max(pos, 0), n - 1)]]
        return l.ini, l.fim

    def texto(self, a: int, b: int) -> str:
        a, b = min(a, b), max(a, b)
        partes: list[str] = []
        ant = None
        for k in range(a, b):
            li = self.linha_de[k]
            if ant is not None and li != ant:
                partes.append("\n")
            partes.append(self.chars[k][4])
            ant = li
        return "\n".join(s.rstrip() for s in "".join(partes).split("\n"))

    def retangulos(self, a: int, b: int) -> list[tuple[float, float, float, float]]:
        """Um retângulo por linha cobrindo os caracteres selecionados."""
        a, b = min(a, b), max(a, b)
        res = []
        k = a
        while k < b:
            li = self.linha_de[k]
            l = self.linhas[li]
            fim = min(b, l.fim)
            cs = self.chars[k:fim]
            res.append((min(c[0] for c in cs), l.y0, max(c[2] for c in cs), l.y1))
            k = fim
        return res
