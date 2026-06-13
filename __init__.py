# -*- coding: utf-8 -*-
"""
Arete VRA Plugin - entry point QGIS
"""

def classFactory(iface):
    from .plugin import AreteVRAPlugin
    return AreteVRAPlugin(iface)
