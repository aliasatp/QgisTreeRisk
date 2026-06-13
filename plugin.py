# -*- coding: utf-8 -*-
"""
Arete VRA - Plugin main class
"""

from qgis.core import QgsApplication


class AreteVRAPlugin:

    def __init__(self, iface):
        self.iface    = iface
        self.provider = None

    def initGui(self):
        from .provider import AreteVRAProvider
        self.provider = AreteVRAProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
