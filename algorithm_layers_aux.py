# -*- coding: utf-8 -*-
"""
Algoritmi Processing per la creazione dei layer ausiliari della CV da OSM:
  1. AreteCreaComuniAlgorithm — layer confini comunali con campo popolazione
  2. AreteCreaZonizzazioneAlgorithm — layer zonizzazione demografica (dz_k)
"""

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterCrs,
    QgsProcessingOutputVectorLayer,
    QgsVectorLayer, QgsField, QgsFields,
    QgsVectorFileWriter, QgsCoordinateReferenceSystem,
    QgsEditorWidgetSetup, QgsDefaultValue,
    QgsEditFormConfig, QgsAttributeEditorContainer,
    QgsAttributeEditorField, QgsProject,
)
from qgis.PyQt.QtCore import QVariant, QCoreApplication

def _attiva_editing_layer(lyr):
    """
    Inizializza lo stack di editing e l'indice spaziale su un layer GeoPackage
    appena creato. Senza questo passaggio QGIS non registra correttamente
    le nuove geometrie: snap, selezione grafica e salva-modifiche non funzionano
    fino al reload del progetto.

    Il ciclo startEditing/commitChanges forza QGIS a:
    - Registrare il layer nel sistema di editing
    - Costruire l'indice spaziale interno
    - Attivare i trigger rtree del GeoPackage
    - Aggiornare le capabilities del provider
    """
    if not lyr or not lyr.isValid():
        return
    if lyr.providerType() != "ogr":
        return
    # Ciclo di editing vuoto: forza l'inizializzazione completa
    lyr.startEditing()
    lyr.commitChanges()
    # Indice spaziale esplicito
    prov = lyr.dataProvider()
    if prov:
        prov.createSpatialIndex()
    lyr.updateExtents()



# ============================================================================
# HELPER: configura form attributi
# ============================================================================

def _field(name, typ, length=0, prec=0, comment=""):
    f = QgsField(name, typ, "", length, prec)
    f.setComment(comment)
    return f

def _editor(lyr, name, widget_type, config):
    idx = lyr.fields().indexOf(name)
    if idx >= 0:
        lyr.setEditorWidgetSetup(idx, QgsEditorWidgetSetup(widget_type, config))

def _default(lyr, name, expr, apply_on_update=False):
    idx = lyr.fields().indexOf(name)
    if idx >= 0:
        lyr.setDefaultValueDefinition(idx, QgsDefaultValue(expr, apply_on_update))

def _alias(lyr, name, label):
    idx = lyr.fields().indexOf(name)
    if idx >= 0:
        lyr.setFieldAlias(idx, label)

def _scheda(title, field_names):
    cont = QgsAttributeEditorContainer(title, None)
    cont.setColumnCount(1)
    # Container di tipo Tab per QGIS >= 3.32 / 4.x
    try:
        from qgis.core import Qgis as _Qgis
        cont.setType(_Qgis.AttributeEditorContainerType.Tab)
    except Exception:
        try:
            cont.setIsGroupBox(False)
        except Exception:  # nosec B110 - fallback API QGIS < 3.32, fallimento non critico
            pass
    for fn in field_names:
        cont.addChildElement(QgsAttributeEditorField(fn, -1, cont))
    return cont


# ============================================================================
# 1. CREA LAYER COMUNI (confini con popolazione)
# ============================================================================

def crea_layer_comuni(percorso_gpkg=None, crs=None):
    """
    Crea un layer poligonale per i confini comunali con campo popolazione.
    Usato dalla funzione 'Genera CV da OSM' come sorgente del fattore k
    demografico (modalità 'Layer comuni').

    Campi:
      com_id        Integer  ID univoco
      com_nome      String   Nome del comune
      com_prov      String   Provincia / codice ISTAT
      com_regione   String   Regione
      pop_res       Integer  Popolazione residente (campo principale per k)
      pop_tot       Integer  Popolazione totale (inclusi presenti non residenti)
      pop_anno      Integer  Anno di riferimento del dato
      com_fonte     String   Fonte del dato (es. 'ISTAT 2021', 'anagrafe')
      com_note      String   Note

    Il plugin cerca il campo popolazione con questi alias (in ordine):
      pop_res, pop, abitanti, resid, tot_res
    Raccomandato: usare pop_res per massima compatibilità.
    """
    LAYER_NAME = "comuni_popolazione"
    _crs = crs if crs is not None else QgsCoordinateReferenceSystem("EPSG:4326")
    _crs_str = _crs.authid()

    fields = QgsFields()
    fields.append(_field("com_id",      QVariant.Int,    0,  0, "ID automatico"))
    fields.append(_field("com_nome",    QVariant.String, 80, 0, "Nome comune"))
    fields.append(_field("com_prov",    QVariant.String, 40, 0, "Provincia / codice ISTAT"))
    fields.append(_field("com_regione", QVariant.String, 40, 0, "Regione"))
    fields.append(_field("pop_res",     QVariant.Int,    0,  0, "Popolazione residente — campo principale per fattore k"))
    fields.append(_field("pop_tot",     QVariant.Int,    0,  0, "Popolazione totale (inclusi presenti)"))
    fields.append(_field("pop_anno",    QVariant.Int,    0,  0, "Anno di riferimento del dato"))
    fields.append(_field("com_fonte",   QVariant.String, 120,0, "Fonte (es. ISTAT 2021, anagrafe comunale)"))
    fields.append(_field("com_note",    QVariant.String, 254,0, "Note"))

    err_msg = None
    lyr = None

    if percorso_gpkg:
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName  = LAYER_NAME
        opts.fileEncoding = "UTF-8"

        tmp = QgsVectorLayer("Polygon?crs=" + _crs_str, LAYER_NAME, "memory")
        tmp.dataProvider().addAttributes(fields.toList())
        tmp.updateFields()

        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, percorso_gpkg, tmp.transformContext(), opts
        )
        if err[0] != QgsVectorFileWriter.WriterError.NoError:
            return None, f"Errore scrittura GeoPackage: {err[1]}"
        lyr = QgsVectorLayer(percorso_gpkg + "|layername=" + LAYER_NAME,
                             LAYER_NAME, "ogr")
        if not lyr.isValid():
            return None, "Layer comuni non valido dopo la scrittura"
    else:
        lyr = QgsVectorLayer("Polygon?crs=" + _crs_str, LAYER_NAME, "memory")
        lyr.dataProvider().addAttributes(fields.toList())
        lyr.updateFields()

    _configura_form_comuni(lyr)
    _attiva_editing_layer(lyr)
    return lyr, err_msg


def _configura_form_comuni(lyr):
    fi = lambda n: lyr.fields().indexOf(n)

    # Widget editor
    _editor(lyr, "com_id",      "Hidden",  {})
    _editor(lyr, "com_nome",    "TextEdit", {"IsMultiline": False})
    _editor(lyr, "com_prov",    "TextEdit", {"IsMultiline": False})
    _editor(lyr, "com_regione", "TextEdit", {"IsMultiline": False})
    _editor(lyr, "pop_res",     "Range", {
        "Min": 0, "Max": 20000000, "Step": 100,
        "Suffix": " abitanti (residenti)", "Style": "SpinBox"
    })
    _editor(lyr, "pop_tot",     "Range", {
        "Min": 0, "Max": 20000000, "Step": 100,
        "Suffix": " abitanti (totali)", "Style": "SpinBox"
    })
    _editor(lyr, "pop_anno",    "Range", {
        "Min": 2000, "Max": 2100, "Step": 1,
        "Suffix": " (anno)", "Style": "SpinBox"
    })
    _editor(lyr, "com_fonte",   "TextEdit", {"IsMultiline": False})
    _editor(lyr, "com_note",    "TextEdit", {"IsMultiline": True})

    # Default
    _default(lyr, "pop_anno",  "year(now())")
    _default(lyr, "com_fonte", "'ISTAT'")

    # Alias
    aliases = {
        "com_id":      "ID",
        "com_nome":    "Nome comune",
        "com_prov":    "Provincia",
        "com_regione": "Regione",
        "pop_res":     "Popolazione residente ★",
        "pop_tot":     "Popolazione totale",
        "pop_anno":    "Anno riferimento",
        "com_fonte":   "Fonte del dato",
        "com_note":    "Note",
    }
    for name, label in aliases.items():
        _alias(lyr, name, label)

    # Form a schede
    cfg = lyr.editFormConfig()
    cfg.setLayout(QgsEditFormConfig.EditorLayout.TabLayout)
    root = cfg.invisibleRootContainer()
    root.clear()

    root.addChildElement(_scheda("Comune", [
        "com_nome", "com_prov", "com_regione",
    ]))
    root.addChildElement(_scheda("Popolazione", [
        "pop_res", "pop_tot", "pop_anno", "com_fonte",
    ]))
    root.addChildElement(_scheda("Note", ["com_note"]))

    lyr.setEditFormConfig(cfg)
    lyr.setReadOnly(False)


# ============================================================================
# 2. CREA LAYER ZONIZZAZIONE (dz_k per zona)
# ============================================================================

def crea_layer_zonizzazione(percorso_gpkg=None, crs=None):
    """
    Crea un layer poligonale per la zonizzazione demografica.
    Usato dalla funzione 'Genera CV da OSM' come sorgente del fattore k
    zona per zona (modalità 'Layer zonizzazione').

    Campi obbligatori:
      dz_k          Double   Moltiplicatore k base (da pop. residente)

    Campi opzionali:
      dz_k_extra    Double   k aggiuntivo per flussi extra
                             (turismo, pendolari, eventi stagionali)
      dz_k_modo     String   'somma' (default) o 'massimo' — come combinare k e k_extra
      dz_nome       String   Etichetta descrittiva della zona

    Valori k di riferimento per fascia demografica:
      XS < 2.000 ab.         k = 0.15
      S  2.000–10.000        k = 0.35
      M  10.000–50.000       k = 0.65
      L  50.000–200.000      k = 1.00  (baseline)
      XL 200.000–500.000     k = 1.60
      XXL > 500.000          k = 2.50

    Logica di combinazione k + k_extra:
      somma:   k_fin = dz_k + dz_k_extra - 1.0
      massimo: k_fin = max(dz_k, dz_k_extra)
    """
    LAYER_NAME = "zonizzazione_k"
    _crs = crs if crs is not None else QgsCoordinateReferenceSystem("EPSG:4326")
    _crs_str = _crs.authid()

    fields = QgsFields()
    fields.append(_field("dz_id",     QVariant.Int,    0,  0, "ID automatico"))
    fields.append(_field("dz_nome",   QVariant.String, 120,0, "Etichetta descrittiva della zona"))
    fields.append(_field("dz_k",      QVariant.Double, 0,  3, "Moltiplicatore k base (obbligatorio)"))
    fields.append(_field("dz_k_extra",QVariant.Double, 0,  3, "k aggiuntivo flussi extra (turismo/pendolari/eventi)"))
    fields.append(_field("dz_k_modo", QVariant.String, 12, 0, "somma | massimo — come combinare k e k_extra"))
    fields.append(_field("dz_fascia", QVariant.String, 8,  0, "Fascia demografica (XS/S/M/L/XL/XXL) — solo riferimento"))
    fields.append(_field("dz_ab_rif", QVariant.Int,    0,  0, "Abitanti di riferimento usati per calcolare dz_k"))
    fields.append(_field("dz_fonte",  QVariant.String, 120,0, "Fonte del dato (es. ISTAT 2021, rilievo)"))
    fields.append(_field("dz_note",   QVariant.String, 254,0, "Note libere"))

    err_msg = None
    lyr = None

    if percorso_gpkg:
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName  = LAYER_NAME
        opts.fileEncoding = "UTF-8"

        tmp = QgsVectorLayer("Polygon?crs=" + _crs_str, LAYER_NAME, "memory")
        tmp.dataProvider().addAttributes(fields.toList())
        tmp.updateFields()

        err = QgsVectorFileWriter.writeAsVectorFormatV3(
            tmp, percorso_gpkg, tmp.transformContext(), opts
        )
        if err[0] != QgsVectorFileWriter.WriterError.NoError:
            return None, f"Errore scrittura GeoPackage: {err[1]}"
        lyr = QgsVectorLayer(percorso_gpkg + "|layername=" + LAYER_NAME,
                             LAYER_NAME, "ogr")
        if not lyr.isValid():
            return None, "Layer zonizzazione non valido dopo la scrittura"
    else:
        lyr = QgsVectorLayer("Polygon?crs=" + _crs_str, LAYER_NAME, "memory")
        lyr.dataProvider().addAttributes(fields.toList())
        lyr.updateFields()

    _configura_form_zonizzazione(lyr)
    _attiva_editing_layer(lyr)
    return lyr, err_msg


def _configura_form_zonizzazione(lyr):
    fi = lambda n: lyr.fields().indexOf(n)

    # Widget editor
    _editor(lyr, "dz_id",    "Hidden", {})
    _editor(lyr, "dz_nome",  "TextEdit", {"IsMultiline": False})
    _editor(lyr, "dz_k", "ValueMap", {"map": [
        {"0.05 — XXS  (< 500 ab.)": 0.05},
        {"0.15 — XS   (500 – 2.000 ab.)": 0.15},
        {"0.35 — S    (2.000 – 10.000 ab.)": 0.35},
        {"0.65 — M-   (10.000 – 30.000 ab.)": 0.65},
        {"1.00 — M    (30.000 – 80.000 ab.) ★ baseline": 1.0},
        {"1.40 — M+   (80.000 – 200.000 ab.)": 1.4},
        {"1.80 — L    (200.000 – 500.000 ab.)": 1.8},
        {"2.50 — XL   (500.000 – 2.000.000 ab.)": 2.5},
        {"3.50 — XXL  (> 2.000.000 ab.)": 3.5},
    ]})
    _editor(lyr, "dz_k_extra", "ValueMap", {"map": [
        {"0.00 — nessun flusso extra": 0.0},
        {"0.15 — flusso extra minimo": 0.15},
        {"0.35 — flusso extra lieve": 0.35},
        {"0.65 — flusso extra moderato": 0.65},
        {"1.00 — flusso extra equivalente ai residenti": 1.0},
        {"1.40 — flusso extra elevato (turismo stagionale)": 1.4},
        {"1.80 — flusso extra intenso": 1.8},
        {"2.50 — flusso extra molto elevato (grande meta turistica)": 2.5},
        {"3.50 — flusso extra metropoli turistica": 3.5},
    ]})
    _editor(lyr, "dz_k_modo", "ValueMap", {"map": [
        {"somma — k = dz_k + dz_k_extra - 1.0  (flussi sovrapposti, es. turismo + residenti)": "somma"},
        {"massimo — k = max(dz_k, dz_k_extra)  (eventi discontinui, es. fiere, partite)": "massimo"},
    ]})
    _editor(lyr, "dz_fascia", "ValueMap", {"map": [
        {"XXS — < 500 ab.              →  k = 0.05": "XXS"},
        {"XS  — 500–2.000 ab.          →  k = 0.15": "XS"},
        {"S   — 2.000–10.000 ab.       →  k = 0.35": "S"},
        {"M-  — 10.000–30.000 ab.      →  k = 0.65": "M-"},
        {"M   — 30.000–80.000 ab.      →  k = 1.00 (baseline)": "M"},
        {"M+  — 80.000–200.000 ab.     →  k = 1.40": "M+"},
        {"L   — 200.000–500.000 ab.    →  k = 1.80": "L"},
        {"XL  — 500.000–2.000.000 ab.  →  k = 2.50": "XL"},
        {"XXL — > 2.000.000 ab.        →  k = 3.50": "XXL"},
    ]})
    _editor(lyr, "dz_ab_rif","Range", {
        "Min": 0, "Max": 20000000, "Step": 100,
        "Suffix": " ab. (riferimento)", "Style": "SpinBox"
    })
    _editor(lyr, "dz_fonte",  "TextEdit", {"IsMultiline": False})
    _editor(lyr, "dz_note",   "TextEdit", {"IsMultiline": True})

    # Default
    _default(lyr, "dz_k",      "1.0")
    _default(lyr, "dz_k_extra","0.0")
    _default(lyr, "dz_k_modo", "'somma'", True)
    _default(lyr, "dz_fonte",  "'ISTAT'")

    # Alias
    aliases = {
        "dz_id":      "ID",
        "dz_nome":    "Nome zona",
        "dz_k":       "Moltiplicatore k base ★",
        "dz_k_extra": "k extra (turismo / eventi)",
        "dz_k_modo":  "Modalità combinazione",
        "dz_fascia":  "Fascia demografica",
        "dz_ab_rif":  "Abitanti di riferimento",
        "dz_fonte":   "Fonte del dato",
        "dz_note":    "Note",
    }
    for name, label in aliases.items():
        _alias(lyr, name, label)

    # Form a schede
    cfg = lyr.editFormConfig()
    cfg.setLayout(QgsEditFormConfig.EditorLayout.TabLayout)
    root = cfg.invisibleRootContainer()
    root.clear()

    root.addChildElement(_scheda("Zona", ["dz_nome", "dz_fascia", "dz_ab_rif"]))
    root.addChildElement(_scheda("Fattore k", [
        "dz_k", "dz_k_extra", "dz_k_modo",
    ]))
    root.addChildElement(_scheda("Fonte", ["dz_fonte", "dz_note"]))

    lyr.setEditFormConfig(cfg)
    lyr.setReadOnly(False)


# ============================================================================
# ALGORITMI PROCESSING
# ============================================================================

class AreteCreaComuniAlgorithm(QgsProcessingAlgorithm):

    OUTPUT = "OUTPUT"

    def name(self):        return "crea_layer_comuni"
    def displayName(self): return "Crea layer Comuni (popolazione per fattore k)"
    def group(self):       return "QgisTreeRisk"
    def groupId(self):     return "qgistreerisck"
    def shortHelpString(self):
        return (
            "<b>Crea un layer poligonale per i confini comunali</b> con il campo "
            "popolazione (<code>pop_res</code>), pronto per essere usato come "
            "sorgente del <b>fattore k demografico</b> nella funzione "
            "<i>Genera Carta Vulnerabilità da OSM</i>.<br><br>"
            "<b>Campo principale:</b> <code>pop_res</code> — popolazione residente.<br>"
            "Il plugin lo cerca automaticamente tra i campi del layer "
            "(alias accettati: pop, abitanti, resid, tot_res, pop_res).<br><br>"
            "<b>Procedura consigliata:</b><br>"
            "1. Crea il layer con questo algoritmo.<br>"
            "2. Digitalizza i confini comunali oppure incolla geometrie da ISTAT.<br>"
            "3. Compila <code>pop_res</code> con la popolazione residente.<br>"
            "4. Usa il layer come sorgente in <i>Genera CV da OSM → "
            "Sorgente k → Layer comuni</i>."
        )

    def createInstance(self):
        return AreteCreaComuniAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "GeoPackage di destinazione",
                fileFilter="GeoPackage (*.gpkg)",
                optional=True,
            )
        )
        self.addOutput(
            QgsProcessingOutputVectorLayer(self.OUTPUT, "Layer comuni")
        )

    def processAlgorithm(self, parameters, context, feedback):
        gpkg = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        lyr, err = crea_layer_comuni(gpkg or None)
        if err:
            feedback.reportError(err, True)
            return {}

        from qgis.core import QgsProject
        QgsProject.instance().addMapLayer(lyr)

        feedback.pushInfo("Layer comuni aggiunto al progetto: " + lyr.name())
        feedback.pushInfo("Compila il campo 'pop_res' con la popolazione residente.")
        feedback.pushInfo(
            "Poi usa questo layer in: "
            "Genera CV da OSM → Sorgente k → Layer comuni"
        )
        return {self.OUTPUT: lyr.id()}


class AreteCreaZonizzazioneAlgorithm(QgsProcessingAlgorithm):

    OUTPUT = "OUTPUT"

    def name(self):        return "crea_layer_zonizzazione"
    def displayName(self): return "Crea layer Zonizzazione demografica (fattore k)"
    def group(self):       return "QgisTreeRisk"
    def groupId(self):     return "qgistreerisck"
    def shortHelpString(self):
        return (
            "<b>Crea un layer poligonale per la zonizzazione demografica</b> con "
            "tutti i campi necessari per la modulazione del fattore k zona per zona "
            "nella funzione <i>Genera Carta Vulnerabilità da OSM</i>.<br><br>"
            "<b>Campi principali:</b><br>"
            "• <code>dz_k</code> ★ — moltiplicatore base (obbligatorio)<br>"
            "• <code>dz_k_extra</code> — flussi extra: turismo, pendolari, eventi<br>"
            "• <code>dz_k_modo</code> — 'somma' o 'massimo' (come combinare k e k_extra)<br>"
            "• <code>dz_nome</code> — etichetta descrittiva<br><br>"
            "<b>Valori k di riferimento:</b><br>"
            "XS &lt;2k → 0.15 | S 2k-10k → 0.35 | M 10k-50k → 0.65<br>"
            "L 50k-200k → 1.00 (baseline) | XL → 1.60 | XXL → 2.50<br><br>"
            "<b>Procedura consigliata:</b><br>"
            "1. Crea il layer con questo algoritmo.<br>"
            "2. Digitalizza le zone (es. centro storico, periferia, area turistica).<br>"
            "3. Compila <code>dz_k</code> e, se necessario, <code>dz_k_extra</code>.<br>"
            "4. Usa il layer in <i>Genera CV da OSM → "
            "Sorgente k → Layer zonizzazione</i>."
        )

    def createInstance(self):
        return AreteCreaZonizzazioneAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "GeoPackage di destinazione",
                fileFilter="GeoPackage (*.gpkg)",
                optional=True,
            )
        )
        self.addOutput(
            QgsProcessingOutputVectorLayer(self.OUTPUT, "Layer zonizzazione")
        )

    def processAlgorithm(self, parameters, context, feedback):
        gpkg = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        lyr, err = crea_layer_zonizzazione(gpkg or None)
        if err:
            feedback.reportError(err, True)
            return {}

        from qgis.core import QgsProject
        QgsProject.instance().addMapLayer(lyr)

        feedback.pushInfo("Layer zonizzazione aggiunto al progetto: " + lyr.name())
        feedback.pushInfo("Campi obbligatori: dz_k (★)")
        feedback.pushInfo("Campi opzionali: dz_k_extra, dz_k_modo, dz_nome")
        feedback.pushInfo(
            "Poi usa questo layer in: "
            "Genera CV da OSM → Sorgente k → Layer zonizzazione"
        )
        return {self.OUTPUT: lyr.id()}
