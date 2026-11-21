"""Varredura da pasta e indexação incremental em paralelo."""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Callable

from . import banco
from .extracao import extrair


@dataclass
class ResumoIndexacao:
    encontrados: int = 0
    novos: int = 0
    alterados: int = 0
    removidos: int = 0
    inalterados: int = 0
    indexados: int = 0
    com_erro: int = 0
    paginas: int = 0
    segundos: float = 0.0
    cancelado: bool = False
    erros_varredura: int = 0


def varrer(raiz: str, cancelar: threading.Event | None = None,
           erros: list | None = None) -> dict[str, tuple[int, int]]:
    """Retorna {caminho_relativo: (tamanho, mtime_ns)} de todos os .pdf.
    Não segue atalhos/links simbólicos de diretório (evita laços)."""
    achados: dict[str, tuple[int, int]] = {}
    pilha = [raiz]
    while pilha:
        if cancelar is not None and cancelar.is_set():
            break
        d = pilha.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            pilha.append(e.path)
                        elif e.name.lower().endswith(".pdf") and e.is_file():
                            st = e.stat()
                            rel = os.path.relpath(e.path, raiz)
                            achados[rel] = (st.st_size, st.st_mtime_ns)
                    except OSError as ex:
                        if erros is not None:
                            erros.append((e.path, str(ex)))
        except OSError as ex:
            if erros is not None:
                erros.append((d, str(ex)))
    return achados


def indexar(raiz: str,
            ocr: dict | None = None,
            processos: int | None = None,
            progresso: Callable[[int, int, str], None] | None = None,
            log: Callable[[str], None] | None = None,
            cancelar: threading.Event | None = None) -> ResumoIndexacao:
    """Indexa (ou atualiza) o índice da pasta ``raiz``."""
    t0 = time.perf_counter()
    raiz = banco.normalizar_raiz(raiz)
    cancelar = cancelar or threading.Event()
    log = log or (lambda s: None)
    progresso = progresso or (lambda a, b, c: None)
    res = ResumoIndexacao()

    con = banco.conectar(raiz)
    try:
        log(f"Varrendo {raiz} …")
        erros_var: list = []
        disco = varrer(raiz, cancelar, erros_var)
        res.erros_varredura = len(erros_var)
        for p, e in erros_var[:20]:
            log(f"Sem acesso: {p} ({e})")
        res.encontrados = len(disco)
        if cancelar.is_set():
            res.cancelado = True
            return res

        no_banco = {c: (i, t, m, s, v, o) for i, c, t, m, s, v, o in con.execute(
            "SELECT id, caminho, tamanho, mtime_ns, status, paginas_vazias, ocr_feito"
            " FROM documentos")}
        # removidos: não remove nada que esteja sob uma pasta que falhou na leitura
        # (ex.: unidade de rede instável), para não apagar o índice por engano
        dirs_falhos = [os.path.relpath(p, raiz) for p, _e in erros_var]
        for c, (doc_id, *_r) in no_banco.items():
            if c not in disco and not any(
                    d == "." or c == d or c.startswith(d + os.sep) for d in dirs_falhos):
                banco.remover_documento(con, doc_id)
                res.removidos += 1
        con.commit()

        fila: list[str] = []
        for c, (t, m) in disco.items():
            ant = no_banco.get(c)
            if ant is None:
                res.novos += 1
                fila.append(c)
            elif ant[1] != t or ant[2] != m:
                res.alterados += 1
                fila.append(c)
            elif ocr and ant[3] == "ok" and ant[4] and not ant[5]:
                # OCR ligado agora: reprocessa documentos que tinham páginas sem texto
                res.alterados += 1
                fila.append(c)
            else:
                res.inalterados += 1
        # maiores primeiro: melhora o balanceamento entre processos
        fila.sort(key=lambda c: -disco[c][0])
        total = len(fila)
        log(f"{res.encontrados} PDFs encontrados: {res.novos} novos, {res.alterados} alterados, "
            f"{res.removidos} removidos, {res.inalterados} sem alteração.")
        progresso(0, total, "")
        if not total:
            return res

        n = processos or max(1, min(8, (os.cpu_count() or 2) - 1))
        _executar(raiz, fila, n, ocr, con, res, progresso, log, cancelar)
        res.cancelado = cancelar.is_set()
        con.execute("INSERT OR REPLACE INTO meta VALUES ('ultima_indexacao', ?)", (banco.agora(),))
        con.commit()
        if res.indexados > 200 and not res.cancelado:
            log("Otimizando o índice…")
            con.execute("INSERT INTO conteudo(conteudo) VALUES('optimize')")
            con.commit()
        return res
    finally:
        res.segundos = time.perf_counter() - t0
        con.close()


def _executar(raiz, fila, n, ocr, con, res, progresso, log, cancelar):
    pendentes = list(reversed(fila))  # pop() do fim = maiores primeiro
    tentativas: dict[str, int] = {}
    feitos = 0
    total = len(fila)
    ultimo_commit = time.monotonic()
    desde_commit = 0
    max_em_voo = n * 3

    def novo_pool():
        return ProcessPoolExecutor(max_workers=n)

    pool = novo_pool()
    em_voo: dict = {}
    try:
        while (pendentes or em_voo) and not cancelar.is_set():
            while pendentes and len(em_voo) < max_em_voo:
                rel = pendentes.pop()
                fut = pool.submit(extrair, os.path.join(raiz, rel), rel, ocr)
                em_voo[fut] = rel
            prontos, _ = wait(list(em_voo), timeout=0.5, return_when=FIRST_COMPLETED)
            quebrou = False
            for fut in prontos:
                rel = em_voo.pop(fut)
                try:
                    r = fut.result()
                except BrokenProcessPool:
                    quebrou = True
                    tentativas[rel] = tentativas.get(rel, 0) + 1
                    if tentativas[rel] < 2:
                        pendentes.append(rel)
                        continue
                    st = _stat(os.path.join(raiz, rel))
                    r = {"caminho": rel, "status": "erro", "tamanho": st[0], "mtime_ns": st[1],
                         "erro": "O leitor de PDF falhou ao processar este arquivo", "paginas": []}
                except Exception as e:
                    st = _stat(os.path.join(raiz, rel))
                    r = {"caminho": rel, "status": "erro", "tamanho": st[0], "mtime_ns": st[1],
                         "erro": f"{type(e).__name__}: {e}", "paginas": []}
                banco.gravar_documento(con, r)
                feitos += 1
                desde_commit += 1
                res.indexados += 1
                res.paginas += r.get("num_paginas", 0)
                if r["status"] != "ok":
                    res.com_erro += 1
                    log(f"[{r['status']}] {rel}: {r.get('erro')}")
                progresso(feitos, total, rel)
            if quebrou:
                # todos os itens em voo do pool quebrado voltam para a fila
                for fut, rel in list(em_voo.items()):
                    tentativas[rel] = tentativas.get(rel, 0) + 1
                    pendentes.append(rel)
                em_voo.clear()
                pool.shutdown(wait=False, cancel_futures=True)
                pool = novo_pool()
                log("Um processo de leitura falhou; reiniciando os processos…")
            if desde_commit >= 50 or (desde_commit and time.monotonic() - ultimo_commit > 3):
                con.commit()
                desde_commit = 0
                ultimo_commit = time.monotonic()
        con.commit()
    finally:
        pool.shutdown(wait=not cancelar.is_set(), cancel_futures=True)


def _stat(p: str) -> tuple[int, int]:
    try:
        st = os.stat(p)
        return st.st_size, st.st_mtime_ns
    except OSError:
        return 0, 0
