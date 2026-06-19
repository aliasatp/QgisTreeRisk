# -*- coding: utf-8 -*-
"""
Arete VRA - Algoritmo "Crea Carta Vulnerabilita'"
Appare nella galleria Processing come:
  Arete VRA > Crea Carta Vulnerabilita' (layer poligonale)

Genera un GeoPackage con lo schema standard Areté per la Carta della
Vulnerabilità, pronto per essere digitalizzato dall'utente.
"""

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterCrs,
    QgsProcessingOutputVectorLayer,
    QgsProject,
)
from qgis.PyQt.QtCore import QCoreApplication


class AreteCreaCVAlgorithm(QgsProcessingAlgorithm):

    OUTPUT = "OUTPUT"

    def name(self):
        return "arete_crea_carta_vulnerabilita"

    def displayName(self):
        return "Crea Carta Vulnerabilita' (layer poligonale)"

    def group(self):
        return "QgisTreeRisk"

    def groupId(self):
        return "qgistreerisck"

    def shortHelpString(self):
        return (
            "Crea un layer poligonale GeoPackage con lo schema standard "
            "Areté v4.0 per la <b>Carta della Vulnerabilità</b>.<br><br>"
            "Il layer generato e' pronto per la digitalizzazione in QGIS. "
            "Ogni poligono rappresenta una zona omogenea di bersaglio "
            "e contiene i campi:<br>"
            "<ul>"
            "<li><b>cv_nome</b>: nome descrittivo (es. 'Via Roma', 'Parco Nord')</li>"
            "<li><b>cv_tipo</b>: veicolare | pedonale | occupazione | manufatto | misto</li>"
            "<li><b>cv_prob</b>: probabilita di presenza 1=raro ... 5=sempre</li>"
            "<li><b>cv_vei_g</b>: veicoli/giorno</li>"
            "<li><b>cv_vel_kmh</b>: velocita di riferimento km/h</li>"
            "<li><b>cv_ped_g</b>: pedoni+ciclisti/giorno</li>"
            "<li><b>cv_ore_occ</b>: ore/giorno di occupazione stabile</li>"
            "<li><b>cv_valore_eu</b>: valore manufatto in euro</li>"
            "<li><b>cv_b_alb</b>: classe B albero gia' nota (0=calcola automatico)</li>"
            "<li><b>cv_b_bra</b>: classe B branca gia' nota (0=calcola automatico)</li>"
            "<li><b>cv_fonte</b>: fonte del dato</li>"
            "<li><b>cv_note</b>: note libere</li>"
            "</ul>"
            "Il layer viene aggiunto al progetto QGIS corrente.<br><br>"
            "Riferimento: Protocollo Arete(r) v4.0 - Allegato 3"
        )

    def createInstance(self):
        return AreteCreaCVAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterCrs(
                "CRS_OUTPUT",
                "CRS di output (default: UTM32N EPSG:32632)",
                defaultValue="EPSG:32632",
                optional=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterMapLayer(
                "LAYER_RIF",
                "Layer alberi (sovrascrive il CRS se fornito, opzionale)",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "GeoPackage di destinazione",
                fileFilter="GeoPackage (*.gpkg)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import os
        gpkg_path = self.parameterAsFileOutput(parameters, self.OUTPUT, context)

        # Normalizza il percorso
        gpkg_path = gpkg_path.replace("\\", "/")
        if not gpkg_path.lower().endswith(".gpkg"):
            gpkg_path += ".gpkg"

        feedback.pushInfo("Creazione Carta Vulnerabilita'...")
        feedback.pushInfo("Percorso: " + gpkg_path)

        # Verifica che la cartella esista
        cartella = os.path.dirname(gpkg_path)
        if cartella and not os.path.isdir(cartella):
            feedback.reportError(
                "La cartella di destinazione non esiste: " + cartella, True
            )
            return {}

        from .arete_engine import crea_layer_cv, crs_utm_da_layer
        from qgis.core import QgsCoordinateReferenceSystem

        # CRS: priorità a layer alberi se fornito, poi parametro CRS, poi 32632
        lyr_rif = self.parameterAsLayer(parameters, "LAYER_RIF", context)
        if lyr_rif:
            crs_out = crs_utm_da_layer(lyr_rif)
            feedback.pushInfo("CRS da layer alberi: " + crs_out.authid())
        else:
            crs_out = self.parameterAsCrs(parameters, "CRS_OUTPUT", context)
            if not crs_out.isValid():
                crs_out = QgsCoordinateReferenceSystem("EPSG:32632")
            feedback.pushInfo("CRS output: " + crs_out.authid())

        lyr, err = crea_layer_cv(gpkg_path, crs=crs_out)

        if err:
            feedback.reportError(err, True)
            return {}

        QgsProject.instance().addMapLayer(lyr)

        # Inizializza editing + indice spaziale (fix snap/selezione/salva)
        if lyr.providerType() == "ogr":
            lyr.startEditing()
            lyr.commitChanges()
            prov = lyr.dataProvider()
            if prov:
                prov.createSpatialIndex()
            lyr.updateExtents()

        feedback.pushInfo("OK - layer aggiunto al progetto QGIS.")
        feedback.pushInfo("")
        feedback.pushInfo("PROSSIMI PASSI:")
        feedback.pushInfo("  1. Seleziona il layer 'carta_vulnerabilita' nel pannello Layer")
        feedback.pushInfo("  2. Attiva la modalita' modifica (icona matita)")
        feedback.pushInfo("  3. Disegna i poligoni di vulnerabilita' con Aggiungi feature")
        feedback.pushInfo("  4. Compila i campi nel form attributi:")
        feedback.pushInfo("     cv_tipo  : veicolare | pedonale | occupazione | manufatto | misto")
        feedback.pushInfo("     cv_prob  : 1=raro  2=occasionale  3=frequente  4=spesso  5=sempre")
        feedback.pushInfo("     cv_vei_g : veicoli/giorno (0 se non applicabile)")
        feedback.pushInfo("     cv_vel_kmh: velocita' di riferimento km/h")
        feedback.pushInfo("     cv_ped_g : pedoni+ciclisti/giorno")
        feedback.pushInfo("     cv_ore_occ: ore/giorno occupazione stabile")
        feedback.pushInfo("     cv_valore_eu: valore manufatto in euro")
        feedback.pushInfo("     cv_b_alb/cv_b_bra: 0 = calcola automatico, 1-7 = imponi classe")
        feedback.pushInfo("")
        feedback.pushInfo(
            "Nel dialog VRA (Tab 1) seleziona 'Carta Vulnerabilita'' "
            "e scegli questo layer."
        )

        return {self.OUTPUT: gpkg_path}
