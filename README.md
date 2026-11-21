# Indexador de PDFs

Programa para Windows que indexa todos os PDFs de uma pasta (e subpastas) e permite
busca textual instantânea em todo o acervo, exibindo cada ocorrência com o trecho de
contexto e a **pré-visualização da página com o termo destacado**.

Autor: Artur Lira dos Santos · Licença MIT

## Recursos

- **Indexação recursiva e incremental**: na primeira vez lê tudo; depois, só processa
  PDFs novos ou alterados (tamanho/data) e retira do índice os excluídos.
- **Paralela**: vários processos de leitura (ajustável na tela). Referência medida:
  4.500 PDFs / 29 mil páginas em ~7 s numa máquina de 4 núcleos (PDFs com texto).
- **Busca full-text (SQLite FTS5)**: sem diferenciar maiúsculas e acentos
  (`acao` encontra "Ação"); buscas típicas respondem em milissegundos.
- **Cada ocorrência numa linha**: documento, página e trecho com o termo destacado.
- **Pré-visualização**: renderiza a página, destaca todas as ocorrências (amarelo) e a
  selecionada (laranja), com zoom, navegação de páginas, F3/Shift+F3 entre ocorrências.
- **Seleção e cópia de texto na prévia**: arraste com o botão esquerdo sobre a página
  para copiar o texto da área (funciona também em páginas com OCR). O botão direito
  oferece "copiar com referência" (arquivo, página e SHA-256) e "copiar a página inteira".
  Para mover a página, arraste com o botão do meio ou com Shift + botão esquerdo.
- **Ctrl+C na lista de resultados** copia o trecho com arquivo, página e SHA-256.
- **OCR opcional (Tesseract)** para páginas escaneadas sem camada de texto.
- **SHA-256** de cada arquivo registrado no índice e exibido na prévia.
- **Exportação** dos resultados e do inventário do acervo (caminho, tamanho, SHA-256,
  páginas, situação) em CSV (abre direto no Excel).

## Integridade dos arquivos de origem

- Os PDFs são abertos **somente para leitura**; o programa nunca grava, renomeia ou move
  nada na pasta indexada.
- O SHA-256 é calculado sobre os mesmos bytes que são lidos para extração do texto.
- O índice fica **fora** da pasta indexada, em
  `%LOCALAPPDATA%\IndexadorPDF\indices\` (um arquivo `.sqlite` por pasta).
- As exportações CSV são recusadas se o destino estiver dentro da pasta indexada.
- Ao abrir um resultado, o programa avisa se o arquivo mudou depois da indexação.
- Observação: a simples leitura pode atualizar a "data do último acesso" do arquivo,
  conforme a configuração do Windows (por padrão o NTFS não atualiza esse atributo
  a cada leitura). Para exames formais, trabalhe sobre cópia de trabalho com hash conferido.

## Como buscar

| Digite | Encontra |
|---|---|
| `arma fogo` | páginas com as duas palavras (modo "Todas as palavras") |
| `"arma de fogo"` | a expressão exata |
| `balist*` | palavras que começam com "balist" (balística, balístico…) |
| `projétil -estojo` | páginas com "projétil" e sem "estojo" |
| `123.456.789-00` | CPF, placa, nº de processo etc., como escrito |

Também há os modos "Qualquer palavra" e "Frase exata", filtro por nome/pasta do arquivo
e ordenação por arquivo ou relevância. Listas muito grandes são limitadas às 20.000
primeiras ocorrências (o total de páginas/documentos continua sendo informado).

## OCR (opcional)

1. Instale o Tesseract para Windows (instalador da UB Mannheim) marcando o idioma
   **Portuguese**; **ou** crie uma pasta `tessdata` ao lado do `IndexadorPDF.exe` e
   coloque nela o arquivo `por.traineddata`.
2. Marque **OCR em páginas escaneadas** e clique em **Indexar / Atualizar**.
   Só as páginas sem texto passam por OCR (300 dpi); nos documentos já indexados, só os
   que tinham páginas sem texto são reprocessados.

Menu **Ajuda › OCR** mostra se o Tesseract foi encontrado.

## Gerar o executável (.exe)

Em um Windows com Python 3.10+ (64 bits):

```bat
build_exe.bat
```

O resultado é a pasta `dist\IndexadorPDF\` (copie-a inteira; execute `IndexadorPDF.exe`).
O repositório também traz um fluxo do GitHub Actions (`.github/workflows/build-windows.yml`)
que gera o `.zip` do executável automaticamente ao publicar uma tag `v*`.

## Executar a partir do código-fonte

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Linha de comando (lotes/testes): `python main.py --cli indexar "D:\Acervo" --ocr` e
`python main.py --cli buscar "D:\Acervo" "termo" --modo frase`.

## Estrutura

| Arquivo | Função |
|---|---|
| `indexador/extracao.py` | leitura do PDF, SHA-256, OCR (roda em processos separados) |
| `indexador/indexacao.py` | varredura da pasta, indexação incremental e paralela |
| `indexador/banco.py` | esquema SQLite/FTS5 e local do índice |
| `indexador/texto.py` | normalização, interpretação da consulta, localização das ocorrências |
| `indexador/busca.py` | execução das buscas |
| `indexador/gui.py` | interface (PySide6) |
| `tests/` | testes automatizados (`pytest`) |

## Limitações conhecidas

- PDFs protegidos por senha de abertura não são indexados (aparecem em
  **Arquivo › Situação do índice**).
- A posição do destaque em palavras com pontuação interna (ex.: CPF) é aproximada
  proporcionalmente à largura da palavra.
- Caminhos com mais de 260 caracteres dependem de o suporte a caminhos longos estar
  habilitado no Windows.
