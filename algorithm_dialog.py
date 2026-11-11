# -*- coding: utf-8 -*-
"""
Arete VRA - Algoritmo "Dialog interattivo"
Appare nella galleria Processing come:
  Arete VRA > Valutazione Rischio Arboreo (Dialog interattivo)

Quando viene eseguito apre il VRADialog con QgsMapLayerComboBox
e dropdown per la mappatura dei campi - identico all'esecuzione
da Editor di Script, ma accessibile dalla galleria Processing.
"""

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessing,
)
from qgis.PyQt.QtCore import QCoreApplication
import os


class AreteVRADialogAlgorithm(QgsProcessingAlgorithm):

    INPUT = "INPUT"

    def name(self):
        return "arete_vra_dialog"

    def displayName(self):
        return "Valutazione Rischio Arboreo (Dialog interattivo)"

    def group(self):
        return "QgisTreeRisk"

    def groupId(self):
        return "qgistreerisck"

    def shortHelpString(self):
        return (
            "Apre il dialog interattivo per la Valutazione del Rischio "
            "Arboreo secondo il Protocollo Arete v4.0.\n\n"
            "Il dialog dispone di:\n"
            "  - QgsMapLayerComboBox per selezionare il layer alberi\n"
            "  - Dropdown per mappare i campi del layer ai parametri\n"
            "    (h, d_ch, circonf, h_bers, d_br, l_br, h_ins)\n"
            "  - Auto-abbinamento automatico per nome canonico\n"
            "  - Calcolo separato Bersaglio ALBERO e BRANCA\n"
            "  - Stima da OpenStreetMap (Overpass API)\n\n"
            "RAGGI OSM:\n"
            "  Albero: raggio = altezza albero\n"
            "  Branca: raggio = diametro chioma / 2\n\n"
            "Riferimento: Protocollo Arete(r) v4.0 - ARBORETE(r)\n"
            "www.protocolloarete.it - CC BY-NC-ND 4.0"
        )

    def createInstance(self):
        return AreteVRADialogAlgorithm()

    def initAlgorithm(self, config=None):
        # Nessun parametro: il dialog gestisce tutto internamente
        pass

    def flags(self):
        # NON mostrare il form parametri standard di Processing
        # Esegue direttamente il codice senza dialog parametri
        return (
            super().flags()
            | QgsProcessingAlgorithm.FlagNoThreading
            | QgsProcessingAlgorithm.FlagNotAvailableInStandaloneTool
        )

    def processAlgorithm(self, parameters, context, feedback):
        """
        Apre il VRADialog invece di eseguire un calcolo batch.
        Viene chiamato quando l'utente preme 'Esegui' nella galleria.
        """
        try:
            from qgis.utils import iface
            from .vra_dialog import VRADialog
            dlg = VRADialog(iface.mainWindow())
            dlg.setModal(False)
            dlg.show()
            # Mantiene il riferimento per evitare garbage collection
            iface._arete_vra_dlg = dlg
        except Exception as ex:
            feedback.reportError("Errore apertura dialog: " + str(ex), True)
        return {}
