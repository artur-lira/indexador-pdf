"""Extração de texto de um PDF (executada em processos separados).

Regras de integridade:
* o arquivo é lido apenas em modo binário de leitura ("rb");
* o SHA-256 é calculado sobre os MESMOS bytes que são extraídos;
* nada é gravado no arquivo de origem nem na sua pasta.

Este módulo não importa Qt, para que os processos auxiliares sejam leves.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pymupdf

# Sem ligaduras preservadas (ﬁ -> fi), para a busca funcionar.
FLAGS_TEXTO = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP

LIMITE_MEMORIA = 256 * 1024 * 1024  # acima disso, abre pelo caminho em vez de memória
BLOCO = 4 * 1024 * 1024

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)


# --------------------------------------------------------------------------
# Tesseract (OCR)
# --------------------------------------------------------------------------

def _candidatos_tessdata() -> list[Path]:
    c: list[Path] = []
    env = os.environ.get("TESSDATA_PREFIX")
    if env:
        c.append(Path(env))
    # pasta "tessdata" ao lado do executável/programa (permite levar por.traineddata junto)
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent.parent
    c.append(base / "tessdata")
    if sys.platform == "win32":
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            v = os.environ.get(var)
            if v:
                c.append(Path(v) / "Tesseract-OCR" / "tessdata")
        la = os.environ.get("LOCALAPPDATA")
        if la:
            c.append(Path(la) / "Programs" / "Tesseract-OCR" / "tessdata")
            c.append(Path(la) / "Tesseract-OCR" / "tessdata")
    else:
        for p in ("/usr/share/tesseract-ocr/5/tessdata", "/usr/share/tesseract-ocr/4.00/tessdata",
                  "/usr/share/tessdata", "/usr/local/share/tessdata",
                  "/opt/homebrew/share/tessdata"):
            c.append(Path(p))
    return c


def detectar_tesseract() -> tuple[str | None, str | None]:
    """Retorna (pasta_tessdata, idiomas) ou (None, motivo)."""
    for d in _candidatos_tessdata():
        try:
            if not d.is_dir():
                continue
            langs = {p.stem for p in d.glob("*.traineddata")}
        except OSError:
            continue
        if "por" in langs:
            return str(d), "por+eng" if "eng" in langs else "por"
        if "eng" in langs:
            return str(d), "eng"
    return None, "Tesseract não encontrado (instale-o ou coloque por.traineddata numa pasta 'tessdata' ao lado do programa)"


# --------------------------------------------------------------------------
# Extração
# --------------------------------------------------------------------------

def _tem_texto(s: str) -> bool:
    return any(ch.isalnum() for ch in s)


def extrair(caminho_abs: str, caminho_rel: str, ocr: dict | None = None) -> dict:
    """Extrai o texto de todas as páginas. ``ocr`` = {"tessdata":..., "idiomas":..., "dpi":...}
    ou None. Nunca lança exceção: erros voltam em ``status``/``erro``."""
    r: dict = {"caminho": caminho_rel, "status": "ok", "erro": None, "paginas": [],
               "num_paginas": 0, "sha256": None, "tamanho": 0, "mtime_ns": 0,
               "ocr_feito": bool(ocr)}
    try:
        st = os.stat(caminho_abs)
        r["tamanho"], r["mtime_ns"] = st.st_size, st.st_mtime_ns
        h = hashlib.sha256()
        if st.st_size <= LIMITE_MEMORIA:
            with open(caminho_abs, "rb") as f:
                dados = f.read()
            h.update(dados)
            doc = pymupdf.open(stream=dados, filetype="pdf")
        else:
            with open(caminho_abs, "rb") as f:
                while True:
                    b = f.read(BLOCO)
                    if not b:
                        break
                    h.update(b)
            doc = pymupdf.open(caminho_abs, filetype="pdf")
        r["sha256"] = h.hexdigest()
    except Exception as e:  # arquivo inacessível, corrompido, etc.
        r["status"], r["erro"] = "erro", f"{type(e).__name__}: {e}"
        return r

    try:
        if doc.needs_pass:
            r["status"], r["erro"] = "protegido", "PDF protegido por senha"
            r["num_paginas"] = doc.page_count
            return r
        r["num_paginas"] = doc.page_count
        erros_pag = []
        for i in range(doc.page_count):
            try:
                page = doc.load_page(i)
                texto = page.get_text("text", flags=FLAGS_TEXTO)
                if _tem_texto(texto):
                    r["paginas"].append((i + 1, texto, "texto", None))
                    continue
                if ocr and page.get_images(full=False):
                    try:
                        tp = page.get_textpage_ocr(flags=FLAGS_TEXTO, language=ocr["idiomas"],
                                                   dpi=ocr.get("dpi", 300), full=True,
                                                   tessdata=ocr["tessdata"])
                        t_ocr = page.get_text("text", textpage=tp, flags=FLAGS_TEXTO)
                        palavras = page.get_text("words", textpage=tp, flags=FLAGS_TEXTO)
                        if _tem_texto(t_ocr):
                            r["paginas"].append((i + 1, t_ocr, "ocr",
                                                 [w[:5] for w in palavras]))
                            continue
                    except Exception as e:
                        erros_pag.append(f"p.{i + 1} OCR: {e}")
                r["paginas"].append((i + 1, "", "vazia", None))
            except Exception as e:
                erros_pag.append(f"p.{i + 1}: {e}")
                r["paginas"].append((i + 1, "", "vazia", None))
        if erros_pag:
            r["erro"] = "; ".join(erros_pag[:5]) + (" …" if len(erros_pag) > 5 else "")
    except Exception as e:
        r["status"], r["erro"] = "erro", f"{type(e).__name__}: {e}"
    finally:
        doc.close()
    return r
