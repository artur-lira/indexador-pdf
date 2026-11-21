"""Execução de buscas no índice."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from .texto import Consulta, Ocorrencia, ocorrencias_no_texto


@dataclass
class Resultado:
    doc_id: int
    caminho: str          # relativo à raiz
    pagina_id: int
    pagina: int           # número da página (1..n)
    origem: str           # texto | ocr
    oc: Ocorrencia


@dataclass
class ResumoBusca:
    paginas: int = 0
    documentos: int = 0
    ocorrencias: int = 0
    truncado: bool = False


def buscar(con: sqlite3.Connection, consulta: Consulta, filtro_caminho: str = "",
           ordem: str = "caminho", limite_ocorrencias: int = 20000,
           cancelar: threading.Event | None = None) -> tuple[list[Resultado], ResumoBusca]:
    expr = consulta.fts()
    resumo = ResumoBusca()
    if not expr:
        return [], resumo
    # o texto é lido depois, página a página: a ordenação não precisa carregar
    # o conteúdo de todas as páginas na memória
    sql = ("SELECT p.id, p.numero, p.origem, d.id, d.caminho "
           "FROM conteudo c JOIN paginas p ON p.id = c.rowid "
           "JOIN documentos d ON d.id = p.doc_id WHERE conteudo MATCH ?")
    args: list = [expr]
    if filtro_caminho.strip():
        for parte in filtro_caminho.split():
            sql += " AND d.caminho LIKE ? ESCAPE '\\'"
            parte = parte.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append(f"%{parte}%")
    if ordem == "relevancia":
        sql += " ORDER BY bm25(conteudo)"
    else:
        sql += " ORDER BY d.caminho COLLATE NOCASE, p.numero"

    saida: list[Resultado] = []
    cur_texto = con.cursor()
    linhas = con.execute(sql, args).fetchall()
    resumo.paginas = len(linhas)
    resumo.documentos = len({l[3] for l in linhas})
    for pid, num, origem, did, cam in linhas:
        if cancelar is not None and cancelar.is_set():
            break
        row = cur_texto.execute("SELECT texto FROM conteudo WHERE rowid=?", (pid,)).fetchone()
        texto = row[0] if row else ""
        ocs = ocorrencias_no_texto(texto or "", consulta)
        if not ocs:  # casou no FTS mas não localizamos a posição exata
            t = " ".join((texto or "").split())[:180]
            ocs = [Ocorrencia(0, -1, -1, "", "", t)]
        for oc in ocs:
            if len(saida) >= limite_ocorrencias:
                resumo.truncado = True
                break
            saida.append(Resultado(did, cam, pid, num, origem, oc))
        if resumo.truncado:
            break
    resumo.ocorrencias = len(saida)
    return saida, resumo
