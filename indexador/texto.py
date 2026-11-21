"""Normalização de texto, interpretação da consulta e localização de ocorrências.

O mesmo critério de tokenização é usado em três lugares, para que os resultados
sejam coerentes entre si:

* no índice SQLite FTS5 (tokenizador ``unicode61 remove_diacritics 2``);
* na extração dos trechos (contexto de cada ocorrência) a partir do texto salvo;
* no destaque das ocorrências na pré-visualização da página.

Um "token" é uma sequência de letras/dígitos (sublinhado e pontuação separam),
comparada sem diferenciar maiúsculas/minúsculas e sem acentos.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Normalização ("dobra") preservando o comprimento da string.
# Cada caractere vira exatamente um caractere, de modo que os índices do texto
# dobrado correspondem aos índices do texto original.
# --------------------------------------------------------------------------

_TABELA: dict[int, int] = {}
for _cp in range(0x20, 0x2FFF):
    _c = chr(_cp)
    _d = unicodedata.normalize("NFD", _c)
    _b = _d[0] if _d else _c
    _l = _b.lower()
    _l = _l[0] if _l else _b
    if _l != _c:
        _TABELA[_cp] = ord(_l)
# ligaduras comuns em PDFs (caso não tenham sido expandidas pelo extrator)
for _lig, _sub in {"ﬁ": "f", "ﬂ": "f", "ﬀ": "f"}.items():
    _TABELA[ord(_lig)] = ord(_sub)
del _cp, _c, _d, _b, _l


def dobrar(texto: str) -> str:
    """Minúsculas e sem acentos, com o MESMO comprimento do original."""
    return texto.translate(_TABELA)


_RE_TOKEN = re.compile(r"[^\W_]+")


def tokens_com_posicao(texto: str) -> list[tuple[str, int, int]]:
    """Lista de (token_dobrado, inicio, fim) no texto original."""
    dob = dobrar(texto)
    return [(m.group(), m.start(), m.end()) for m in _RE_TOKEN.finditer(dob)]


def tokenizar(texto: str) -> list[str]:
    return _RE_TOKEN.findall(dobrar(texto))


# --------------------------------------------------------------------------
# Consulta
# --------------------------------------------------------------------------

MODO_TODAS = "todas"        # todas as palavras/expressões (E)
MODO_QUALQUER = "qualquer"  # qualquer uma (OU)
MODO_FRASE = "frase"        # frase exata


@dataclass
class Termo:
    tokens: list[str]
    prefixo: bool = False   # último token aceita complemento (termo*)
    excluir: bool = False   # -termo (NÃO)


@dataclass
class Consulta:
    texto: str
    modo: str
    termos: list[Termo] = field(default_factory=list)

    @property
    def positivos(self) -> list[Termo]:
        return [t for t in self.termos if not t.excluir]

    def fts(self) -> str:
        """Expressão MATCH do FTS5 (sempre com aspas, imune a sintaxe)."""
        def frag(t: Termo) -> str:
            s = '"' + " ".join(t.tokens) + '"'
            return s + "*" if t.prefixo else s

        pos = [frag(t) for t in self.positivos]
        neg = [frag(t) for t in self.termos if t.excluir]
        if not pos:
            return ""
        if self.modo == MODO_QUALQUER:
            expr = " OR ".join(pos)
            if len(pos) > 1 and neg:
                expr = "(" + expr + ")"
        else:
            expr = " AND ".join(pos)
        for n in neg:
            expr += " NOT " + n
        return expr


_RE_PARTES = re.compile(r'(-?)"([^"]*)"(\*?)|(\S+)')


def interpretar(texto: str, modo: str = MODO_TODAS) -> Consulta:
    """Converte o que o usuário digitou em uma Consulta.

    Sintaxe aceita (modos "todas"/"qualquer"):
      palavra            -> palavra inteira (sem acento/maiúscula)
      prefix*            -> palavras que começam com "prefix"
      "duas palavras"    -> expressão exata
      -palavra           -> exclui páginas que contenham a palavra
    No modo "frase" o texto inteiro é tratado como uma expressão exata;
    CPF, placas, números de processo etc. funcionam bem neste modo
    (ex.: 123.456.789-00 vira a sequência 123 456 789 00).
    """
    texto = texto.strip()
    c = Consulta(texto=texto, modo=modo)
    if not texto:
        return c
    if modo == MODO_FRASE:
        prefixo = texto.endswith("*")
        toks = tokenizar(texto.rstrip("*"))
        if toks:
            c.termos.append(Termo(toks, prefixo=prefixo))
        return c
    for m in _RE_PARTES.finditer(texto):
        if m.group(2) is not None:
            excluir, bruto, prefixo = m.group(1) == "-", m.group(2), m.group(3) == "*"
        else:
            bruto = m.group(4)
            excluir = bruto.startswith("-") and len(bruto) > 1
            if excluir:
                bruto = bruto[1:]
            prefixo = bruto.endswith("*")
            bruto = bruto.rstrip("*")
        toks = tokenizar(bruto)
        if toks:
            c.termos.append(Termo(toks, prefixo=prefixo, excluir=excluir))
    return c


# --------------------------------------------------------------------------
# Localização de ocorrências numa sequência de tokens
# --------------------------------------------------------------------------

def localizar(tokens: list[str], consulta: Consulta) -> list[tuple[int, int]]:
    """Retorna intervalos [i, j) de índices de tokens que casam com algum
    termo positivo da consulta, em ordem de leitura e sem sobreposição."""
    termos = consulta.positivos
    if not termos or not tokens:
        return []
    # índice pelo primeiro token para não testar todos os termos em cada posição
    por_inicial: dict[str, list[Termo]] = {}
    prefixos_iniciais: list[Termo] = []
    for t in termos:
        if len(t.tokens) == 1 and t.prefixo:
            prefixos_iniciais.append(t)
        else:
            por_inicial.setdefault(t.tokens[0], []).append(t)

    def casa(t: Termo, i: int) -> int:
        n = len(t.tokens)
        if i + n > len(tokens):
            return 0
        for k in range(n):
            tok = tokens[i + k]
            alvo = t.tokens[k]
            if k == n - 1 and t.prefixo:
                if not tok.startswith(alvo):
                    return 0
            elif tok != alvo:
                return 0
        return n

    res: list[tuple[int, int]] = []
    i = 0
    total = len(tokens)
    while i < total:
        tok = tokens[i]
        melhor = 0
        for t in por_inicial.get(tok, ()):
            melhor = max(melhor, casa(t, i))
        if not melhor:
            for t in prefixos_iniciais:
                if tok.startswith(t.tokens[0]):
                    melhor = 1
                    break
        if melhor:
            res.append((i, i + melhor))
            i += melhor
        else:
            i += 1
    return res


@dataclass
class Ocorrencia:
    indice: int          # ordem da ocorrência na página (0, 1, 2...)
    inicio: int          # posição no texto original da página
    fim: int
    antes: str
    trecho: str
    depois: str


_RE_ESPACOS = re.compile(r"\s+")


def _limpar(s: str) -> str:
    return _RE_ESPACOS.sub(" ", s)


def ocorrencias_no_texto(texto: str, consulta: Consulta, contexto: int = 90,
                         limite: int = 200) -> list[Ocorrencia]:
    toks = tokens_com_posicao(texto)
    hits = localizar([t[0] for t in toks], consulta)
    saida: list[Ocorrencia] = []
    for n, (i, j) in enumerate(hits[:limite]):
        ini, fim = toks[i][1], toks[j - 1][2]
        a = max(0, ini - contexto)
        b = min(len(texto), fim + contexto)
        # não cortar palavra no meio do contexto
        while a > 0 and texto[a - 1].isalnum() and ini - a < contexto + 20:
            a -= 1
        while b < len(texto) and texto[b].isalnum() and b - fim < contexto + 20:
            b += 1
        antes = _limpar(texto[a:ini])
        depois = _limpar(texto[fim:b])
        if a > 0:
            antes = "…" + antes.lstrip()
        if b < len(texto):
            depois = depois.rstrip() + "…"
        saida.append(Ocorrencia(n, ini, fim, antes, _limpar(texto[ini:fim]), depois))
    return saida


def tokens_de_palavras(palavras) -> tuple[list[str], list[tuple[float, float, float, float]]]:
    """A partir de palavras com caixa (x0, y0, x1, y1, texto, ...) — formato do
    PyMuPDF ``page.get_text("words")`` — gera tokens e a caixa aproximada de
    cada token (dividindo a largura da palavra proporcionalmente)."""
    toks: list[str] = []
    caixas: list[tuple[float, float, float, float]] = []
    for w in palavras:
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        if not txt:
            continue
        n = len(txt)
        larg = (x1 - x0) / n if n else 0
        for tok, a, b in tokens_com_posicao(txt):
            toks.append(tok)
            caixas.append((x0 + a * larg, y0, x0 + b * larg, y1))
    return toks, caixas


def texto_na_area(palavras, area: tuple[float, float, float, float]) -> str:
    """Texto das palavras (formato ``page.get_text("words")``) cujo centro está dentro de
    ``area`` (x0, y0, x1, y1, nas coordenadas da página), na ordem de leitura e com
    quebras de linha preservadas."""
    ax0, ay0, ax1, ay1 = area
    linhas: list[list[str]] = []
    chave_ant = None
    y_ant = None
    for w in palavras:
        x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if not (ax0 <= cx <= ax1 and ay0 <= cy <= ay1) or not txt:
            continue
        if len(w) >= 7:
            chave = (w[5], w[6])          # bloco, linha (PDF com texto)
            nova = chave != chave_ant
        else:                             # palavras de OCR: decide pela altura
            chave = None
            nova = y_ant is None or abs(cy - y_ant) > max(2.0, (y1 - y0) * 0.5)
        if nova or not linhas:
            linhas.append([])
        linhas[-1].append(txt)
        chave_ant, y_ant = chave, cy
    return "\n".join(" ".join(l) for l in linhas)
