# -*- coding: utf-8 -*-
"""
Arete VRA - Processing Provider
Registra il gruppo "Arete" nella galleria Processing di QGIS.
"""

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
import os


class AreteVRAProvider(QgsProcessingProvider):

    def id(self):
        return "arete"

    def name(self):
        return "QgisTreeRisk"

    def longName(self):
        return "Protocollo Arete v4.0 - Valutazione Rischio Arboreo"

    def icon(self):
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_path):
            return QIcon(icon_path)
        return super().icon()

    def loadAlgorithms(self):
        from .algorithm_dialog    import AreteVRADialogAlgorithm
        from .algorithm_cv        import AreteCreaCVAlgorithm
        from .algorithm_osm_cv    import AreteGeneraCVdaOSMAlgorithm
        from .algorithm_layers_aux import (
            AreteCreaComuniAlgorithm,
            AreteCreaZonizzazioneAlgorithm,
        )
        self.addAlgorithm(AreteVRADialogAlgorithm())
        self.addAlgorithm(AreteCreaCVAlgorithm())
        self.addAlgorithm(AreteGeneraCVdaOSMAlgorithm())
        self.addAlgorithm(AreteCreaComuniAlgorithm())
        self.addAlgorithm(AreteCreaZonizzazioneAlgorithm())
