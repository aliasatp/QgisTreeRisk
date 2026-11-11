# -*- coding: utf-8 -*-
"""
Arete VRA - Dialog interattivo (vra_dialog.py)
VRADialog con QgsMapLayerComboBox, _FieldRow, _PofRow, VRAWorkerDialog.
Importato da algorithm_dialog.py per aprirlo dalla galleria Processing.
"""

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QComboBox, QPushButton, QProgressBar, QTextEdit,
    QSpinBox, QDoubleSpinBox, QTabWidget, QWidget,
    QMessageBox, QScrollArea, QRadioButton, QButtonGroup,
)
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QFont

from qgis.gui import QgsMapLayerComboBox
from qgis.core import (
    QgsMapLayerProxyModel,
    QgsProject, QgsVectorLayer, QgsField, QgsFeature,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsGeometry, QgsPointXY,
    QgsMarkerSymbol, QgsCategorizedSymbolRenderer, QgsRendererCategory,
)
from qgis.PyQt.QtCore import QVariant

# Import the calculation engine
from .arete_engine import (
    DEFS_ALBERO, DEFS_BRANCA, DEFS_POF, POF_LABELS, CATEGORIE_BERSAGLIO,
    stima_bersaglio_albero, stima_bersaglio_branca,
    stima_bersaglio_da_cv,
    classe_fisica_albero, classe_fisica_branca,
    calc_rischio, rischio_peggiore,
    build_output_fields, applica_stile,
    crea_layer_spot,
    crs_utm_da_layer,
    configura_form_vra,
)

# 9. DIALOG INTERATTIVO
# ===========================================================================

# Metadati parametri: (chiave_params, nome_canonico_layer, etichetta, unita, default)
# I nomi canonici corrispondono esattamente ai campi del layer alberi-geometria.
DEFS_ALBERO = [
    ("h",      "h",       "Altezza albero",        "m",  12.0),
    ("d_ch",   "d_ch",    "Diametro chioma",        "m",   6.0),
    ("circonf","circonf",  "Circonferenza tronco",   "cm", 80.0),
    ("h_bers", "h_bers",  "Altezza bersaglio",      "m",   1.8),
]

DEFS_BRANCA = [
    ("d_br",   "d_br",    "Diametro branca",        "cm", 10.0),
    ("l_br",   "l_br",    "Lunghezza branca",       "m",   3.0),
    ("h_ins",  "h_ins",   "Inserzione branca",      "m",   6.0),
]

DEFS_POF = [
    ("pof1", "pof_radici",   "POF Radici"),
    ("pof2", "pof_colletto", "POF Colletto"),
    ("pof3", "pof_fusto",    "POF Fusto/Castello"),
    ("pof4", "pof_branche",  "POF Branche/Rami"),
]


class _FieldRow(QWidget):
    """
    Widget riga: [ComboBox campi layer] o: [SpinBox valore fisso]
    I campi vengono iniettati dopo la costruzione via set_fields().
    """

    def __init__(self, default=0.0, unit="", is_int=False,
                 mn=0.0, mx=9999.0, parent=None):
        super().__init__(parent)
        self._is_int = is_int

        hl = QHBoxLayout(self)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)

        self.cb = QComboBox()
        self.cb.setMinimumWidth(160)
        self.cb.setMaximumWidth(260)
        self.cb.setToolTip(
            "Seleziona il campo del layer oppure\n"
            "lascia vuoto e inserisci il valore fisso a destra."
        )
        self.cb.addItem("-- valore fisso --", "")
        hl.addWidget(self.cb, 3)

        hl.addWidget(QLabel("o:"))

        if is_int:
            self.sp = QSpinBox()
            self.sp.setRange(int(mn), int(mx))
            self.sp.setValue(int(default))
        else:
            self.sp = QDoubleSpinBox()
            self.sp.setRange(float(mn), float(mx))
            self.sp.setValue(float(default))
            self.sp.setDecimals(1)
            if unit:
                self.sp.setSuffix(" " + unit)
        self.sp.setMinimumWidth(85)
        hl.addWidget(self.sp, 2)

        self.cb.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        self.sp.setEnabled(self.cb.currentData() == "")

    def set_fields(self, field_names, canonical=""):
        """Popola il dropdown con i campi del layer; auto-seleziona se canonical."""
        prev = self.cb.currentData()
        self.cb.blockSignals(True)
        self.cb.clear()
        self.cb.addItem("-- valore fisso --", "")
        for fn in field_names:
            self.cb.addItem(fn, fn)
        # Priorita': 1) selezione precedente, 2) auto-match canonical
        restored = False
        if prev and prev in field_names:
            idx = self.cb.findData(prev)
            if idx >= 0:
                self.cb.setCurrentIndex(idx)
                restored = True
        if not restored and canonical and canonical in field_names:
            idx = self.cb.findData(canonical)
            if idx >= 0:
                self.cb.setCurrentIndex(idx)
        self.cb.blockSignals(False)
        self._sync()

    def value(self, feat=None, fnames=None):
        campo = self.cb.currentData()
        if campo and fnames and campo in fnames and feat is not None:
            v = feat[campo]
            if v is not None and str(v) not in ("NULL", "None", ""):
                try:
                    return int(v) if self._is_int else float(v)
                except (ValueError, TypeError):
                    pass
        return self.sp.value()

    def field_name(self):
        return self.cb.currentData() or ""


class _PofRow(QWidget):
    """Riga POF: [ComboBox campo layer] o: [ComboBox classe POF]"""

    def __init__(self, parent=None):
        super().__init__(parent)

        hl = QHBoxLayout(self)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)

        self.cb = QComboBox()
        self.cb.setMinimumWidth(160)
        self.cb.setMaximumWidth(260)
        self.cb.setToolTip("Campo layer con valore POF (1-7, 9, 0).")
        self.cb.addItem("-- valore fisso --", "")
        hl.addWidget(self.cb, 3)

        hl.addWidget(QLabel("o:"))

        self.pof = QComboBox()
        self.pof.setMinimumWidth(185)
        for val, label in POF_LABELS:
            self.pof.addItem(label, val)
        self.pof.setCurrentIndex(0)
        hl.addWidget(self.pof, 3)

        self.cb.currentIndexChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        self.pof.setEnabled(self.cb.currentData() == "")

    def set_fields(self, field_names, canonical=""):
        prev = self.cb.currentData()
        self.cb.blockSignals(True)
        self.cb.clear()
        self.cb.addItem("-- valore fisso --", "")
        for fn in field_names:
            self.cb.addItem(fn, fn)
        restored = False
        if prev and prev in field_names:
            idx = self.cb.findData(prev)
            if idx >= 0:
                self.cb.setCurrentIndex(idx)
                restored = True
        if not restored and canonical and canonical in field_names:
            idx = self.cb.findData(canonical)
            if idx >= 0:
                self.cb.setCurrentIndex(idx)
        self.cb.blockSignals(False)
        self._sync()

    def value(self, feat=None, fnames=None):
        campo = self.cb.currentData()
        if campo and fnames and campo in fnames and feat is not None:
            v = feat[campo]
            if v is not None and str(v) not in ("NULL", "None", ""):
                try:
                    return int(float(v))
                except (ValueError, TypeError):
                    pass
        return int(self.pof.currentData())

    def field_name(self):
        return self.cb.currentData() or ""


class VRADialog(QDialog):
    """
    Dialog VRA Arete v4.0.
    Usa QgsMapLayerComboBox nativo QGIS: i dropdown dei campi si aggiornano
    automaticamente al cambio del layer, senza pulsanti intermedi.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QgisTreeRisk")
        self.setMinimumWidth(700)
        self.setMinimumHeight(540)
        self.worker    = None
        self._alb_rows = {}
        self._bra_rows = {}
        self._pof_rows = {}
        self._row_molt     = None
        self._row_bman_alb = None
        self._row_bman_bra = None
        self._cv_layer     = None
        self._build_ui()
        # Primo caricamento campi
        self._aggiorna_campi()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(4)

        hdr = QLabel(
            "<b style='color:#2E7D32;font-size:12px'>"
            "Valutazione del Rischio Arboreo - VRA</b><br>"
            "<small>ALBERO raggio=altezza | BRANCA raggio=raggio chioma</small>"
        )
        hdr.setAlignment(Qt.AlignmentFlag.AlignCenter if hasattr(Qt, 'AlignmentFlag') else Qt.AlignCenter)
        root.addWidget(hdr)

        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        # --- TAB 1: Layer -------------------------------------------------
        t1_scroll = QScrollArea()
        t1_scroll.setWidgetResizable(True)
        t1    = QWidget()
        lt1   = QVBoxLayout(t1)
        lt1.setSpacing(8)
        t1_scroll.setWidget(t1)
        self._tabs.addTab(t1_scroll, "1 - Layer")

        # Banda licenza
        lbl_lic = QLabel(
            "<small><b>Protocollo Areté® v4.0 — ARBORETE®</b> &nbsp;|&nbsp; "
            "Il plugin rispetta la Licenza d'uso "
            "<b>CC BY-NC-ND 4.0</b> con cui è rilasciato il Protocollo Areté® &nbsp;|&nbsp; "
            "Plugin: <b>© ALIAS ATP — GPL 2.0</b></small>"
        )
        lbl_lic.setWordWrap(True)
        lbl_lic.setStyleSheet(
            "background:#1F4E79;color:white;padding:4px 8px;border-radius:3px;"
        )
        lt1.addWidget(lbl_lic)

        g_lay = QGroupBox("Layer alberi (punti)")
        fl    = QFormLayout(g_lay)
        self.lyr_combo = QgsMapLayerComboBox()
        try:
            self.lyr_combo.setFilters(QgsMapLayerProxyModel.Filter.PointLayer)
        except AttributeError:
            self.lyr_combo.setFilters(QgsMapLayerProxyModel.PointLayer)
        self.lyr_combo.layerChanged.connect(self._aggiorna_campi)
        fl.addRow("Layer alberi:", self.lyr_combo)
        self.lbl_stato = QLabel(
            "<small><i>I dropdown si aggiornano automaticamente "
            "al cambio del layer.</i></small>"
        )
        self.lbl_stato.setWordWrap(True)
        fl.addRow(self.lbl_stato)
        lt1.addWidget(g_lay)

        # ── Sorgente Bersaglio ─────────────────────────────────────────
        g_src = QGroupBox("Sorgente dati Bersaglio")
        ls    = QVBoxLayout(g_src)
        ls.addWidget(QLabel(
            "<small>Scegli come stimare la classe di Bersaglio per ogni albero.</small>"
        ))
        self._btn_grp = QButtonGroup(self)
        self.rb_cv  = QRadioButton(
            "Carta Vulnerabilita'  (layer poligonale creato dall'utente)"
        )
        self.rb_lay = QRadioButton(
            "Da layer alberi  (campi B_man_alb / B_man_bra nella tabella attributi)"
        )
        self.rb_cv.setChecked(True)
        for rb in (self.rb_cv, self.rb_lay):
            self._btn_grp.addButton(rb)
            ls.addWidget(rb)
        # rb_osm mantenuto come attributo per compatibilità interna
        self.rb_osm = self.rb_cv

        # Pannello CV
        self._w_cv = QWidget()
        lw = QFormLayout(self._w_cv)
        lw.setContentsMargins(20, 4, 0, 4)
        self.cv_combo = QgsMapLayerComboBox()
        try:
            self.cv_combo.setFilters(QgsMapLayerProxyModel.Filter.PolygonLayer)
        except AttributeError:
            self.cv_combo.setFilters(QgsMapLayerProxyModel.PolygonLayer)
        self.cv_combo.setAllowEmptyLayer(True)
        lw.addRow("Layer CV:", self.cv_combo)
        lw.addRow(QLabel(
            "<small><i>"
            "Vince il poligono con <b>cv_prob</b> piu' alto che interseca l'albero.<br>"
            "In parita' vince la classe B piu' gravosa.<br>"
            "Nessun poligono trovato → B = 7."
            "</i></small>"
        ))
        self._w_cv.setVisible(False)
        ls.addWidget(self._w_cv)

        # Pannello Da layer
        self._w_lay = QWidget()
        lay_vbox = QVBoxLayout(self._w_lay)
        lay_vbox.setContentsMargins(20, 6, 4, 6)
        lay_vbox.setSpacing(6)

        # Intestazione
        lbl_intro = QLabel(
            "<small><i>"
            "Configura come il plugin determina la classe di Bersaglio per ogni albero.<br>"
            "I campi per-albero hanno priorità sul valore fisso; il valore fisso ha priorità su OSM."
            "</i></small>"
        )
        lbl_intro.setWordWrap(True)
        lay_vbox.addWidget(lbl_intro)

        # Opzione A: campo per-albero
        g_opA = QGroupBox("A — Campo per-albero (dalla tabella attributi)")
        g_opA.setStyleSheet("QGroupBox{font-weight:bold;color:#1F4E79;}")
        fA = QFormLayout(g_opA)
        fA.setContentsMargins(8, 12, 8, 8)
        fA.addRow(QLabel(
            "<small><i>Seleziona il campo del layer che contiene la classe B "
            "(1-7=classe | 9=assente | 0/NULL=fallback).</i></small>"
        ))
        self._row_bman_alb = _FieldRow(default=0, is_int=True, mn=0, mx=9)
        fA.addRow("B_man_alb — Albero:", self._row_bman_alb)
        self._row_bman_bra = _FieldRow(default=0, is_int=True, mn=0, mx=9)
        fA.addRow("B_man_bra — Branca:", self._row_bman_bra)
        lay_vbox.addWidget(g_opA)

        # Opzione B: valore fisso globale
        g_opB = QGroupBox("B — Valore fisso globale (tutti gli alberi)")
        g_opB.setStyleSheet("QGroupBox{font-weight:bold;color:#375623;}")
        fB = QFormLayout(g_opB)
        fB.setContentsMargins(8, 12, 8, 8)
        fB.addRow(QLabel(
            "<small><i>Valore unico applicato a tutti gli alberi se il campo "
            "per-albero (A) è 0 o assente. 0 = fallback OSM.</i></small>"
        ))
        self.spin_b_alb = QSpinBox()
        self.spin_b_alb.setRange(0, 9)
        self.spin_b_alb.setValue(0)
        self.spin_b_alb.setToolTip(
            "B Albero fisso (0 = leggi da campo layer o fallback OSM)"
        )
        fB.addRow("B Albero fisso:", self.spin_b_alb)
        self.spin_b_bra = QSpinBox()
        self.spin_b_bra.setRange(0, 9)
        self.spin_b_bra.setValue(0)
        self.spin_b_bra.setToolTip(
            "B Branca fisso (0 = leggi da campo layer o fallback OSM)"
        )
        fB.addRow("B Branca fisso:", self.spin_b_bra)
        lay_vbox.addWidget(g_opB)

        # Moltiplicatore
        g_molt = QGroupBox("Moltiplicatore fattore di contatto")
        g_molt.setStyleSheet("QGroupBox{font-weight:bold;color:#833C00;}")
        fM = QFormLayout(g_molt)
        fM.setContentsMargins(8, 12, 8, 8)
        fM.addRow(QLabel(
            "<small><i>Fattore correttivo dell'impulso: 0=sospeso, "
            "1=standard, >1=aggravante.</i></small>"
        ))
        self._row_molt = _FieldRow(default=1, is_int=True, mn=0, mx=7)
        fM.addRow("Moltiplicatore:", self._row_molt)
        lay_vbox.addWidget(g_molt)

        self._w_lay.setVisible(False)
        ls.addWidget(self._w_lay)

        lt1.addWidget(g_src)

        g_geo = QGroupBox("Raggi di ricerca (da geometria albero)")
        fg    = QFormLayout(g_geo)
        fg.addRow(QLabel(
            "<small>"
            "<b>Albero</b>: raggio = altezza | area = media(h, chioma)<br>"
            "<b>Branca</b>: raggio = d_chioma/2 | area = lunghezza x 1.25"
            "</small>"
        ))
        lt1.addWidget(g_geo)
        lt1.addStretch()

        self.rb_cv.toggled.connect(self._aggiorna_pannello_sorgente)
        self.rb_lay.toggled.connect(self._aggiorna_pannello_sorgente)
        # Forza l'aggiornamento del pannello al primo avvio
        # (rb_cv è già selezionato ma i connect non erano ancora attivi)
        self._aggiorna_pannello_sorgente()


        # --- TAB 2: Settore ALBERO ----------------------------------------
        t2    = QScrollArea()
        t2.setWidgetResizable(True)
        i2    = QWidget()
        li2   = QVBoxLayout(i2)
        t2.setWidget(i2)
        self._tabs.addTab(t2, "2 - Albero (radici/colletto/fusto)")

        li2.addWidget(self._info(
            "<b>Settore ALBERO</b>: cedimento radici, colletto, fusto.<br>"
            "Raggio OSM = altezza albero.<br>"
            "Per ogni riga seleziona il <b>campo del layer</b> dal dropdown "
            "oppure inserisci un <b>valore fisso</b> nello spinbox."
        ))

        g_bio2 = QGroupBox("Biometria - Albero")
        fb2    = QFormLayout(g_bio2)
        for (key, canonical, label, unit, default) in DEFS_ALBERO:
            row = _FieldRow(default=default, unit=unit)
            self._alb_rows[key] = row
            fb2.addRow(label + " [" + unit + "]:", row)
        li2.addWidget(g_bio2)

        g_pof2 = QGroupBox("POF - Probabilita cedimento Radici / Colletto / Fusto")
        fp2    = QFormLayout(g_pof2)
        for (key, canonical, label) in DEFS_POF[:3]:
            row = _PofRow()
            self._pof_rows[key] = row
            fp2.addRow(label + ":", row)
        li2.addWidget(g_pof2)
        li2.addStretch()

        # --- TAB 3: Settore BRANCA ----------------------------------------
        t3    = QScrollArea()
        t3.setWidgetResizable(True)
        i3    = QWidget()
        li3   = QVBoxLayout(i3)
        t3.setWidget(i3)
        self._tabs.addTab(t3, "3 - Branca pericolosa")

        li3.addWidget(self._info(
            "<b>Settore BRANCA</b>: cedimento branche e rami.<br>"
            "Raggio OSM = raggio chioma (d_ch/2)."
        ))

        g_bio3 = QGroupBox("Biometria - Branca")
        fb3    = QFormLayout(g_bio3)
        for (key, canonical, label, unit, default) in DEFS_BRANCA:
            row = _FieldRow(default=default, unit=unit)
            self._bra_rows[key] = row
            fb3.addRow(label + " [" + unit + "]:", row)
        fb3.addRow(QLabel(
            "<small><i>Altezza bersaglio: condivisa col Tab 2.</i></small>"
        ))
        li3.addWidget(g_bio3)

        g_pof3 = QGroupBox("POF - Branche / Rami")
        fp3    = QFormLayout(g_pof3)
        row    = _PofRow()
        self._pof_rows["pof4"] = row
        fp3.addRow("POF Branche/Rami:", row)
        li3.addWidget(g_pof3)

        li3.addStretch()

        # --- TAB 4: Esegui ------------------------------------------------
        t4  = QWidget()
        lt4 = QVBoxLayout(t4)
        self._tabs.addTab(t4, "4 - Esegui")

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setFont(QFont("Courier New", 8))
        lt4.addWidget(self.txt_log)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        lt4.addWidget(self.progress)

        br = QHBoxLayout()
        self.btn_run = QPushButton("  Calcola VRA Arete v4.0")
        self.btn_run.setStyleSheet(
            "QPushButton{background:#2E7D32;color:white;"
            "font-weight:bold;padding:9px;border-radius:5px}"
            "QPushButton:hover{background:#1B5E20}"
        )
        self.btn_run.clicked.connect(self._run)
        bc = QPushButton("Chiudi")
        bc.clicked.connect(self.close)
        br.addWidget(self.btn_run)
        br.addWidget(bc)
        lt4.addLayout(br)

        root.addWidget(QLabel(
            "<small><i>Alias Associazione tra professionisti</i></small>"
        ))

    # ------------------------------------------------------------------
    # AGGIORNAMENTO AUTOMATICO CAMPI
    # ------------------------------------------------------------------


        # --- TAB 5: HELP --------------------------------------------------
        t5   = QWidget()
        lt5  = QVBoxLayout(t5)
        self._tabs.addTab(t5, "? Help")

        help_html = QTextEdit()
        help_html.setReadOnly(True)
        help_html.setHtml("""
<style>
  body  { font-family: Arial, sans-serif; font-size: 13px; color: #222; margin:12px; }
  h2    { color: #1F4E79; border-bottom: 2px solid #2E75B6; padding-bottom:4px; }
  h3    { color: #2E75B6; margin-top:14px; margin-bottom:4px; }
  table { border-collapse: collapse; width:100%; margin-bottom:10px; }
  th    { background:#2E75B6; color:#fff; padding:4px 8px; text-align:left; font-size:12px; }
  td    { border:1px solid #ccc; padding:4px 8px; font-size:12px; }
  tr:nth-child(even) td { background:#f2f2f2; }
  .warn { background:#FFF2CC; border-left:4px solid #F0AB18; padding:6px 10px; margin:8px 0; }
  .note { background:#D6E4F0; border-left:4px solid #2E75B6; padding:6px 10px; margin:8px 0; }
  .code { font-family: monospace; background:#f4f4f4; padding:2px 6px; border-radius:3px; }
</style>

<h2>QgisTreeRisk — Valutazione Rischio Arboreo</h2>
<p>Plugin QGIS per la Valutazione del Rischio Arboreo secondo il
<b>Protocollo Areté® v4.0</b> — ARBORETE®.</p>

<div class="warn">
<b>⚠ STRUMENTO AD USO ESCLUSIVAMENTE DIDATTICO — NON OPERATIVO</b><br>
I risultati non costituiscono una perizia professionale né hanno valenza legale,
assicurativa o amministrativa. Per valutazioni ufficiali consultare un tecnico
abilitato: <a href="http://www.protocolloarete.it">protocolloarete.it</a>
</div>

<h2>Sistema di riferimento degli output</h2>
<p>Il layer <span class="code">VRA_Arete_v4</span> e il layer
<span class="code">SPOT_*</span> vengono generati automaticamente nel
<b>CRS UTM WGS84</b> coerente con il layer alberi sorgente:</p>
<ul>
<li>Se il layer alberi è in <b>EPSG:4326</b> (gradi), il plugin calcola il fuso
UTM corretto dal centroide del layer (es. Italia → UTM32N o UTM33N).</li>
<li>Se il layer alberi è già in un <b>CRS proiettato metrico</b>, viene usato
quello direttamente.</li>
</ul>
<p>Il CRS di input e quello di output vengono mostrati nel log del Tab 4 all'avvio.</p>

<h2>Come usare il plugin</h2>

<h3>Tab 1 — Layer e Sorgente Bersaglio</h3>
<p>Selezionare il layer vettoriale puntuale contenente gli alberi da valutare.
Scegliere la sorgente per la stima del <b>Bersaglio (B)</b>:</p>
<table>
<tr><th>Sorgente</th><th>Descrizione</th></tr>
<tr><td><b>Carta Vulnerabilità</b></td><td>Layer poligonale con parametri di frequentazione personalizzati. Scegliere il layer CV dal selettore.</td></tr>
<tr><td><b>Manuale</b></td><td>Imposta una classe B fissa (1–7) per tutti gli alberi. 9 = bersaglio assente.</td></tr>
</table>

<h3>Tab 1 — Sorgente dati Bersaglio</h3>
<p>Scegliere come il plugin stima la classe di Bersaglio per ogni albero:</p>
<table>
<tr><th>Opzione</th><th>Descrizione</th><th>Quando usarla</th></tr>
<tr><td><b>Carta Vulnerabilità</b></td>
    <td>Usa un layer poligonale precompilato con i parametri di
    frequentazione reali. Vince il poligono con cv_prob più alto.</td>
    <td>Quando si dispone di dati locali (PUT, rilievi, ISTAT)</td></tr>
<tr><td><b>Da layer alberi</b></td>
    <td>Legge la classe B dai campi <span class="code">B_man_alb</span> /
    <span class="code">B_man_bra</span> della tabella attributi (mappati nel Tab 3).
    Se il campo è 0/NULL usa il <b>valore fisso</b> impostabile negli spin
    dello stesso pannello Tab 1. Fallback finale a OSM.</td>
    <td>Quando la classe B è già nota albero per albero o si vuole un valore fisso</td></tr>
</table>
<div class="note">
<b>Nota override:</b> anche in modalità OSM e Carta Vulnerabilità,
i campi <span class="code">B_man_alb</span> / <span class="code">B_man_bra</span>
della tabella attributi hanno <b>priorità massima</b> se valorizzati 1-9.
Permettono di personalizzare singoli alberi senza cambiare la sorgente globale.
</div>

<h3>Tab 2 — Settore Albero (radici/colletto/fusto)</h3>
<p>Per ogni parametro scegliere:</p>
<ul>
<li><b>Campo layer</b>: il plugin legge il valore dalla tabella attributi per ogni albero.</li>
<li><b>Valore fisso</b>: stesso valore per tutti gli alberi del layer.</li>
</ul>
<table>
<tr><th>Parametro</th><th>Unità</th><th>Descrizione</th></tr>
<tr><td>Altezza albero (h)</td><td>m</td><td>Altezza totale stimata — determina la classe CF e il raggio SPOT</td></tr>
<tr><td>Diametro chioma</td><td>m</td><td>Diametro medio chioma — usato per CF e raggio bersaglio branca</td></tr>
<tr><td>Circonferenza tronco</td><td>cm</td><td>Circonferenza a 1.30 m — usata per la classe CF albero</td></tr>
<tr><td>Altezza bersaglio</td><td>m</td><td>Altezza del bersaglio di riferimento (default 1.8 m)</td></tr>
<tr><td>POF Radici / Colletto / Fusto</td><td>classe 0–9</td><td>Probabilità di cedimento per settore (0=approfondimento, 9=assente)</td></tr>
</table>

<h3>Tab 3 — Settore Branca pericolosa</h3>
<table>
<tr><th>Parametro</th><th>Unità</th><th>Descrizione</th></tr>
<tr><td>Diametro branca</td><td>cm</td><td>Diametro della branca/ramo analizzato</td></tr>
<tr><td>Lunghezza branca</td><td>m</td><td>Lunghezza stimata della branca</td></tr>
<tr><td>Inserzione branca</td><td>m</td><td>Altezza di inserzione sul fusto</td></tr>
<tr><td>POF Branche/Rami</td><td>classe 0-9</td><td>Probabilità di cedimento branche</td></tr>
</table>
<div class="note">
Il <b>Moltiplicatore</b> e i campi <b>B_man_alb / B_man_bra</b> sono stati spostati nel
<b>Tab 1 → pannello "Da layer alberi"</b> dove è più coerente gestire tutte le sorgenti
del Bersaglio in un unico posto.
</div>

<h3>Tab 4 — Esegui</h3>
<p>Avviare il calcolo con il pulsante <b>Calcola VRA</b>.
Al termine vengono aggiunti due layer al progetto QGIS:</p>
<table>
<tr><th>Layer</th><th>Tipo</th><th>Contenuto</th></tr>
<tr><td>VRA_Arete_v4</td><td>Punti</td><td>Alberi con tutti i campi di rischio (B, CF, R per settore)</td></tr>
<tr><td>SPOT_*</td><td>Poligoni</td><td>Area di potenziale caduta (raggio = altezza albero)</td></tr>
</table>

<h2>Campi di output principali</h2>
<table>
<tr><th>Campo</th><th>Descrizione</th></tr>
<tr><td><span class="code">Ba_finale / Bb_finale</span></td><td>Classe bersaglio finale albero / branca (1–7, 9=assente)</td></tr>
<tr><td><span class="code">CF_albero / CF_branca</span></td><td>Classe di Impulso (energia cedimento)</td></tr>
<tr><td><span class="code">R_radici/colletto/fusto/branche</span></td><td>Livello di rischio per settore (1:3 → &lt;1:1M)</td></tr>
<tr><td><span class="code">Rg_radici/colletto/fusto/branche</span></td><td>Giudizio ordinario per settore</td></tr>
<tr><td><span class="code">Rs_radici/colletto/fusto/branche</span></td><td>Speditiva triage per settore</td></tr>
<tr><td><span class="code">Rv_radici/colletto/fusto/branche</span></td><td>Gravità numerica (1=accettabile ... 6=inaccettabile)</td></tr>
<tr><td><span class="code">R_peggiore</span></td><td>Rischio complessivo più gravoso tra i 4 settori</td></tr>
<tr><td><span class="code">R_giudizio / R_speditiva</span></td><td>Giudizio ordinario e triage complessivo</td></tr>
</table>

<h2>Carta della Vulnerabilità (CV)</h2>
<p>Per creare una Carta della Vulnerabilità personalizzata usare gli algoritmi
nella galleria Processing <b>QgisTreeRisk</b>:</p>
<ul>
<li><b>Crea Carta Vulnerabilità</b> — genera un layer GeoPackage vuoto con
lo schema standard e form di inserimento guidato.</li>
<li><b>Genera Carta Vulnerabilità da OSM</b> — scarica strade, edifici,
landuse e POI da OpenStreetMap e precompila la CV con valori standard.
Supporta il fattore demografico k (popolazione, turismo, zonizzazione).</li>
</ul>

<div class="note">
<b>Metodo di calcolo B occupazione:</b><br>
• <b>Geometrico</b> (default per tipo occupazione): proporziona l'occupazione alla SPOT/SDAN — consigliato per aree estese (parchi, pertinenze).<br>
• <b>Flat</b>: usa cv_ore_flat direttamente — per aree piccole o dato già elaborato.
</div>

<h2>Riferimenti</h2>
<ul>
<li>Protocollo Areté® v4.0 — <a href="http://www.protocolloarete.it">protocolloarete.it</a></li>
<li>Repository plugin: <a href="https://github.com/aliasatp/QgisTreeRisk">github.com/aliasatp/QgisTreeRisk</a></li>
<li>Segnalazioni: <a href="https://github.com/aliasatp/QgisTreeRisk/issues">Issues GitHub</a></li>
</ul>
<p><small>
<b>Plugin QgisTreeRisk:</b> © ALIAS ATP — GPL 2.0 License<br>
<b>Protocollo Areté® v4.0:</b> Il plugin rispetta la Licenza d'uso
<b>CC BY-NC-ND 4.0</b> con cui è rilasciato il Protocollo Areté® —
ARBORETE®. L'utilizzo non è consentito per scopi commerciali e non è
ammessa la distribuzione di opere derivate senza autorizzazione degli autori.<br>
Citazione: <i>"Protocollo Areté® per la Valutazione del Rischio Arboreo
[ver. 4.0] — ARBORETE® (protocolloarete.it)"</i>
</small></p>
""")
        lt5.addWidget(help_html)
    def _aggiorna_pannello_sorgente(self):
        """Mostra/nasconde i pannelli CV e Manuale in base al radiobutton scelto."""
        self._w_cv.setVisible(self.rb_cv.isChecked())
        self._w_lay.setVisible(self.rb_lay.isChecked())

    def sorgente_bersaglio(self):
        """Restituisce 'osm', 'cv' o 'man'."""
        if self.rb_lay.isChecked():
            return "lay"
        return "cv"

    def _aggiorna_campi(self):
        """
        Chiamata automaticamente da QgsMapLayerComboBox.layerChanged.
        Popola tutti i _FieldRow e _PofRow con i campi del layer corrente
        e tenta l'auto-match per nome canonico.
        """
        layer = self.lyr_combo.currentLayer()
        if not layer:
            return

        fnames = [f.name() for f in layer.fields()]
        n_match = 0

        # Tab 2 - biometria albero
        for (key, canonical, label, unit, default) in DEFS_ALBERO:
            row = self._alb_rows.get(key)
            if row:
                row.set_fields(fnames, canonical)
                if row.field_name():
                    n_match += 1

        # Tab 2 - POF radici/colletto/fusto
        for (key, canonical, label) in DEFS_POF[:3]:
            row = self._pof_rows.get(key)
            if row:
                row.set_fields(fnames, canonical)
                if row.field_name():
                    n_match += 1

        # Tab 3 - biometria branca
        for (key, canonical, label, unit, default) in DEFS_BRANCA:
            row = self._bra_rows.get(key)
            if row:
                row.set_fields(fnames, canonical)
                if row.field_name():
                    n_match += 1

        # Tab 3 - POF branche
        r4 = self._pof_rows.get("pof4")
        if r4:
            r4.set_fields(fnames, "pof_branche")
            if r4.field_name():
                n_match += 1

        # Tab 3 - moltiplicatore e B_manuale
        if self._row_molt:
            self._row_molt.set_fields(fnames, "molt")
            if self._row_molt.field_name():
                n_match += 1
        if self._row_bman_alb:
            self._row_bman_alb.set_fields(fnames, "B_man_alb")
            if self._row_bman_alb.field_name():
                n_match += 1
        if self._row_bman_bra:
            self._row_bman_bra.set_fields(fnames, "B_man_bra")
            if self._row_bman_bra.field_name():
                n_match += 1

        self.lbl_stato.setText(
            "<small><b style='color:#2E7D32'>"
            + layer.name() + ": "
            + str(len(fnames)) + " campi"
            + (", " + str(n_match) + " auto-abbinati" if n_match else "")
            + ".</b> Verifica/correggi nei Tab 2 e 3.</small>"
        )

    # ------------------------------------------------------------------
    # HELPERS
    # ------------------------------------------------------------------

    def _info(self, html):
        lbl = QLabel("<small>" + html + "</small>")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            "background:#e8f5e9;border:1px solid #a5d6a7;"
            "border-radius:4px;padding:5px;"
        )
        return lbl

    # ------------------------------------------------------------------
    # LETTURA VALORI PER FEATURE
    # ------------------------------------------------------------------

    def read_params(self, feat, fnames):
        def rv(rows, key, default=0.0):
            r = rows.get(key)
            return r.value(feat, fnames) if r else default

        def rp(key, default=9):
            r = self._pof_rows.get(key)
            return r.value(feat, fnames) if r else default

        # Sorgente bersaglio
        sorg = self.sorgente_bersaglio()

        # Bersaglio per-albero (da campo layer) -- usato solo in modalita' manuale
        bm_alb = self._row_bman_alb.value(feat, fnames) if self._row_bman_alb else 0
        bm_bra = self._row_bman_bra.value(feat, fnames) if self._row_bman_bra else 0

        # sorg=='lay': campo layer è principale; valore fisso spin se campo è 0
        # sorg=='osm' o 'cv': campo layer fa override max priorità se 1-9
        if sorg == "lay":
            # Fallback a valore fisso se il campo layer è 0 o non valorizzato
            if int(bm_alb) not in range(1, 10):
                bm_alb = self.spin_b_alb.value()
            if int(bm_bra) not in range(1, 10):
                bm_bra = self.spin_b_bra.value()
        else:
            # OSM/CV: usa il campo layer solo se valorizzato 1-9
            if int(bm_alb) not in range(1, 10):
                bm_alb = 0
            if int(bm_bra) not in range(1, 10):
                bm_bra = 0

        # Layer CV selezionato (None se non in modalita' cv)
        cv_layer = self.cv_combo.currentLayer() if sorg == "cv" else None

        return {
            "h":      rv(self._alb_rows, "h",      12.0),
            "d_ch":   rv(self._alb_rows, "d_ch",    6.0),
            "circonf":rv(self._alb_rows, "circonf", 80.0),
            "h_bers": rv(self._alb_rows, "h_bers",  1.8),
            "d_br":   rv(self._bra_rows, "d_br",   10.0),
            "l_br":   rv(self._bra_rows, "l_br",    3.0),
            "h_ins":  rv(self._bra_rows, "h_ins",   6.0),
            "pof1":   rp("pof1"), "pof2": rp("pof2"),
            "pof3":   rp("pof3"), "pof4": rp("pof4"),
            "molt":   self._row_molt.value(feat, fnames) if self._row_molt else 1,
            "bm_alb": int(bm_alb),
            "bm_bra": int(bm_bra),
            "sorg":   sorg,
            "cv_layer": cv_layer,
        }

    # ------------------------------------------------------------------
    # RUN
    # ------------------------------------------------------------------

    def _run(self):
        layer = self.lyr_combo.currentLayer()
        if not layer:
            QMessageBox.warning(self, "Attenzione", "Seleziona un layer puntuale.")
            return

        self.btn_run.setEnabled(False)
        self.progress.setValue(0)
        self.txt_log.clear()
        self._tabs.setCurrentIndex(3)

        sorg = self.sorgente_bersaglio()
        sorg_label = {
            "cv":  "Carta Vulnerabilita' - " + (
                self.cv_combo.currentLayer().name()
                if self.cv_combo.currentLayer() else "nessun layer"),
            "lay": "Da layer alberi",
        }.get(sorg, sorg)

        crs_out_log = crs_utm_da_layer(layer)
        self.txt_log.append("VRA Arete v4.0 - avvio")
        self.txt_log.append(
            "Layer: " + layer.name()
            + " (" + str(layer.featureCount()) + " feature)"
        )
        self.txt_log.append("CRS input:  " + layer.crs().authid())
        self.txt_log.append("CRS output: " + crs_out_log.authid() + " (UTM)")
        self.txt_log.append("Sorgente bersaglio: " + sorg_label)

        def d(rows, key):
            r = rows.get(key)
            if r:
                fn = r.field_name()
                return ("campo[" + fn + "]") if fn else "fisso=" + str(r.sp.value())
            return "N/D"

        def dp(key):
            r = self._pof_rows.get(key)
            if r:
                fn = r.field_name()
                return ("campo[" + fn + "]") if fn else "fisso"
            return "N/D"

        self.txt_log.append(
            "ALB: h=" + d(self._alb_rows, "h")
            + " ch=" + d(self._alb_rows, "d_ch")
            + " circ=" + d(self._alb_rows, "circonf")
            + " hb=" + d(self._alb_rows, "h_bers")
        )
        self.txt_log.append(
            "ALB POF: rad=" + dp("pof1")
            + " col=" + dp("pof2")
            + " fus=" + dp("pof3")
        )
        self.txt_log.append(
            "BRA: db=" + d(self._bra_rows, "d_br")
            + " lb=" + d(self._bra_rows, "l_br")
            + " hi=" + d(self._bra_rows, "h_ins")
            + " pof=" + dp("pof4")
        )
        self.txt_log.append("")

        self.worker = VRAWorkerDialog(layer, self)
        self.worker.sig_progress.connect(self.progress.setValue)
        self.worker.sig_log.connect(self.txt_log.append)
        self.worker.sig_done.connect(self._done)
        self.worker.start()

    def _done(self, result_layer):
        self.btn_run.setEnabled(True)
        if result_layer is None:
            QMessageBox.warning(self, "Errore", "Elaborazione fallita.")
            return
        QgsProject.instance().addMapLayer(result_layer)
        applica_stile(result_layer)
        configura_form_vra(result_layer)

        # Crea e aggiunge il layer SPOT (area di potenziale caduta, raggio = h)
        try:
            result_layer.dataProvider().forceReload()
            result_layer.updateExtents()
            spot_lyr = crea_layer_spot(
                result_layer,
                "SPOT_" + (result_layer.name() or "VRA")
            )
            if spot_lyr is not None and spot_lyr.isValid() and spot_lyr.featureCount() > 0:
                QgsProject.instance().addMapLayer(spot_lyr)
                self.txt_log.append(
                    "Layer SPOT aggiunto: " + spot_lyr.name()
                    + " (" + str(spot_lyr.featureCount()) + " poligoni)"
                )
            elif spot_lyr is not None:
                self.txt_log.append("SPOT: layer vuoto (verifica campo Ba_raggio_m)")
        except Exception as ex_spot:
            import traceback
            self.txt_log.append("SPOT non generata: " + str(ex_spot))
            self.txt_log.append(traceback.format_exc()[:400])

        QMessageBox.information(
            self, "Completato",
            "Elaborazione completata.\n\n"
            "Layer aggiunti al progetto:\n"
            "  VRA_Arete_v4    — punti alberi con tutti i campi di rischio\n"
            "  SPOT_*          — poligoni area di potenziale caduta (raggio=h)\n\n"
            "Campi rischio per settore:\n"
            "  R_radici/R_colletto/R_fusto/R_branche — livello (1:3, 1:20...)\n"
            "  Rg_radici/colletto/fusto/branche      — giudizio ordinario\n"
            "  Rs_radici/colletto/fusto/branche      — speditiva (triage)\n"
            "  Rv_radici/colletto/fusto/branche      — gravita (1-7)\n\n"
            "Rischio complessivo: R_peggiore / R_giudizio / R_speditiva\n"
            "Stile Arete applicato su R_peggiore."
        )


# ---------------------------------------------------------------------------
# Worker per il dialog
# ---------------------------------------------------------------------------

class VRAWorkerDialog(QThread):
    sig_progress = pyqtSignal(int)
    sig_log      = pyqtSignal(str)
    sig_done     = pyqtSignal(object)

    def __init__(self, layer, dialog, parent=None):
        super().__init__(parent)
        self.layer  = layer
        self.dialog = dialog

    def run(self):
        layer = self.layer
        dlg   = self.dialog
        total = layer.featureCount()
        if total == 0:
            self.sig_log.emit("Layer vuoto.")
            self.sig_done.emit(None)
            return

        crs_src   = layer.crs()
        # Determina CRS UTM coerente con il layer sorgente
        crs_out   = crs_utm_da_layer(layer)
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        # Trasformazione: CRS sorgente → WGS84 (per CV e calcoli geografici)
        tr        = QgsCoordinateTransform(crs_src, crs_wgs84,
                                           QgsProject.instance())
        # Trasformazione: CRS sorgente → UTM output
        tr_out    = QgsCoordinateTransform(crs_src, crs_out,
                                           QgsProject.instance())

        out_lyr = QgsVectorLayer(
            "Point?crs=" + crs_out.authid(), "VRA_Arete_v4", "memory"
        )
        prov            = out_lyr.dataProvider()
        existing_fields = layer.fields()
        out_field_list  = build_output_fields(existing_fields)
        prov.addAttributes(out_field_list)
        out_lyr.updateFields()
        out_fields = out_lyr.fields()

        fnames    = [f.name() for f in existing_fields]
        feats_out = []

        for i, feat in enumerate(layer.getFeatures()):
            geom = feat.geometry()
            if geom.isEmpty():
                self.sig_log.emit(
                    "Feature " + str(feat.id()) + " vuota, saltata."
                )
                continue

            pt  = tr.transform(geom.centroid().asPoint())
            lat = pt.y()
            lon = pt.x()

            self.sig_log.emit(
                "[" + str(i + 1) + "/" + str(total)
                + "] id=" + str(feat.id())
            )

            p      = dlg.read_params(feat, fnames)
            h      = p["h"];   d_ch = p["d_ch"]; circ = p["circonf"]
            hb     = p["h_bers"]
            db     = p["d_br"]; lb = p["l_br"];  hi   = p["h_ins"]
            bm_alb = p["bm_alb"]
            bm_bra = p["bm_bra"]
            sorg   = p["sorg"]
            cv_lyr = p["cv_layer"]

            ba_veic = ba_ped = ba_occ = -1
            bb_veic = bb_ped = bb_occ = -1

            # ── Sorgente: Carta Vulnerabilita' ────────────────────────────
            if sorg == "lay":
                ba_fin = int(bm_alb) if int(bm_alb) in range(1, 10) else 7
                bb_fin = int(bm_bra) if int(bm_bra) in range(1, 10) else 7
                ba_info = {"raggio_m": int(round(h)), "strada": "layer",
                           "vel": 0, "vei_g": 0, "ped_g": 0}
                bb_info = {"raggio_m": max(int(round(d_ch/2)), 3), "strada": "layer",
                           "vel": 0, "vei_g": 0, "ped_g": 0}
                log_alb = "B_alb=layer(" + str(ba_fin) + ")"
                log_bra = "B_bra=layer(" + str(bb_fin) + ")"

            elif sorg == "cv" and cv_lyr is not None:
                geom_pt = feat.geometry()
                ba_fin, bb_fin, cv_info = stima_bersaglio_da_cv(
                    geom_pt, cv_lyr, layer.crs(), h, d_ch, lb
                )
                zona_alb = cv_info.get("zona_alb", "?")
                zona_bra = cv_info.get("zona_bra", "?")
                prob_alb = cv_info.get("prob_alb", 0)
                prob_bra = cv_info.get("prob_bra", 0)
                r_alb   = cv_info.get("r_alb_m", 0)
                r_bra   = cv_info.get("r_bra_m", 0)
                log_alb = (
                    "B_alb=CV(" + str(ba_fin)
                    + ",r=" + str(round(r_alb)) + "m"
                    + ",zona='" + zona_alb + "'"
                    + ",prob=" + str(prob_alb) + ")"
                )
                log_bra = (
                    "B_bra=CV(" + str(bb_fin)
                    + ",r=" + str(round(r_bra)) + "m"
                    + ",zona='" + zona_bra + "'"
                    + ",prob=" + str(prob_bra) + ")"
                )
                ba_info = {
                    "raggio_m": round(r_alb), "strada": "CV:" + zona_alb,
                    "vel": 0, "vei_g": 0, "ped_g": 0
                }
                bb_info = {
                    "raggio_m": round(r_bra), "strada": "CV:" + zona_bra,
                    "vel": 0, "vei_g": 0, "ped_g": 0
                }

            # ── Sorgente: Manuale ─────────────────────────────────────────
            elif int(bm_alb) in range(1, 10) or int(bm_bra) in range(1, 10):
                ba_fin  = int(bm_alb) if int(bm_alb) in range(1, 10) else 7
                bb_fin  = int(bm_bra) if int(bm_bra) in range(1, 10) else 7
                ba_info = {
                    "raggio_m": int(round(h)), "strada": "manuale",
                    "vel": 0, "vei_g": 0, "ped_g": 0
                }
                bb_info = {
                    "raggio_m": max(int(round(d_ch / 2)), 3),
                    "strada": "manuale", "vel": 0, "vei_g": 0, "ped_g": 0
                }
                log_alb = "B_alb=man(" + str(ba_fin) + ")"
                log_bra = "B_bra=man(" + str(bb_fin) + ")"

            # ── Nessuna sorgente valida: B=7 (trascurabile) ───────────────
            else:
                ba_fin = 7
                ba_info = {"raggio_m": int(round(h)), "strada": "N.D.",
                           "vel": 0, "vei_g": 0, "ped_g": 0}
                ba_veic = ba_ped = ba_occ = 7
                bb_fin = 7
                bb_info = {"raggio_m": max(int(round(d_ch/2)), 3),
                           "strada": "N.D.", "vel": 0, "vei_g": 0, "ped_g": 0}
                bb_veic = bb_ped = bb_occ = 7
                log_alb = "B_alb=N.D."
                log_bra = "B_bra=N.D."

            log_ab = log_alb + "  " + log_bra

            cfa, e_alb = classe_fisica_albero(h, circ, hb)
            cfb, e_bra = classe_fisica_branca(db, lb, hi, hb)

            r_rad = calc_rischio(ba_fin, cfa, p["pof1"], p["molt"])
            r_col = calc_rischio(ba_fin, cfa, p["pof2"], p["molt"])
            r_fus = calc_rischio(ba_fin, cfa, p["pof3"], p["molt"])
            r_bra = calc_rischio(bb_fin, cfb, p["pof4"], p["molt"])
            r_peg = rischio_peggiore(r_rad, r_col, r_fus, r_bra)

            self.sig_log.emit(
                "  " + log_ab
                + " CF_a=" + str(cfa) + " CF_b=" + str(cfb)
                + " R=" + r_peg["r"]
            )

            out_feat = QgsFeature(out_fields)
            # Riproietta la geometria nel CRS di output (UTM)
            geom_out = feat.geometry()
            if crs_src != crs_out:
                geom_out = QgsGeometry(geom_out)
                geom_out.transform(tr_out)
            out_feat.setGeometry(geom_out)
            out_feat.setAttributes(feat.attributes() + [
                ba_veic, ba_ped, ba_occ, ba_fin,
                ba_info["raggio_m"], ba_info["strada"],
                ba_info["vel"], ba_info["vei_g"], ba_info["ped_g"],
                bb_veic, bb_ped, bb_occ, bb_fin,
                bb_info["raggio_m"], bb_info["strada"],
                bb_info["vel"], bb_info["vei_g"], bb_info["ped_g"],
                cfa, cfb, round(e_alb, 1), round(e_bra, 1),
                # livelli r per settore
                r_rad["r"], r_col["r"], r_fus["r"], r_bra["r"],
                # giudizio ordinario per settore
                r_rad["giudizio"], r_col["giudizio"],
                r_fus["giudizio"], r_bra["giudizio"],
                # speditiva triage per settore
                r_rad["speditiva"], r_col["speditiva"],
                r_fus["speditiva"], r_bra["speditiva"],
                # gravita per settore
                r_rad["gr"], r_col["gr"], r_fus["gr"], r_bra["gr"],
                # complessivo
                r_peg["r"], r_peg["colore"],
                r_peg["giudizio"], r_peg["speditiva"], r_peg["gr"],
            ])
            feats_out.append(out_feat)
            self.sig_progress.emit(int((i + 1) / total * 100))

        prov.addFeatures(feats_out)
        out_lyr.updateExtents()
        self.sig_log.emit(
            "\nCompletato: " + str(len(feats_out)) + " alberi elaborati."
        )
        self.sig_done.emit(out_lyr)



# ===========================================================================
