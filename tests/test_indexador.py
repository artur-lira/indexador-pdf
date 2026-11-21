"""Testes automatizados (pytest)."""

import hashlib
import os

import pymupdf
import pytest

from indexador import banco
from indexador.busca import buscar
from indexador.indexacao import indexar
from indexador.texto import dobrar, interpretar, localizar, ocorrencias_no_texto, tokenizar


@pytest.fixture(autouse=True)
def dados_isolados(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "dados"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "dados"))


def _pdf(caminho, *paginas):
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    d = pymupdf.open()
    for t in paginas:
        d.new_page().insert_textbox(pymupdf.Rect(50, 50, 550, 800), t, fontsize=11)
    d.save(caminho)


def _hashes(raiz):
    out = {}
    for b, _d, fs in os.walk(raiz):
        for f in fs:
            p = os.path.join(b, f)
            out[p] = (hashlib.sha256(open(p, "rb").read()).hexdigest(), os.stat(p).st_mtime_ns)
    return out


def test_dobrar_preserva_comprimento():
    s = "AÇÃO Pericial – Projétil ﬁnal"
    assert len(dobrar(s)) == len(s)
    assert dobrar("AÇÃO") == "acao"


def test_tokens_cpf():
    assert tokenizar("CPF 123.456.789-00") == ["cpf", "123", "456", "789", "00"]


def test_consulta_fts():
    c = interpretar('"arma de fogo" balist* -estojo')
    assert c.fts() == '"arma de fogo" AND "balist"* NOT "estojo"'
    assert interpretar("a b", "qualquer").fts() == '"a" OR "b"'
    assert interpretar("123.456.789-00", "frase").fts() == '"123 456 789 00"'


def test_localizar_e_trecho():
    texto = "O projétil foi encontrado. Outro Projetil, e balística do PROJÉTIL."
    oc = ocorrencias_no_texto(texto, interpretar("projetil"))
    assert [o.trecho for o in oc] == ["projétil", "Projetil", "PROJÉTIL"]
    assert localizar(tokenizar("arma de fogo e arma"), interpretar('"arma de fogo"')) == [(0, 3)]


def test_indexacao_incremental_e_integridade(tmp_path):
    raiz = tmp_path / "acervo"
    _pdf(str(raiz / "a" / "laudo1.pdf"), "Exame de local de crime com vestígio de sangue.",
         "Projétil calibre .38 CPF 123.456.789-00")
    _pdf(str(raiz / "b" / "c" / "laudo2.PDF"), "Ação penal e balística forense.")
    (raiz / "ruim.pdf").write_bytes(b"%PDF-1.4 corrompido")
    antes = _hashes(raiz)

    r = indexar(str(raiz), processos=2)
    assert (r.novos, r.indexados, r.com_erro) == (3, 3, 1)
    assert _hashes(raiz) == antes, "arquivos de origem não podem ser alterados"
    assert not any(n.endswith((".sqlite", "-wal", "-shm")) for n in os.listdir(raiz))

    con = banco.conectar(str(raiz), somente_leitura=True)
    res, s = buscar(con, interpretar("123.456.789-00"))
    assert s.ocorrencias == 1 and res[0].pagina == 2
    res, _ = buscar(con, interpretar("acao penal", "frase"))
    assert res[0].oc.trecho == "Ação penal"
    sha = con.execute("SELECT sha256 FROM documentos WHERE caminho LIKE '%laudo1.pdf'").fetchone()[0]
    assert sha == hashlib.sha256((raiz / "a" / "laudo1.pdf").read_bytes()).hexdigest()
    con.close()

    # nada mudou -> nada reprocessado
    r = indexar(str(raiz))
    assert (r.inalterados, r.indexados) == (3, 0)

    # alteração e remoção
    _pdf(str(raiz / "b" / "c" / "laudo2.PDF"), "Texto novo: papiloscopia.")
    os.remove(raiz / "ruim.pdf")
    r = indexar(str(raiz))
    assert (r.alterados, r.removidos) == (1, 1)
    con = banco.conectar(str(raiz), somente_leitura=True)
    assert buscar(con, interpretar("balistica"))[1].ocorrencias == 0
    assert buscar(con, interpretar("papiloscopia"))[1].ocorrencias == 1
    con.close()
