"""Interface gráfica (PySide6)."""

from __future__ import annotations

import csv
import html
import os
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pymupdf
from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QRectF, QSettings, Qt,
                            QThread, QTimer, Signal)
from PySide6.QtGui import (QAction, QBrush, QColor, QGuiApplication, QImage, QKeySequence,
                           QPainter, QPen, QPixmap, QTextDocument)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QGraphicsPixmapItem,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
                               QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableView,
                               QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget, QMenu)

from . import NOME_APP, __autor__, __version__, banco
from .busca import Resultado, ResumoBusca, buscar
from .extracao import FLAGS_TEXTO, detectar_tesseract
from .indexacao import ResumoIndexacao, indexar
from .texto import (MODO_FRASE, MODO_QUALQUER, MODO_TODAS, Consulta, interpretar, localizar,
                    texto_na_area, tokens_de_palavras)

pymupdf.TOOLS.mupdf_display_errors(False)

COR_TODAS = QColor(255, 230, 0, 110)
COR_ATUAL = QColor(255, 120, 0, 150)
COR_BORDA_ATUAL = QColor(220, 30, 0)


def _fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


# ==========================================================================
# Trabalhadores em segundo plano
# ==========================================================================

class TrabalhadorIndexacao(QThread):
    progresso = Signal(int, int, str)
    mensagem = Signal(str)
    terminado = Signal(object)

    def __init__(self, raiz: str, ocr: dict | None, processos: int):
        super().__init__()
        self.raiz, self.ocr, self.processos = raiz, ocr, processos
        self.cancelar = threading.Event()

    def run(self):
        try:
            r = indexar(self.raiz, ocr=self.ocr, processos=self.processos,
                        progresso=lambda a, b, c: self.progresso.emit(a, b, c),
                        log=lambda s: self.mensagem.emit(s), cancelar=self.cancelar)
        except Exception as e:  # pragma: no cover
            self.mensagem.emit(f"ERRO na indexação: {type(e).__name__}: {e}")
            r = None
        self.terminado.emit(r)


class TrabalhadorBusca(QThread):
    pronto = Signal(int, list, object, float, str)

    def __init__(self, ident: int, raiz: str, consulta: Consulta, filtro: str, ordem: str):
        super().__init__()
        self.ident, self.raiz, self.consulta = ident, raiz, consulta
        self.filtro, self.ordem = filtro, ordem
        self.cancelar = threading.Event()

    def run(self):
        t0 = time.perf_counter()
        try:
            con = banco.conectar(self.raiz, somente_leitura=True)
            try:
                res, resumo = buscar(con, self.consulta, self.filtro, self.ordem,
                                     cancelar=self.cancelar)
            finally:
                con.close()
            erro = ""
        except Exception as e:
            res, resumo, erro = [], ResumoBusca(), f"{type(e).__name__}: {e}"
        self.pronto.emit(self.ident, res, resumo, time.perf_counter() - t0, erro)


# ==========================================================================
# Tabela de resultados
# ==========================================================================

class ModeloResultados(QAbstractTableModel):
    COLS = ("Documento", "Pág.", "Trecho")

    def __init__(self):
        super().__init__()
        self.itens: list[Resultado] = []
        self._html: dict[int, str] = {}

    def definir(self, itens: list[Resultado]):
        self.beginResetModel()
        self.itens = itens
        self._html.clear()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.itens)

    def columnCount(self, parent=QModelIndex()):
        return 3

    def headerData(self, s, o, role=Qt.DisplayRole):
        if o == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLS[s]
        return None

    def html_trecho(self, row: int, corte: int | None = None) -> str:
        """HTML do trecho; ``corte`` limita o contexto anterior (em caracteres) para
        que o termo destacado fique visível em colunas estreitas."""
        if corte is None:
            h = self._html.get(row)
            if h is not None:
                return h
        r = self.itens[row]
        antes = r.oc.antes
        if corte is not None and len(antes) > corte:
            antes = "…" + antes[-corte:].lstrip()
        h = (html.escape(antes) + "<span style='background:#ffd84d;color:#000;font-weight:600'>"
             + html.escape(r.oc.trecho) + "</span>" + html.escape(r.oc.depois))
        if r.origem == "ocr":
            h = "<span style='color:#a05a00'>[OCR]</span> " + h
        if corte is None:
            self._html[row] = h
        return h

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        r = self.itens[idx.row()]
        c = idx.column()
        if role == Qt.DisplayRole:
            if c == 0:
                return os.path.basename(r.caminho)
            if c == 1:
                return r.pagina
            return r.oc.antes + r.oc.trecho + r.oc.depois
        if role == Qt.ToolTipRole:
            if c == 0:
                return r.caminho
            if c == 2:
                return "<qt>" + self.html_trecho(idx.row()) + "</qt>"
        if role == Qt.TextAlignmentRole and c == 1:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class DelegadoTrecho(QStyledItemDelegate):
    """Desenha o trecho com o termo destacado (HTML), em uma linha."""

    def __init__(self, modelo: ModeloResultados, parent=None):
        super().__init__(parent)
        self.modelo = modelo
        self.doc = QTextDocument()
        self.doc.setDocumentMargin(2)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        self.doc.setDefaultFont(opt.font)
        cor = opt.palette.highlightedText().color() if opt.state & QStyle.State_Selected \
            else opt.palette.text().color()
        media = max(1, opt.fontMetrics.averageCharWidth())
        corte = max(12, int(opt.rect.width() / media * 0.3))
        self.doc.setHtml(f"<span style='color:{cor.name()};white-space:nowrap'>"
                         + self.modelo.html_trecho(index.row(), corte) + "</span>")
        painter.save()
        painter.setClipRect(opt.rect)
        y = opt.rect.top() + (opt.rect.height() - self.doc.size().height()) / 2
        painter.translate(opt.rect.left(), y)
        self.doc.drawContents(painter)
        painter.restore()


# ==========================================================================
# Pré-visualização da página
# ==========================================================================

class Visualizador(QGraphicsView):
    """Botão esquerdo arrastado: seleciona uma área de texto.
    Botão do meio (ou Shift + esquerdo) arrastado: move a página."""
    zoomPedido = Signal(float)
    areaSelecionada = Signal(QRectF)
    menuPedido = Signal(object)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setBackgroundBrush(QColor(90, 90, 90))
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.viewport().setCursor(Qt.IBeamCursor)
        self._ini_sel = None
        self._item_sel: QGraphicsRectItem | None = None
        self._ini_pan = None

    def limpar_selecao(self):
        if self._item_sel is not None and self._item_sel.scene() is self.scene():
            self.scene().removeItem(self._item_sel)
        self._item_sel = None

    def mousePressEvent(self, ev):
        pan = ev.button() == Qt.MiddleButton or (
            ev.button() == Qt.LeftButton and ev.modifiers() & Qt.ShiftModifier)
        if pan:
            self._ini_pan = ev.position().toPoint()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        if ev.button() == Qt.LeftButton:
            self.limpar_selecao()
            self._ini_sel = self.mapToScene(ev.position().toPoint())
            self._item_sel = QGraphicsRectItem(QRectF(self._ini_sel, self._ini_sel))
            self._item_sel.setBrush(QBrush(QColor(40, 120, 255, 45)))
            self._item_sel.setPen(QPen(QColor(40, 120, 255), 1, Qt.DashLine))
            self._item_sel.setZValue(10)
            self.scene().addItem(self._item_sel)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._ini_pan is not None:
            d = ev.position().toPoint() - self._ini_pan
            self._ini_pan = ev.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            return
        if self._ini_sel is not None and self._item_sel is not None:
            p = self.mapToScene(ev.position().toPoint())
            self._item_sel.setRect(QRectF(self._ini_sel, p).normalized())
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._ini_pan is not None:
            self._ini_pan = None
            self.viewport().setCursor(Qt.IBeamCursor)
            return
        if self._ini_sel is not None and self._item_sel is not None:
            self._ini_sel = None
            r = self._item_sel.rect()
            if r.width() < 3 and r.height() < 3:
                self.limpar_selecao()
            else:
                self.areaSelecionada.emit(r)
            return
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev):
        self.menuPedido.emit(ev.globalPos())

    def wheelEvent(self, ev):
        if ev.modifiers() & Qt.ControlModifier:
            self.zoomPedido.emit(1.15 if ev.angleDelta().y() > 0 else 1 / 1.15)
            ev.accept()
        else:
            super().wheelEvent(ev)


class PainelPrevia(QWidget):
    def __init__(self, janela: "JanelaPrincipal"):
        super().__init__()
        self.janela = janela
        self.docs: OrderedDict[str, pymupdf.Document] = OrderedDict()
        self.raiz: str | None = None
        self.res: Resultado | None = None
        self.consulta: Consulta | None = None
        self.pagina = 1          # página exibida (pode diferir da do resultado)
        self.zoom = None         # None = ajustar à largura
        self.ultimo_zoom = 1.0
        self.palavras_ocr_cache: dict[int, list] = {}

        self.cab = QLabel("Selecione um resultado para visualizar a página.")
        self.cab.setWordWrap(True)
        self.cab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.aviso = QLabel()
        self.aviso.setStyleSheet("color:#b00000;font-weight:bold")
        self.aviso.hide()

        def bt(txt, dica, fn, atalho=None):
            b = QToolButton()
            b.setText(txt)
            b.setToolTip(dica + (f" ({atalho})" if atalho else ""))
            b.clicked.connect(fn)
            if atalho:
                b.setShortcut(QKeySequence(atalho))
            return b

        self.lbl_pag = QLabel("")
        barra = QHBoxLayout()
        barra.addWidget(bt("◀", "Página anterior", lambda: self.ir_pagina(self.pagina - 1), "PgUp"))
        barra.addWidget(self.lbl_pag)
        barra.addWidget(bt("▶", "Próxima página", lambda: self.ir_pagina(self.pagina + 1), "PgDown"))
        barra.addSpacing(12)
        barra.addWidget(bt("−", "Diminuir zoom", lambda: self.mudar_zoom(1 / 1.25), "Ctrl+-"))
        barra.addWidget(bt("+", "Aumentar zoom", lambda: self.mudar_zoom(1.25), "Ctrl+="))
        barra.addWidget(bt("↔", "Ajustar à largura", self.ajustar_largura, "Ctrl+0"))
        barra.addStretch(1)
        barra.addWidget(bt("Abrir PDF", "Abrir no leitor de PDF padrão do Windows", self.abrir_pdf))
        barra.addWidget(bt("Mostrar na pasta", "Abrir o Explorador com o arquivo selecionado",
                           self.mostrar_na_pasta))
        barra.addWidget(bt("Copiar caminho", "Copiar o caminho completo do arquivo",
                           self.copiar_caminho))
        barra.addWidget(bt("Copiar imagem", "Copiar a imagem da página (com destaques)",
                           self.copiar_imagem))

        self.vis = Visualizador()
        self.vis.zoomPedido.connect(self.mudar_zoom)
        self.vis.areaSelecionada.connect(self.copiar_area)
        self.vis.menuPedido.connect(self.menu_previa)
        self.vis.setToolTip("Arraste com o botão esquerdo para selecionar e copiar texto.\n"
                            "Arraste com o botão do meio (ou Shift + esquerdo) para mover a página.\n"
                            "Ctrl + roda do mouse: zoom.")
        self.texto_selecionado = ""
        self._mat_inv = None
        self._palavras_pag = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.addWidget(self.cab)
        lay.addWidget(self.aviso)
        lay.addLayout(barra)
        lay.addWidget(self.vis, 1)

    # ---- documentos abertos (somente leitura) ----
    def fechar_todos(self):
        for d in self.docs.values():
            try:
                d.close()
            except Exception:
                pass
        self.docs.clear()
        self.palavras_ocr_cache.clear()

    def _abrir(self, caminho_abs: str) -> pymupdf.Document:
        d = self.docs.get(caminho_abs)
        if d is not None:
            self.docs.move_to_end(caminho_abs)
            return d
        d = pymupdf.open(caminho_abs, filetype="pdf")  # abertura somente leitura
        self.docs[caminho_abs] = d
        while len(self.docs) > 4:
            _, velho = self.docs.popitem(last=False)
            velho.close()
        return d

    def caminho_abs(self) -> str | None:
        if not (self.res and self.raiz):
            return None
        return os.path.join(self.raiz, self.res.caminho)

    # ---- exibição ----
    def mostrar(self, raiz: str, res: Resultado, consulta: Consulta):
        mesmo_doc = self.res is not None and self.res.doc_id == res.doc_id and self.raiz == raiz
        self.raiz, self.res, self.consulta = raiz, res, consulta
        self.pagina = res.pagina
        if not mesmo_doc:
            self._verificar_integridade()
        self.renderizar(atual=res.oc.indice)

    def _verificar_integridade(self):
        self.aviso.hide()
        p = self.caminho_abs()
        info = self.janela.info_documento(self.res.doc_id)
        if not info:
            return
        try:
            st = os.stat(p)
            if st.st_size != info["tamanho"] or st.st_mtime_ns != info["mtime_ns"]:
                self.aviso.setText("Atenção: o arquivo foi modificado após a indexação. "
                                   "Atualize o índice antes de usar este resultado.")
                self.aviso.show()
        except OSError:
            self.aviso.setText("Atenção: o arquivo não está mais acessível neste caminho.")
            self.aviso.show()

    def ir_pagina(self, n: int):
        if not self.res:
            return
        try:
            total = self._abrir(self.caminho_abs()).page_count
        except Exception:
            return
        if 1 <= n <= total:
            self.pagina = n
            self.renderizar(atual=self.res.oc.indice if n == self.res.pagina else None)

    def mudar_zoom(self, fator: float):
        if not self.res:
            return
        base = self.zoom if self.zoom is not None else self.ultimo_zoom
        self.zoom = max(0.25, min(8.0, base * fator))
        self.renderizar(atual=self.res.oc.indice if self.pagina == self.res.pagina else None,
                        manter_rolagem=True)

    def ajustar_largura(self):
        self.zoom = None
        if self.res:
            self.renderizar(atual=self.res.oc.indice if self.pagina == self.res.pagina else None)

    def _caixas_palavras(self, page: pymupdf.Page) -> list:
        """Palavras da página exibida: do PDF ou, em páginas de OCR, do índice."""
        if self.pagina == self.res.pagina and self.res.origem == "ocr":
            pid = self.res.pagina_id
        else:
            pid = self.janela.id_pagina_ocr(self.res.doc_id, self.pagina)
        if pid:
            if pid not in self.palavras_ocr_cache:
                self.palavras_ocr_cache[pid] = self.janela.palavras_ocr(pid) or []
            return self.palavras_ocr_cache[pid]
        return page.get_text("words", flags=FLAGS_TEXTO)

    def renderizar(self, atual: int | None = None, manter_rolagem: bool = False):
        cena = self.vis.scene()
        hs, vs = self.vis.horizontalScrollBar().value(), self.vis.verticalScrollBar().value()
        self.vis.limpar_selecao()
        cena.clear()
        self.texto_selecionado = ""
        self._mat_inv = None
        self._palavras_pag = None
        p = self.caminho_abs()
        try:
            doc = self._abrir(p)
            page = doc.load_page(self.pagina - 1)
        except Exception as e:
            self.cab.setText(f"<b>Não foi possível abrir</b> {html.escape(p or '')}<br>{e}")
            return

        if self.zoom is None:
            vp = self.vis.viewport().width()
            if not self.vis.verticalScrollBar().isVisible():
                vp -= self.vis.verticalScrollBar().sizeHint().width()
            larg = max(200, vp - 16)
            z = larg / max(1.0, page.rect.width)
        else:
            z = self.zoom
        self.ultimo_zoom = z
        dpr = self.devicePixelRatioF()
        pix = page.get_pixmap(matrix=pymupdf.Matrix(z * dpr, z * dpr), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        qpix = QPixmap.fromImage(img)
        qpix.setDevicePixelRatio(dpr)
        cena.addItem(QGraphicsPixmapItem(qpix))
        cena.setSceneRect(QRectF(0, 0, pix.width / dpr, pix.height / dpr))

        # destaques
        alvo_rect = None
        n_hits = 0
        try:
            mat = page.rotation_matrix * pymupdf.Matrix(z, z)
            self._mat_inv = ~mat
            palavras = self._caixas_palavras(page)
            self._palavras_pag = palavras
            toks, caixas = tokens_de_palavras(palavras)
            hits = localizar(toks, self.consulta)
            n_hits = len(hits)
            for k, (i, j) in enumerate(hits):
                cur = (k == atual)
                for linha in _juntar_por_linha(caixas[i:j]):
                    r = pymupdf.Rect(linha) * mat
                    qr = QRectF(r.x0 - 1, r.y0 - 1, r.width + 2, r.height + 2)
                    item = QGraphicsRectItem(qr)
                    item.setBrush(QBrush(COR_ATUAL if cur else COR_TODAS))
                    item.setPen(QPen(COR_BORDA_ATUAL, 1.5) if cur else QPen(Qt.NoPen))
                    cena.addItem(item)
                    if cur and alvo_rect is None:
                        alvo_rect = qr
        except Exception:
            pass

        info = self.janela.info_documento(self.res.doc_id) or {}
        origem = " · texto obtido por OCR" if (self.pagina == self.res.pagina
                                               and self.res.origem == "ocr") else ""
        self.cab.setText(
            f"<b>{html.escape(os.path.basename(p))}</b><br>"
            f"<span style='color:#555'>{html.escape(p)}</span><br>"
            f"<span style='color:#555'>SHA-256 (na indexação): "
            f"<code>{info.get('sha256') or '—'}</code></span>")
        self.lbl_pag.setText(f" Página {self.pagina} de {doc.page_count} · "
                             f"{n_hits} ocorrência(s) nesta página{origem} ")
        if manter_rolagem:
            self.vis.horizontalScrollBar().setValue(hs)
            self.vis.verticalScrollBar().setValue(vs)
        elif alvo_rect is not None:
            self.vis.centerOn(alvo_rect.center())
        else:
            self.vis.verticalScrollBar().setValue(0)

    # ---- ações ----
    def abrir_pdf(self):
        p = self.caminho_abs()
        if not p:
            return
        try:
            if sys.platform == "win32":
                os.startfile(p)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
        except Exception as e:
            QMessageBox.warning(self, NOME_APP, f"Não foi possível abrir o arquivo:\n{e}")

    def mostrar_na_pasta(self):
        p = self.caminho_abs()
        if not p:
            return
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(p)])

    def copiar_caminho(self):
        p = self.caminho_abs()
        if p:
            QGuiApplication.clipboard().setText(
                f"{p} (página {self.pagina})")
            self.janela.statusBar().showMessage("Caminho copiado.", 3000)

    def copiar_area(self, r: QRectF):
        """Copia para a área de transferência o texto da área selecionada."""
        if self._mat_inv is None or self._palavras_pag is None:
            return
        a = pymupdf.Rect(r.left(), r.top(), r.right(), r.bottom()) * self._mat_inv
        a.normalize()
        self.texto_selecionado = texto_na_area(self._palavras_pag, (a.x0, a.y0, a.x1, a.y1))
        if self.texto_selecionado:
            QGuiApplication.clipboard().setText(self.texto_selecionado)
            n = len(self.texto_selecionado.split())
            self.janela.statusBar().showMessage(
                f"Texto copiado ({n} palavra(s)). Botão direito: copiar com referência.", 6000)
        else:
            self.janela.statusBar().showMessage("Nenhum texto na área selecionada.", 4000)

    def _referencia(self) -> str:
        p = self.caminho_abs() or ""
        info = (self.janela.info_documento(self.res.doc_id) if self.res else None) or {}
        ref = f"(Fonte: {p}, página {self.pagina}"
        if info.get("sha256"):
            ref += f", SHA-256 {info['sha256']}"
        return ref + ")"

    def menu_previa(self, pos):
        if not self.res:
            return
        m = QMenu(self)
        a1 = m.addAction("Copiar texto selecionado")
        a2 = m.addAction("Copiar texto selecionado com referência (arquivo, página, hash)")
        a3 = m.addAction("Copiar texto da página inteira")
        m.addSeparator()
        a4 = m.addAction("Copiar imagem da página")
        a1.setEnabled(bool(self.texto_selecionado))
        a2.setEnabled(bool(self.texto_selecionado))
        esc = m.exec(pos)
        if esc is a1:
            QGuiApplication.clipboard().setText(self.texto_selecionado)
        elif esc is a2:
            QGuiApplication.clipboard().setText(
                f"\u201c{self.texto_selecionado}\u201d\n{self._referencia()}")
        elif esc is a3 and self._palavras_pag is not None:
            QGuiApplication.clipboard().setText(
                texto_na_area(self._palavras_pag, (-1e9, -1e9, 1e9, 1e9)))
        elif esc is a4:
            self.copiar_imagem()
        if esc in (a1, a2, a3):
            self.janela.statusBar().showMessage("Texto copiado.", 3000)

    def copiar_imagem(self):
        cena = self.vis.scene()
        if not self.res or cena.sceneRect().isEmpty():
            return
        self.vis.limpar_selecao()
        r = cena.sceneRect()
        dpr = self.devicePixelRatioF()
        img = QImage(int(r.width() * dpr), int(r.height() * dpr), QImage.Format_RGB32)
        img.fill(Qt.white)
        pt = QPainter(img)
        cena.render(pt, QRectF(0, 0, img.width(), img.height()), r)
        pt.end()
        QGuiApplication.clipboard().setImage(img)
        self.janela.statusBar().showMessage("Imagem da página copiada.", 3000)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        if self.res and self.zoom is None:
            if not hasattr(self, "_t_resize"):
                self._t_resize = QTimer(self, singleShot=True, interval=150)
                self._t_resize.timeout.connect(
                    lambda: self.res and self.renderizar(
                        atual=self.res.oc.indice if self.pagina == self.res.pagina else None))
            self._t_resize.start()


def _juntar_por_linha(caixas):
    """Agrupa caixas de tokens consecutivos que estão na mesma linha."""
    linhas = []
    for c in caixas:
        if linhas:
            u = linhas[-1]
            if abs(u[1] - c[1]) < 2 and abs(u[3] - c[3]) < 2 and c[0] >= u[0] - 1:
                linhas[-1] = (u[0], min(u[1], c[1]), max(u[2], c[2]), max(u[3], c[3]))
                continue
        linhas.append(tuple(c))
    return linhas


# ==========================================================================
# Diálogo de situação do índice
# ==========================================================================

class DialogoSituacao(QDialog):
    def __init__(self, parent, raiz: str):
        super().__init__(parent)
        self.raiz = raiz
        self.setWindowTitle("Situação do índice")
        self.resize(900, 520)
        con = banco.conectar(raiz, somente_leitura=True)
        try:
            est = banco.estatisticas(con)
            ult = con.execute("SELECT valor FROM meta WHERE chave='ultima_indexacao'").fetchone()
            linhas = con.execute(
                "SELECT caminho, status, paginas, paginas_vazias, paginas_ocr, COALESCE(erro,'') "
                "FROM documentos WHERE status<>'ok' OR paginas_vazias>0 OR erro IS NOT NULL "
                "ORDER BY status DESC, caminho").fetchall()
        finally:
            con.close()
        txt = (f"<b>Pasta:</b> {html.escape(raiz)}<br>"
               f"<b>Arquivo do índice:</b> {html.escape(str(banco.arquivo_indice(raiz)))}<br>"
               f"<b>Última indexação:</b> {ult[0] if ult else '—'}<br>"
               f"<b>Documentos:</b> {_fmt_int(est['documentos'])} · <b>Páginas:</b> "
               f"{_fmt_int(est['paginas'])} · <b>Páginas por OCR:</b> {_fmt_int(est['paginas_ocr'])}"
               f" · <b>Páginas sem texto:</b> {_fmt_int(est['paginas_vazias'])} · "
               f"<b>Com erro/protegidos:</b> {_fmt_int(est['com_problema'])}")
        lbl = QLabel(txt)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setWordWrap(True)
        tab = QTableWidget(len(linhas), 6)
        tab.setHorizontalHeaderLabels(["Documento", "Situação", "Páginas", "Sem texto", "OCR",
                                       "Observação"])
        for i, l in enumerate(linhas):
            for j, v in enumerate(l):
                tab.setItem(i, j, QTableWidgetItem(str(v)))
        tab.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        tab.horizontalHeader().setStretchLastSection(True)
        tab.setColumnWidth(0, 380)
        tab.setEditTriggers(QAbstractItemView.NoEditTriggers)
        bts = QDialogButtonBox(QDialogButtonBox.Close)
        b_inv = bts.addButton("Exportar inventário (CSV)…", QDialogButtonBox.ActionRole)
        b_inv.clicked.connect(self.exportar_inventario)
        bts.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(lbl)
        lay.addWidget(QLabel("Documentos que merecem atenção (erro, senha ou páginas sem texto):"))
        lay.addWidget(tab, 1)
        lay.addWidget(bts)

    def exportar_inventario(self):
        destino, _ = QFileDialog.getSaveFileName(self, "Exportar inventário",
                                                 str(Path.home() / "inventario_pdfs.csv"),
                                                 "CSV (*.csv)")
        if not destino:
            return
        if _dentro_de(destino, self.raiz):
            QMessageBox.warning(self, NOME_APP, "Por segurança, salve fora da pasta indexada.")
            return
        con = banco.conectar(self.raiz, somente_leitura=True)
        try:
            with open(destino, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["caminho", "tamanho_bytes", "sha256", "paginas", "paginas_ocr",
                            "paginas_sem_texto", "situacao", "observacao", "indexado_em"])
                for row in con.execute(
                        "SELECT caminho, tamanho, sha256, paginas, paginas_ocr, paginas_vazias,"
                        " status, erro, indexado_em FROM documentos ORDER BY caminho"):
                    w.writerow([os.path.join(self.raiz, row[0]), *row[1:]])
        finally:
            con.close()
        QMessageBox.information(self, NOME_APP, f"Inventário salvo em:\n{destino}")


def _dentro_de(caminho: str, raiz: str) -> bool:
    try:
        a = os.path.normcase(os.path.abspath(caminho))
        b = os.path.normcase(os.path.abspath(raiz))
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


# ==========================================================================
# Janela principal
# ==========================================================================

AJUDA_BUSCA = (
    "Dicas de busca (não diferencia maiúsculas nem acentos):\n"
    "  arma fogo        → páginas com as duas palavras\n"
    "  \"arma de fogo\"   → expressão exata\n"
    "  balist*          → palavras que começam com \"balist\"\n"
    "  projétil -estojo → com projétil e sem estojo\n"
    "  CPF, placas e números (ex.: 123.456.789-00) podem ser digitados como estão.")


class JanelaPrincipal(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{NOME_APP} {__version__}")
        self.resize(1400, 860)
        self.cfg = QSettings("ArturLira", "IndexadorPDF")
        self.raiz: str | None = None
        self.con: "banco.sqlite3.Connection | None" = None
        self.trab_idx: TrabalhadorIndexacao | None = None
        self.trab_busca: TrabalhadorBusca | None = None
        self.id_busca = 0
        self.consulta_atual: Consulta | None = None
        self._cache_info: dict[int, dict] = {}

        # ---- linha da pasta ----
        self.cb_pasta = QComboBox()
        self.cb_pasta.setEditable(False)
        self.cb_pasta.setMinimumWidth(420)
        self.cb_pasta.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        for p in self.cfg.value("pastas_recentes", [], type=list) or []:
            self.cb_pasta.addItem(p)
        self.cb_pasta.currentTextChanged.connect(self.definir_raiz)
        b_esc = QPushButton("Escolher pasta…")
        b_esc.clicked.connect(self.escolher_pasta)
        self.b_idx = QPushButton("Indexar / Atualizar")
        self.b_idx.setToolTip("Indexa PDFs novos ou alterados e remove do índice os excluídos.\n"
                              "Os arquivos originais nunca são alterados.")
        self.b_idx.clicked.connect(self.iniciar_indexacao)
        self.b_cancel = QPushButton("Cancelar")
        self.b_cancel.setEnabled(False)
        self.b_cancel.clicked.connect(self.cancelar_indexacao)
        self.ck_ocr = QCheckBox("OCR em páginas escaneadas")
        self.ck_ocr.setChecked(self.cfg.value("ocr", False, type=bool))
        self.ck_ocr.toggled.connect(lambda v: self.cfg.setValue("ocr", v))
        self.sp_proc = QSpinBox()
        self.sp_proc.setRange(1, max(1, os.cpu_count() or 1))
        self.sp_proc.setValue(int(self.cfg.value("processos",
                                                 max(1, min(8, (os.cpu_count() or 2) - 1)))))
        self.sp_proc.setToolTip("Processos simultâneos de leitura de PDF")
        self.sp_proc.valueChanged.connect(lambda v: self.cfg.setValue("processos", v))

        lin1 = QHBoxLayout()
        lin1.addWidget(QLabel("Pasta:"))
        lin1.addWidget(self.cb_pasta, 1)
        lin1.addWidget(b_esc)
        lin1.addWidget(self.b_idx)
        lin1.addWidget(self.b_cancel)
        lin1.addSpacing(10)
        lin1.addWidget(self.ck_ocr)
        lin1.addWidget(QLabel("Processos:"))
        lin1.addWidget(self.sp_proc)

        self.barra = QProgressBar()
        self.barra.setTextVisible(True)
        self.barra.setFormat("")
        self.barra.setMaximumHeight(16)
        self.lbl_idx = QLabel("")

        # ---- linha de busca ----
        self.ed_busca = QLineEdit()
        self.ed_busca.setPlaceholderText("Digite o termo e tecle Enter  (ex.: \"arma de fogo\", "
                                         "balist*, 123.456.789-00)")
        self.ed_busca.setToolTip(AJUDA_BUSCA)
        self.ed_busca.setClearButtonEnabled(True)
        self.ed_busca.returnPressed.connect(self.buscar)
        f = self.ed_busca.font()
        f.setPointSizeF(f.pointSizeF() * 1.15)
        self.ed_busca.setFont(f)
        self.cb_modo = QComboBox()
        self.cb_modo.addItem("Todas as palavras", MODO_TODAS)
        self.cb_modo.addItem("Qualquer palavra", MODO_QUALQUER)
        self.cb_modo.addItem("Frase exata", MODO_FRASE)
        self.cb_modo.currentIndexChanged.connect(lambda _i: self.ed_busca.text() and self.buscar())
        self.ed_filtro = QLineEdit()
        self.ed_filtro.setPlaceholderText("Filtrar por nome/pasta do arquivo")
        self.ed_filtro.setClearButtonEnabled(True)
        self.ed_filtro.setMaximumWidth(260)
        self.ed_filtro.returnPressed.connect(self.buscar)
        self.cb_ordem = QComboBox()
        self.cb_ordem.addItem("Ordenar por arquivo", "caminho")
        self.cb_ordem.addItem("Ordenar por relevância", "relevancia")
        self.cb_ordem.currentIndexChanged.connect(lambda _i: self.ed_busca.text() and self.buscar())
        b_buscar = QPushButton("Buscar")
        b_buscar.setDefault(True)
        b_buscar.clicked.connect(self.buscar)
        lin2 = QHBoxLayout()
        lin2.addWidget(self.ed_busca, 1)
        lin2.addWidget(self.cb_modo)
        lin2.addWidget(self.ed_filtro)
        lin2.addWidget(self.cb_ordem)
        lin2.addWidget(b_buscar)

        # ---- resultados + prévia ----
        self.modelo = ModeloResultados()
        self.tabela = QTableView()
        self.tabela.setModel(self.modelo)
        self.tabela.setItemDelegateForColumn(2, DelegadoTrecho(self.modelo, self.tabela))
        self.tabela.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tabela.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tabela.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tabela.setAlternatingRowColors(True)
        self.tabela.setWordWrap(False)
        self.tabela.verticalHeader().setVisible(False)
        self.tabela.verticalHeader().setDefaultSectionSize(26)
        hh = self.tabela.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        self.tabela.setColumnWidth(0, 220)
        self.tabela.selectionModel().currentRowChanged.connect(self.ao_selecionar)
        a = QAction("Copiar trecho", self.tabela)
        a.setShortcut(QKeySequence.Copy)
        a.setShortcutContext(Qt.WidgetShortcut)
        a.triggered.connect(self.copiar_resultado)
        self.tabela.addAction(a)
        self.tabela.setContextMenuPolicy(Qt.ActionsContextMenu)

        self.previa = PainelPrevia(self)
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.tabela)
        self.split.addWidget(self.previa)
        self.split.setSizes([560, 840])
        st = self.cfg.value("split")
        if st:
            self.split.restoreState(st)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setMaximumHeight(110)
        self.log.setPlaceholderText("Registro da indexação")

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(lin1)
        lin_prog = QHBoxLayout()
        lin_prog.addWidget(self.barra, 1)
        lin_prog.addWidget(self.lbl_idx)
        lay.addLayout(lin_prog)
        lay.addLayout(lin2)
        lay.addWidget(self.split, 1)
        lay.addWidget(self.log)
        self.setCentralWidget(central)

        self._menus()
        self.lbl_status = QLabel("")
        self.statusBar().addPermanentWidget(self.lbl_status)
        geo = self.cfg.value("geometria")
        if geo:
            self.restoreGeometry(geo)

        if self.cb_pasta.count():
            self.definir_raiz(self.cb_pasta.currentText())
        else:
            self.lbl_status.setText("Escolha uma pasta para começar.")

    # ---- menus ----
    def _menus(self):
        m = self.menuBar().addMenu("&Arquivo")
        a = QAction("Escolher pasta…", self, shortcut=QKeySequence("Ctrl+O"))
        a.triggered.connect(self.escolher_pasta)
        m.addAction(a)
        a = QAction("Exportar resultados (CSV)…", self, shortcut=QKeySequence("Ctrl+E"))
        a.triggered.connect(self.exportar_resultados)
        m.addAction(a)
        a = QAction("Situação do índice…", self)
        a.triggered.connect(self.situacao)
        m.addAction(a)
        a = QAction("Reconstruir índice do zero…", self)
        a.triggered.connect(self.reconstruir)
        m.addAction(a)
        m.addSeparator()
        a = QAction("Remover pasta da lista", self)
        a.triggered.connect(self.remover_pasta_lista)
        m.addAction(a)
        m.addSeparator()
        a = QAction("Sair", self, shortcut=QKeySequence("Ctrl+Q"))
        a.triggered.connect(self.close)
        m.addAction(a)
        m = self.menuBar().addMenu("&Navegar")
        a = QAction("Focar a busca", self, shortcut=QKeySequence("Ctrl+F"))
        a.triggered.connect(lambda: (self.ed_busca.setFocus(), self.ed_busca.selectAll()))
        m.addAction(a)
        a = QAction("Próxima ocorrência", self, shortcut=QKeySequence("F3"))
        a.triggered.connect(lambda: self.mover_selecao(1))
        m.addAction(a)
        a = QAction("Ocorrência anterior", self, shortcut=QKeySequence("Shift+F3"))
        a.triggered.connect(lambda: self.mover_selecao(-1))
        m.addAction(a)
        m = self.menuBar().addMenu("A&juda")
        a = QAction("Como buscar", self)
        a.triggered.connect(lambda: QMessageBox.information(self, "Como buscar", AJUDA_BUSCA))
        m.addAction(a)
        a = QAction("OCR (Tesseract)", self)
        a.triggered.connect(self.info_ocr)
        m.addAction(a)
        a = QAction("Sobre", self)
        a.triggered.connect(self.sobre)
        m.addAction(a)

    # ---- pasta / índice ----
    def escolher_pasta(self):
        d = QFileDialog.getExistingDirectory(self, "Escolha a pasta com os PDFs",
                                             self.raiz or str(Path.home()))
        if not d:
            return
        d = banco.normalizar_raiz(d)
        self._adicionar_recente(d)
        if not banco.indice_existe(d):
            if QMessageBox.question(self, NOME_APP,
                                    "Esta pasta ainda não foi indexada.\nIndexar agora?"
                                    ) == QMessageBox.Yes:
                self.iniciar_indexacao()

    def _adicionar_recente(self, d: str):
        rec = [d] + [p for p in (self.cfg.value("pastas_recentes", [], type=list) or [])
                     if os.path.normcase(p) != os.path.normcase(d)]
        rec = rec[:15]
        self.cfg.setValue("pastas_recentes", rec)
        self.cb_pasta.blockSignals(True)
        self.cb_pasta.clear()
        self.cb_pasta.addItems(rec)
        self.cb_pasta.setCurrentIndex(0)
        self.cb_pasta.blockSignals(False)
        self.definir_raiz(d)

    def remover_pasta_lista(self):
        if not self.raiz:
            return
        rec = [p for p in (self.cfg.value("pastas_recentes", [], type=list) or [])
               if os.path.normcase(p) != os.path.normcase(self.raiz)]
        self.cfg.setValue("pastas_recentes", rec)
        self.cb_pasta.blockSignals(True)
        self.cb_pasta.clear()
        self.cb_pasta.addItems(rec)
        self.cb_pasta.blockSignals(False)
        self.definir_raiz(rec[0] if rec else "")

    def definir_raiz(self, d: str):
        if self.trab_idx is not None:
            return
        self._fechar_con()
        self.previa.fechar_todos()
        self.modelo.definir([])
        self.raiz = d or None
        if d and banco.indice_existe(d):
            try:
                self.con = banco.conectar(d, somente_leitura=True)
            except Exception as e:
                self.con = None
                self.lbl_status.setText(f"Erro ao abrir o índice: {e}")
                return
        self.atualizar_status_indice()

    def _fechar_con(self):
        if self.con is not None:
            self.con.close()
            self.con = None
        self._cache_info.clear()

    def atualizar_status_indice(self):
        if not self.raiz:
            self.lbl_status.setText("Escolha uma pasta para começar.")
            return
        if self.con is None:
            self.lbl_status.setText("Pasta ainda não indexada.")
            return
        est = banco.estatisticas(self.con)
        txt = (f"Índice: {_fmt_int(est['documentos'])} PDFs · {_fmt_int(est['paginas'])} páginas")
        if est["paginas_ocr"]:
            txt += f" · {_fmt_int(est['paginas_ocr'])} por OCR"
        if est["paginas_vazias"]:
            txt += f" · {_fmt_int(est['paginas_vazias'])} sem texto"
        if est["com_problema"]:
            txt += f" · {_fmt_int(est['com_problema'])} com erro/senha"
        self.lbl_status.setText(txt)

    def info_documento(self, doc_id: int) -> dict | None:
        if self.con is None:
            return None
        d = self._cache_info.get(doc_id)
        if d is None:
            row = self.con.execute("SELECT tamanho, mtime_ns, sha256, paginas FROM documentos "
                                   "WHERE id=?", (doc_id,)).fetchone()
            if not row:
                return None
            d = {"tamanho": row[0], "mtime_ns": row[1], "sha256": row[2], "paginas": row[3]}
            self._cache_info[doc_id] = d
        return d

    def id_pagina_ocr(self, doc_id: int, numero: int) -> int | None:
        if self.con is None:
            return None
        row = self.con.execute("SELECT id FROM paginas WHERE doc_id=? AND numero=? AND "
                               "origem='ocr'", (doc_id, numero)).fetchone()
        return row[0] if row else None

    def palavras_ocr(self, pagina_id: int):
        if self.con is None:
            return None
        row = self.con.execute("SELECT palavras FROM paginas WHERE id=?", (pagina_id,)).fetchone()
        return banco.descompactar_palavras(row[0]) if row else None

    # ---- indexação ----
    def iniciar_indexacao(self):
        if not self.raiz:
            self.escolher_pasta()
            return
        if not os.path.isdir(self.raiz):
            QMessageBox.warning(self, NOME_APP, f"Pasta inacessível:\n{self.raiz}")
            return
        ocr = None
        if self.ck_ocr.isChecked():
            td, idi = detectar_tesseract()
            if td:
                ocr = {"tessdata": td, "idiomas": idi, "dpi": 300}
                self.log.appendPlainText(f"OCR ativado ({idi}) usando {td}")
            else:
                QMessageBox.information(self, NOME_APP, idi + "\n\nA indexação seguirá sem OCR.")
        self._fechar_con()
        self.previa.fechar_todos()
        self.b_idx.setEnabled(False)
        self.b_cancel.setEnabled(True)
        self.cb_pasta.setEnabled(False)
        self.barra.setRange(0, 0)
        self.barra.setFormat("Varrendo pastas…")
        self.t_idx0 = time.perf_counter()
        self.trab_idx = TrabalhadorIndexacao(self.raiz, ocr, self.sp_proc.value())
        self.trab_idx.progresso.connect(self.ao_progresso)
        self.trab_idx.mensagem.connect(self.log.appendPlainText)
        self.trab_idx.terminado.connect(self.ao_terminar_indexacao)
        self.trab_idx.start()

    def reconstruir(self):
        if not self.raiz or self.trab_idx is not None:
            return
        if QMessageBox.question(
                self, NOME_APP, "Apagar o índice desta pasta e indexar tudo novamente?\n"
                "(somente o arquivo de índice é apagado; os PDFs não são tocados)"
        ) != QMessageBox.Yes:
            return
        self._fechar_con()
        self.previa.fechar_todos()
        base = str(banco.arquivo_indice(self.raiz))
        for suf in ("", "-wal", "-shm"):
            try:
                os.remove(base + suf)
            except FileNotFoundError:
                pass
            except OSError as e:
                QMessageBox.warning(self, NOME_APP, f"Não foi possível apagar o índice:\n{e}")
                return
        self.iniciar_indexacao()

    def cancelar_indexacao(self):
        if self.trab_idx:
            self.trab_idx.cancelar.set()
            self.b_cancel.setEnabled(False)
            self.barra.setFormat("Cancelando…")

    def ao_progresso(self, feitos: int, total: int, atual: str):
        if total == 0:
            self.barra.setRange(0, 1)
            self.barra.setValue(1)
            return
        self.barra.setRange(0, total)
        self.barra.setValue(feitos)
        dt = time.perf_counter() - self.t_idx0
        taxa = feitos / dt if dt > 0 else 0
        resta = (total - feitos) / taxa if taxa > 0 else 0
        self.barra.setFormat(f"{feitos}/{total} PDFs (%p%)")
        self.lbl_idx.setText(f"{taxa:.1f} PDFs/s · faltam ~{int(resta // 60)}min{int(resta % 60):02d}s")

    def ao_terminar_indexacao(self, r: ResumoIndexacao | None):
        self.trab_idx.wait()
        self.trab_idx = None
        self.b_idx.setEnabled(True)
        self.b_cancel.setEnabled(False)
        self.cb_pasta.setEnabled(True)
        self.barra.setRange(0, 1)
        self.barra.setValue(1)
        self.lbl_idx.setText("")
        if r:
            msg = (f"{'Indexação cancelada' if r.cancelado else 'Indexação concluída'} em "
                   f"{r.segundos:.1f}s: {r.indexados} PDFs processados ({_fmt_int(r.paginas)} pág.), "
                   f"{r.removidos} removidos, {r.inalterados} sem alteração, {r.com_erro} com erro.")
            self.barra.setFormat(msg)
            self.log.appendPlainText(msg)
        else:
            self.barra.setFormat("Falha na indexação (veja o registro).")
        self.definir_raiz(self.raiz or "")
        if self.ed_busca.text().strip():
            self.buscar()

    # ---- busca ----
    def buscar(self):
        texto = self.ed_busca.text().strip()
        if not texto:
            return
        if self.con is None:
            self.statusBar().showMessage("Indexe a pasta antes de buscar.", 5000)
            return
        consulta = interpretar(texto, self.cb_modo.currentData())
        if not consulta.positivos:
            self.statusBar().showMessage("Consulta sem termos pesquisáveis.", 5000)
            return
        if self.trab_busca is not None:
            self.trab_busca.cancelar.set()
        self.id_busca += 1
        self.statusBar().showMessage("Buscando…")
        t = TrabalhadorBusca(self.id_busca, self.raiz, consulta, self.ed_filtro.text(),
                             self.cb_ordem.currentData())
        t.pronto.connect(self.ao_resultados)
        t.finished.connect(t.deleteLater)
        self.trab_busca = t
        self._consulta_pendente = consulta
        t.start()

    def ao_resultados(self, ident: int, res: list, resumo: ResumoBusca, seg: float, erro: str):
        if ident != self.id_busca:
            return
        self.trab_busca = None
        if erro:
            self.statusBar().showMessage(f"Erro na busca: {erro}", 8000)
            return
        self.consulta_atual = self._consulta_pendente
        self.modelo.definir(res)
        msg = (f"{_fmt_int(resumo.ocorrencias)} ocorrência(s) em {_fmt_int(resumo.paginas)} "
               f"página(s) de {_fmt_int(resumo.documentos)} PDF(s) · {seg * 1000:.0f} ms")
        if resumo.truncado:
            msg += " · lista limitada às primeiras 20.000 ocorrências (refine a busca ou use o filtro)"
        self.statusBar().showMessage(msg)
        if res:
            self.tabela.selectRow(0)
            self.tabela.setFocus()

    def ao_selecionar(self, atual: QModelIndex, _ant=None):
        if not atual.isValid() or not self.consulta_atual:
            return
        r = self.modelo.itens[atual.row()]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.previa.mostrar(self.raiz, r, self.consulta_atual)
        finally:
            QApplication.restoreOverrideCursor()

    def copiar_resultado(self):
        idx = self.tabela.currentIndex()
        if not idx.isValid():
            return
        r = self.modelo.itens[idx.row()]
        info = self.info_documento(r.doc_id) or {}
        txt = (f"\u201c{r.oc.antes}{r.oc.trecho}{r.oc.depois}\u201d\n"
               f"(Fonte: {os.path.join(self.raiz, r.caminho)}, página {r.pagina}"
               + (f", SHA-256 {info['sha256']}" if info.get("sha256") else "") + ")")
        QGuiApplication.clipboard().setText(txt)
        self.statusBar().showMessage("Trecho copiado com a referência.", 3000)

    def mover_selecao(self, passo: int):
        n = self.modelo.rowCount()
        if not n:
            return
        cur = self.tabela.currentIndex().row()
        novo = max(0, min(n - 1, (cur if cur >= 0 else -1) + passo))
        self.tabela.selectRow(novo)

    def exportar_resultados(self):
        if not self.modelo.itens:
            QMessageBox.information(self, NOME_APP, "Não há resultados para exportar.")
            return
        destino, _ = QFileDialog.getSaveFileName(self, "Exportar resultados",
                                                 str(Path.home() / "resultados_busca.csv"),
                                                 "CSV (*.csv)")
        if not destino:
            return
        if _dentro_de(destino, self.raiz):
            QMessageBox.warning(self, NOME_APP, "Por segurança, salve fora da pasta indexada.")
            return
        with open(destino, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow([f"# Busca: {self.consulta_atual.texto}  |  modo: "
                        f"{self.cb_modo.currentText()}  |  pasta: {self.raiz}  |  "
                        f"gerado em {banco.agora()} por {NOME_APP} {__version__}"])
            w.writerow(["arquivo", "pagina", "origem_texto", "trecho", "sha256_arquivo"])
            for r in self.modelo.itens:
                info = self.info_documento(r.doc_id) or {}
                w.writerow([os.path.join(self.raiz, r.caminho), r.pagina, r.origem,
                            f"{r.oc.antes}[{r.oc.trecho}]{r.oc.depois}", info.get("sha256", "")])
        self.statusBar().showMessage(f"Resultados exportados para {destino}", 6000)

    def situacao(self):
        if not self.raiz or not banco.indice_existe(self.raiz):
            QMessageBox.information(self, NOME_APP, "Esta pasta ainda não foi indexada.")
            return
        DialogoSituacao(self, self.raiz).exec()

    def info_ocr(self):
        td, idi = detectar_tesseract()
        if td:
            txt = f"Tesseract encontrado.\nIdiomas: {idi}\nPasta: {td}"
        else:
            txt = (idi + "\n\nPara ativar o OCR:\n"
                   "1. Instale o Tesseract para Windows (UB Mannheim) marcando o idioma "
                   "Portuguese; ou\n"
                   "2. Crie uma pasta 'tessdata' ao lado do IndexadorPDF.exe e coloque nela "
                   "o arquivo por.traineddata.\n\n"
                   "Depois marque 'OCR em páginas escaneadas' e clique em Indexar / Atualizar: "
                   "só os PDFs com páginas sem texto serão reprocessados.")
        QMessageBox.information(self, "OCR", txt)

    def sobre(self):
        QMessageBox.about(
            self, "Sobre",
            f"<b>{NOME_APP}</b> {__version__}<br>{__autor__}<br>Licença MIT<br><br>"
            "Indexação full-text (SQLite FTS5) e leitura de PDF com PyMuPDF.<br>"
            "Os arquivos de origem são abertos somente para leitura e nunca são alterados; "
            "o índice é gravado em:<br><code>"
            f"{html.escape(str(banco.pasta_dados()))}</code>")

    def closeEvent(self, ev):
        if self.trab_idx is not None:
            if QMessageBox.question(self, NOME_APP, "A indexação está em andamento. "
                                    "Cancelar e sair? (o progresso já feito fica salvo)"
                                    ) != QMessageBox.Yes:
                ev.ignore()
                return
            self.trab_idx.cancelar.set()
            self.trab_idx.wait(30000)
        self.cfg.setValue("geometria", self.saveGeometry())
        self.cfg.setValue("split", self.split.saveState())
        self.previa.fechar_todos()
        self._fechar_con()
        super().closeEvent(ev)


def executar() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(NOME_APP)
    app.setStyle("Fusion")
    j = JanelaPrincipal()
    j.show()
    return app.exec()
