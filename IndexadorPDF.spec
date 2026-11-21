# -*- mode: python ; coding: utf-8 -*-
# Gera dist\IndexadorPDF\IndexadorPDF.exe (modo pasta: inicia mais rápido que "onefile"
# e os processos de extração não precisam descompactar o programa a cada vez).

EXCLUIR = [
    "tkinter", "unittest", "pydoc", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
    "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtPdf", "PySide6.QtSql",
    "PySide6.QtOpenGL", "PySide6.QtSvg", "pymupdf4llm", "pymupdf.layout", "numpy", "PIL", "lxml", "pandas", "matplotlib",
    "IPython", "networkx", "tabulate", "psutil",
]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("LICENSE", ".")],
    hiddenimports=[],
    excludes=EXCLUIR,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="IndexadorPDF",
    console=False,
    icon=None,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="IndexadorPDF")
