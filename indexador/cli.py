"""Linha de comando (útil para lotes e testes).

    IndexadorPDF.exe --cli indexar "D:\\Acervo" [--ocr]
    IndexadorPDF.exe --cli buscar "D:\\Acervo" "termo de busca" [--modo frase]
"""

from __future__ import annotations

import argparse
import sys

from . import banco
from .busca import buscar
from .extracao import detectar_tesseract
from .indexacao import indexar
from .texto import interpretar


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="IndexadorPDF --cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("indexar")
    a.add_argument("pasta")
    a.add_argument("--ocr", action="store_true")
    a.add_argument("--processos", type=int)
    b = sub.add_parser("buscar")
    b.add_argument("pasta")
    b.add_argument("consulta")
    b.add_argument("--modo", choices=["todas", "qualquer", "frase"], default="todas")
    b.add_argument("--max", type=int, default=50)
    args = ap.parse_args(argv)

    if args.cmd == "indexar":
        ocr = None
        if args.ocr:
            td, idi = detectar_tesseract()
            if td:
                ocr = {"tessdata": td, "idiomas": idi, "dpi": 300}
            else:
                print("Aviso:", idi, file=sys.stderr)
        r = indexar(args.pasta, ocr=ocr, processos=args.processos, log=print,
                    progresso=lambda f, t, c: print(f"\r{f}/{t}", end="", file=sys.stderr))
        print()
        print(r)
        return 0

    con = banco.conectar(args.pasta, somente_leitura=True)
    res, resumo = buscar(con, interpretar(args.consulta, args.modo))
    print(f"{resumo.ocorrencias} ocorrências em {resumo.paginas} páginas de "
          f"{resumo.documentos} documentos")
    for r in res[: args.max]:
        print(f"{r.caminho} | p.{r.pagina} | {r.oc.antes}[{r.oc.trecho}]{r.oc.depois}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
