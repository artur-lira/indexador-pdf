@echo off
REM Gera o executavel do Indexador de PDFs para Windows.
REM Requisito: Python 3.10+ (64 bits) instalado e no PATH.
setlocal
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv || goto :erro
)
call .venv\Scripts\activate.bat || goto :erro
python -m pip install --upgrade pip || goto :erro
pip install -r requirements-dev.txt || goto :erro
python -m pytest -q tests || goto :erro
pyinstaller --noconfirm --clean IndexadorPDF.spec || goto :erro
echo.
echo Pronto: dist\IndexadorPDF\IndexadorPDF.exe
echo Copie a pasta dist\IndexadorPDF inteira para onde quiser.
echo Para OCR sem instalar o Tesseract, crie dist\IndexadorPDF\tessdata com por.traineddata.
exit /b 0
:erro
echo.
echo FALHA na geracao do executavel.
exit /b 1
