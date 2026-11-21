"""Ponto de entrada do Indexador de PDFs."""

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()  # obrigatório no .exe (processos de extração)
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":
        from indexador.cli import main as cli
        return cli(sys.argv[2:])
    from indexador.gui import executar
    return executar()


if __name__ == "__main__":
    sys.exit(main())
