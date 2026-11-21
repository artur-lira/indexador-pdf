"""Banco de dados do índice (SQLite + FTS5).

O índice fica SEMPRE fora da pasta indexada (por padrão em
%LOCALAPPDATA%\\IndexadorPDF\\indices), um arquivo por pasta-raiz. Os arquivos
de origem nunca são abertos para escrita.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import zlib
from datetime import datetime
from pathlib import Path

VERSAO_ESQUEMA = 1

ESQUEMA = """
CREATE TABLE IF NOT EXISTS meta (
    chave TEXT PRIMARY KEY,
    valor TEXT
);
CREATE TABLE IF NOT EXISTS documentos (
    id              INTEGER PRIMARY KEY,
    caminho         TEXT NOT NULL UNIQUE,   -- relativo à pasta-raiz
    tamanho         INTEGER NOT NULL,
    mtime_ns        INTEGER NOT NULL,
    sha256          TEXT,
    paginas         INTEGER DEFAULT 0,
    paginas_ocr     INTEGER DEFAULT 0,
    paginas_vazias  INTEGER DEFAULT 0,      -- sem texto e sem OCR
    ocr_feito       INTEGER DEFAULT 0,      -- 1 = OCR já foi tentado nas páginas vazias
    status          TEXT NOT NULL,          -- ok | erro | protegido
    erro            TEXT,
    indexado_em     TEXT
);
CREATE TABLE IF NOT EXISTS paginas (
    id        INTEGER PRIMARY KEY,
    doc_id    INTEGER NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
    numero    INTEGER NOT NULL,             -- 1, 2, 3...
    origem    TEXT NOT NULL,                -- texto | ocr | vazia
    palavras  BLOB                          -- caixas das palavras (só OCR), zlib+json
);
CREATE INDEX IF NOT EXISTS ix_paginas_doc ON paginas(doc_id, numero);
CREATE VIRTUAL TABLE IF NOT EXISTS conteudo USING fts5(
    texto,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


def pasta_dados() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    p = base / "IndexadorPDF"
    p.mkdir(parents=True, exist_ok=True)
    return p


def normalizar_raiz(raiz: str | os.PathLike) -> str:
    r = os.path.normpath(os.path.abspath(os.fspath(raiz)))
    return r


def arquivo_indice(raiz: str) -> Path:
    chave = normalizar_raiz(raiz)
    if sys.platform == "win32":
        chave = chave.lower()
    h = hashlib.sha1(chave.encode("utf-8")).hexdigest()[:16]
    nome = "".join(ch if ch.isalnum() else "_" for ch in Path(chave).name)[:40] or "raiz"
    d = pasta_dados() / "indices"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{nome}_{h}.sqlite"


def conectar(raiz: str, somente_leitura: bool = False) -> sqlite3.Connection:
    caminho = arquivo_indice(raiz)
    if somente_leitura:
        if not caminho.exists():
            raise FileNotFoundError(str(caminho))
        con = sqlite3.connect(f"file:{caminho.as_posix()}?mode=ro", uri=True,
                              check_same_thread=False)
    else:
        con = sqlite3.connect(str(caminho), check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-65536")  # 64 MB
    if not somente_leitura:
        con.executescript(ESQUEMA)
        con.execute("INSERT OR IGNORE INTO meta VALUES ('raiz', ?)", (normalizar_raiz(raiz),))
        con.execute("INSERT OR IGNORE INTO meta VALUES ('esquema', ?)", (str(VERSAO_ESQUEMA),))
        con.commit()
    return con


def indice_existe(raiz: str) -> bool:
    return arquivo_indice(raiz).exists()


def agora() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def compactar_palavras(palavras) -> bytes | None:
    if not palavras:
        return None
    dados = [[round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1), w[4]]
             for w in palavras]
    return zlib.compress(json.dumps(dados, ensure_ascii=False).encode("utf-8"), 6)


def descompactar_palavras(blob: bytes | None):
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def remover_documento(con: sqlite3.Connection, doc_id: int) -> None:
    con.execute("DELETE FROM conteudo WHERE rowid IN (SELECT id FROM paginas WHERE doc_id=?)",
                (doc_id,))
    con.execute("DELETE FROM paginas WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM documentos WHERE id=?", (doc_id,))


def gravar_documento(con: sqlite3.Connection, r: dict) -> None:
    """Insere/substitui um documento já extraído (resultado de extracao.extrair)."""
    row = con.execute("SELECT id FROM documentos WHERE caminho=?", (r["caminho"],)).fetchone()
    if row:
        remover_documento(con, row[0])
    paginas = r.get("paginas") or []
    cur = con.execute(
        "INSERT INTO documentos (caminho, tamanho, mtime_ns, sha256, paginas, paginas_ocr,"
        " paginas_vazias, ocr_feito, status, erro, indexado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (r["caminho"], r["tamanho"], r["mtime_ns"], r.get("sha256"), r.get("num_paginas", 0),
         sum(1 for p in paginas if p[2] == "ocr"),
         sum(1 for p in paginas if p[2] == "vazia"), 1 if r.get("ocr_feito") else 0,
         r["status"], r.get("erro"), agora()))
    doc_id = cur.lastrowid
    for numero, texto, origem, palavras in paginas:
        c = con.execute("INSERT INTO paginas (doc_id, numero, origem, palavras) VALUES (?,?,?,?)",
                        (doc_id, numero, origem, compactar_palavras(palavras)))
        if texto:
            con.execute("INSERT INTO conteudo (rowid, texto) VALUES (?, ?)", (c.lastrowid, texto))


def estatisticas(con: sqlite3.Connection) -> dict:
    d = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(paginas),0), COALESCE(SUM(paginas_ocr),0),"
        " COALESCE(SUM(paginas_vazias),0), COALESCE(SUM(status<>'ok'),0) FROM documentos"
    ).fetchone()
    return {"documentos": d[0], "paginas": d[1], "paginas_ocr": d[2],
            "paginas_vazias": d[3], "com_problema": d[4]}
