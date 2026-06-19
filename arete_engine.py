# -*- coding: utf-8 -*-
"""
Arete VRA - Motore di calcolo (arete_engine.py)
Condiviso da algorithm_dialog.py, algorithm_batch.py e dal dialog interattivo.
Contiene: costanti, formule bersaglio, classe fisica, rischio, OSM, helpers.
Riferimento: Protocollo Arete(r) v4.0 - ARBORETE(r) - CC BY-NC-ND 4.0
"""

# -*- coding: utf-8 -*-
"""
Protocollo Arete v4.0 - Valutazione Rischio Arboreo
DIALOG INTERATTIVO con dropdown per la mappatura dei campi del layer.

USO:
  Aprire dall'Editor di Script di QGIS (Menu Elaborazione > Editor di Script)
  e premere Esegui (triangolo verde).
  Si apre il dialog con 4 tab:
    Tab 1 - Layer: seleziona il layer puntuale (QgsMapLayerComboBox)
                   i dropdown si aggiornano automaticamente
    Tab 2 - Albero: mappa h, d_ch, circonf, h_bers + POF
    Tab 3 - Branca: mappa d_br, l_br, h_ins + POF
    Tab 4 - Esegui

NON installare nella cartella processing/scripts/ altrimenti
QGIS apre la toolbox al posto del dialog.

Riferimento: Protocollo Arete(r) v4.0 - ARBORETE(r)
  www.protocolloarete.it - CC BY-NC-ND 4.0
"""

# -*- coding: utf-8 -*-
"""
Protocollo Arete v4.0 - Valutazione Rischio Arboreo per QGIS 4.0
Stima Bersaglio da OSM con formule identiche al simulatore HTML ufficiale.

LOGICA GEOMETRICA DEL BERSAGLIO (Allegato 3 - Protocollo Arete v4.0):
  - Albero intero (radici/colletto/fusto):
      raggio ricerca OSM = altezza albero (h_m)
      dimensione area pericolosa = media(h_m, d_chioma_m)
  - Branca pericolosa:
      raggio ricerca OSM = d_chioma_m / 2 (raggio chioma)
      dimensione area pericolosa = l_branca_m * 1.25

CAMPI OBBLIGATORI nel layer alberi:
  h_m          Altezza albero (m)
  d_chioma_m   Diametro chioma (m)
  circonf_cm   Circonferenza a 130 cm (cm)
  h_bers_m     Altezza bersaglio (m)
  d_branca_cm  Diametro branca pericolosa (cm)
  l_branca_m   Lunghezza branca pericolosa (m)
  h_ins_m      Inserzione branca (m)

CAMPI FACOLTATIVI per POF/moltiplicatore (altrimenti si usano i default del dialog):
  pof_radici, pof_colletto, pof_fusto, pof_branche  (1-7, 9=assente, 0=appr.)
  molt         Moltiplicatore (0-7)
  B_manuale    Classe bersaglio imposta manualmente (1-7, sovrascrive OSM)
  B_cat_manuale Categoria bersaglio (stringa)

INSTALLAZIONE nella libreria Processing QGIS 4.0:
  Copia in: %APPDATA%/QGIS/QGIS3/profiles/default/processing/scripts/  (Win)
            ~/.local/share/QGIS/QGIS3/profiles/default/processing/scripts/ (Lin)
  Poi: Elaborazione > Aggiorna provider

Riferimento: Protocollo Arete(r) v4.0 - ARBORETE(r)
  www.protocolloarete.it - CC BY-NC-ND 4.0
"""

from qgis.PyQt.QtCore import QVariant, QThread, pyqtSignal

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsField, QgsFeature,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform,
    QgsMarkerSymbol, QgsCategorizedSymbolRenderer, QgsRendererCategory,
)

import math

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

import json
import urllib.parse


# ===========================================================================
# 1. COSTANTI E TABELLE ARETE v4.0
# ===========================================================================

# Metadati parametri biometrici usati da VRADialog (vra_dialog.py)
# (chiave_params, nome_canonico_layer, etichetta, unita, default)
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



# ---------------------------------------------------------------------------
# RISCHIO_TAB: lookup diretto B+CF+POF -> livello di rischio.
# Fonte autorevole: arete.xlsx (TAB ufficiale Protocollo Arete v4.0).
# Sostituisce il calcolo formula B_w*CF_w*POF_w che soffre di errori
# floating-point sulle soglie di confine.
# cod = str(B) + str(CF) + str(POF)
RISCHIO_TAB = {
    '110': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '111': {'r':'1:3','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.3333333333333333},
    '112': {'r':'1:20','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.05},
    '113': {'r':'1:200','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.005},
    '114': {'r':'1:2k','gr':5,'giudizio':'RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#eb130c','d':0.0005},
    '115': {'r':'1:20k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':5e-05},
    '116': {'r':'1:200k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':5e-06},
    '117': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '120': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '121': {'r':'1:20','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.05},
    '122': {'r':'1:100','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.01},
    '123': {'r':'1:1k','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.001},
    '124': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '125': {'r':'1:130k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':7.692307692307692e-06},
    '126': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '127': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '130': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '131': {'r':'1:200','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.005},
    '132': {'r':'1:1k','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.001},
    '133': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '134': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '135': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '136': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '137': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '140': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '141': {'r':'1:2k','gr':5,'giudizio':'RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#eb130c','d':0.0005},
    '142': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '143': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '144': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '145': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '146': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '147': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '150': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '151': {'r':'1:20k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':5e-05},
    '152': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '153': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '154': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '155': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '156': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '157': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '160': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '161': {'r':'1:200k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':5e-06},
    '162': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '163': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '164': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '165': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '166': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '167': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '170': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '171': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '172': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '173': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '174': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '175': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '176': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '177': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '210': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '211': {'r':'1:20','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.05},
    '212': {'r':'1:100','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.01},
    '213': {'r':'1:1k','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.001},
    '214': {'r':'1:10k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':0.0001},
    '215': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '216': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '217': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '220': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '221': {'r':'1:100','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.01},
    '222': {'r':'1:800','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.00125},
    '223': {'r':'1:8k','gr':5,'giudizio':'RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#eb130c','d':0.000125},
    '224': {'r':'1:80k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':1.25e-05},
    '225': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '226': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '227': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '230': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '231': {'r':'1:1k','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.001},
    '232': {'r':'1:8k','gr':5,'giudizio':'RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#eb130c','d':0.000125},
    '233': {'r':'1:80k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':1.25e-05},
    '234': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '235': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '236': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '237': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '240': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '241': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '242': {'r':'1:80k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':1.25e-05},
    '243': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '244': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '245': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '246': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '247': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '250': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '251': {'r':'1:130k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':7.692307692307692e-06},
    '252': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '253': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '254': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '255': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '256': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '257': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '260': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '261': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '262': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '263': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '264': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '265': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '266': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '267': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '270': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '271': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '272': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '273': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '274': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '275': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '276': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '277': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '310': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '311': {'r':'1:200','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.005},
    '312': {'r':'1:1k','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.001},
    '313': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '314': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '315': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '316': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '317': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '320': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '321': {'r':'1:1k','gr':6,'giudizio':'RISCHIO INACETTABILE','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#ca09e8','d':0.001},
    '322': {'r':'1:8k','gr':5,'giudizio':'RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#eb130c','d':0.000125},
    '323': {'r':'1:80k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':1.25e-05},
    '324': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '325': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '326': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '327': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '330': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '331': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '332': {'r':'1:80k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':1.25e-05},
    '333': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '334': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '335': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '336': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '337': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '340': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '341': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '342': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '343': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '344': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '345': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '346': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '347': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '350': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '351': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '352': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '353': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '354': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '355': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '356': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '357': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '360': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '361': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '362': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '363': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '364': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '365': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '366': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '367': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '370': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '371': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '372': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '373': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '374': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '375': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '376': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '377': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '410': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '411': {'r':'1:2k','gr':5,'giudizio':'RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA','speditiva':'RISOLUZIONE DELL\'EMERGENZA','colore':'#eb130c','d':0.0005},
    '412': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '413': {'r':'1:130k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':7.692307692307692e-06},
    '414': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '415': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '416': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '417': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '420': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '421': {'r':'1:12k','gr':4,'giudizio':'RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI','speditiva':'VALUTAZIONE URGENTE','colore':'#f0ab18','d':8.333333333333333e-05},
    '422': {'r':'1:80k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':1.25e-05},
    '423': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '424': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '425': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '426': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '427': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '430': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '431': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '432': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '433': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '434': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '435': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '436': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '437': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '440': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '441': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '442': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '443': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '444': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '445': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '446': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '447': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '450': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '451': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '452': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '453': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '454': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '455': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '456': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '457': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '460': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '461': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '462': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '463': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '464': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '465': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '466': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '467': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '470': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '471': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '472': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '473': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '474': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '475': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '476': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '477': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '510': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '511': {'r':'1:20k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':5e-05},
    '512': {'r':'1:120k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':8.333333333333334e-06},
    '513': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '514': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '515': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '516': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '517': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '520': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '521': {'r':'1:130k','gr':3,'giudizio':'RISCHIO TOLLERABILE SE ALARP','speditiva':'VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO','colore':'#edea13','d':7.692307692307692e-06},
    '522': {'r':'1:800k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1.25e-06},
    '523': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '524': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '525': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '526': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '527': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '530': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '531': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '532': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '533': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '534': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '535': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '536': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '537': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '540': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '541': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '542': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '543': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '544': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '545': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '546': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '547': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '550': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '551': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '552': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '553': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '554': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '555': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '556': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '557': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '560': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '561': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '562': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '563': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '564': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '565': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '566': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '567': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '570': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '571': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '572': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '573': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '574': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '575': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '576': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '577': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '610': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '611': {'r':'1:200k','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':5e-06},
    '612': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '613': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '614': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '615': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '616': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '617': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '620': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '621': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '622': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '623': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '624': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '625': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '626': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '627': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '630': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '631': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '632': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '633': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '634': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '635': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '636': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '637': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '640': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '641': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '642': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '643': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '644': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '645': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '646': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '647': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '650': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '651': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '652': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '653': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '654': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '655': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '656': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '657': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '660': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '661': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '662': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '663': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '664': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '665': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '666': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '667': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '670': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '671': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '672': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '673': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '674': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '675': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '676': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '677': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '710': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '711': {'r':'1:1M','gr':2,'giudizio':'RISCHIO TOLLERABILE','speditiva':'VALUTAZIONE OPPORTUNA MA NON URGENTE','colore':'#16e813','d':1e-06},
    '712': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '713': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '714': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '715': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '716': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '717': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '720': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '721': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '722': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '723': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '724': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '725': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '726': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '727': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '730': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '731': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '732': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '733': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '734': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '735': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '736': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '737': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '740': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '741': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '742': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '743': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '744': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '745': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '746': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '747': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '750': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '751': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '752': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '753': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '754': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '755': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '756': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '757': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '760': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '761': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '762': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '763': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '764': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '765': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '766': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '767': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '770': {'r':'N.D.','gr':7,'giudizio':'VALUTAZIONE SOSPESA','speditiva':'APPROFONDIMENTO','colore':'#9fa0a6','d':0.0},
    '771': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '772': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '773': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '774': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '775': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '776': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
    '777': {'r':'<1:1M','gr':1,'giudizio':'RISCHIO LARGAMENTE ACCETTABILE','speditiva':'VALUTAZIONE PROCRASTINABILE','colore':'#00b0f0','d':1e-11},
}
VELOCITA_STRADA = {
    "motorway": 130, "trunk": 110, "primary": 90, "secondary": 70,
    "tertiary": 50, "unclassified": 50, "residential": 30,
    "service": 30, "living_street": 10, "cycleway": 0,
    "footway": 0, "path": 0, "track": 30, "default": 50,
}

CATEGORIA_ARETE = {
    "motorway": "autostrada", "trunk": "statale", "primary": "statale",
    "secondary": "provinciale", "tertiary": "comunale",
    "unclassified": "comunale", "residential": "comunale",
    "service": "vicinale", "living_street": "vicinale", "default": "comunale",
}

FLUSSI_TIPICI = {
    "motorway": 50000, "trunk": 20000, "primary": 10000,
    "secondary": 5000, "tertiary": 2000, "unclassified": 1000,
    "residential": 500, "service": 200, "living_street": 50,
    "cycleway": 0, "footway": 0, "path": 0, "track": 30, "default": 1000,
}

LANDUSE_ORE = {
    "park": 5.0, "recreation_ground": 4.0, "playground": 4.0,
    "grass": 2.0, "garden": 3.0, "school": 8.0, "university": 8.0,
    "hospital": 12.0, "cemetery": 1.0, "retail": 8.0,
    "commercial": 8.0, "industrial": 8.0, "residential": 5.0,
    "farmland": 6.0, "forest": 1.0, "meadow": 1.0,
    "allotments": 3.0, "sports_centre": 6.0, "stadium": 4.0,
    "pitch": 4.0, "pedestrian": 8.0, "plaza": 6.0, "default": 2.0,
}

PEDONI_MOLT_LANDUSE = {
    "park": 5.0, "recreation_ground": 4.0, "playground": 4.0,
    "pedestrian": 8.0, "retail": 6.0, "commercial": 5.0,
    "school": 4.0, "university": 4.0, "hospital": 3.0,
    "sports_centre": 3.0, "stadium": 5.0, "pitch": 3.0, "residential": 2.0,
}

POF_VALORI = {
    1: 1.0 / 3,
    2: 1.0 / 20,
    3: 1.0 / 100,
    4: 1.0 / 200,
    5: 1.0 / 1000,
    6: 1.0 / 10000,
    7: 1.0 / 1000000,
    9: 0.0,
    0: None,
}

# Pesi classe fisica (CF) sul rischio.
# Derivati dalla tabella TAB del simulatore Arete HTML ufficiale.
# CF_PESO[k] = letalita' normalizzata per la classe k:
#   CF1 (E>10000 J): 1.0
#   CF2 (1000-10000 J): 0.15  (= 3/20)
#   CF3 (100-1000 J):   0.015 (= 3/200)
#   CF4 (50-100 J):     0.0015
#   CF5 (10-50 J):      3/13000 ~= 0.000231
#   CF6 (5-10 J):       1/43333 ~= 0.0000231
#   CF7 (<5 J):         1/433333 ~= 0.00000231
CF_PESO = {
    0: 0.0,
    1: 1.0,
    2: 3.0 / 20.0,
    3: 3.0 / 200.0,
    4: 3.0 / 2000.0,
    5: 3.0 / 13000.0,
    6: 3.0 / 130000.0,
    7: 3.0 / 1300000.0,
}

B_PESO = {
    1: 1.0, 2: 0.1, 3: 0.01, 4: 0.001,
    5: 0.0001, 6: 0.00001, 7: 0.000001, 9: 0.0,
}

SOGLIE_RISCHIO = sorted([
    # (soglia_d, ratio, colore, giudizio, speditiva, gravita)
    # d >= soglia -> questo livello di rischio
    # Ordinate DECRESCENTE: la prima soglia che d supera e' il livello corretto
    (1.0 / 3,        "1:3",    "#ca09e8",
     "RISCHIO INACCETTABILE",
     "RISOLUZIONE DELL EMERGENZA", 6),
    (1.0 / 20,       "1:20",   "#ca09e8",
     "RISCHIO INACCETTABILE",
     "RISOLUZIONE DELL EMERGENZA", 6),
    (1.0 / 100,      "1:100",  "#ca09e8",
     "RISCHIO INACCETTABILE",
     "RISOLUZIONE DELL EMERGENZA", 6),
    (1.0 / 200,      "1:200",  "#ca09e8",
     "RISCHIO INACCETTABILE",
     "RISOLUZIONE DELL EMERGENZA", 6),
    (1.0 / 800,      "1:800",  "#ca09e8",
     "RISCHIO INACCETTABILE",
     "RISOLUZIONE DELL EMERGENZA", 6),
    (1.0 / 1000,     "1:1k",   "#ca09e8",
     "RISCHIO INACCETTABILE",
     "RISOLUZIONE DELL EMERGENZA", 6),
    (1.0 / 2000,     "1:2k",   "#eb130c",
     "RISCHIO TOLLERABILE PER ACCORDO SE IL VALORE E MOLTO ELEVATO",
     "RISOLUZIONE DELL EMERGENZA", 5),
    (1.0 / 8000,     "1:8k",   "#eb130c",
     "RISCHIO TOLLERABILE PER ACCORDO SE IL VALORE E MOLTO ELEVATO",
     "RISOLUZIONE DELL EMERGENZA", 5),
    (1.0 / 10000,    "1:10k",  "#f0ab18",
     "RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI",
     "VALUTAZIONE URGENTE", 4),
    (1.0 / 12000,    "1:12k",  "#f0ab18",
     "RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI",
     "VALUTAZIONE URGENTE", 4),
    (1.0 / 20000,    "1:20k",  "#edea13",
     "RISCHIO TOLLERABILE SE ALARP",
     "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
    (1.0 / 80000,    "1:80k",  "#edea13",
     "RISCHIO TOLLERABILE SE ALARP",
     "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
    (1.0 / 120000,   "1:120k", "#edea13",
     "RISCHIO TOLLERABILE SE ALARP",
     "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
    (1.0 / 130000,   "1:130k", "#edea13",
     "RISCHIO TOLLERABILE SE ALARP",
     "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
    (1.0 / 200000,   "1:200k", "#16e813",
     "RISCHIO TOLLERABILE",
     "VALUTAZIONE OPPORTUNA MA NON URGENTE", 2),
    (1.0 / 800000,   "1:800k", "#16e813",
     "RISCHIO TOLLERABILE",
     "VALUTAZIONE OPPORTUNA MA NON URGENTE", 2),
    (1.0 / 1000000,  "1:1M",   "#16e813",
     "RISCHIO TOLLERABILE",
     "VALUTAZIONE OPPORTUNA MA NON URGENTE", 2),
    (1.0e-11,        "<1:1M",  "#00b0f0",
     "RISCHIO LARGAMENTE ACCETTABILE",
     "VALUTAZIONE PROCRASTINABILE", 1),
], key=lambda x: x[0], reverse=True)   # DECRESCENTE

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

POF_LABELS = [
    ("9", "9 - Assente"),
    ("1", "1 - Critico"),
    ("2", "2 - Elevato"),
    ("3", "3 - Significativo"),
    ("4", "4 - Incerto"),
    ("5", "5 - Moderato"),
    ("6", "6 - Basso"),
    ("7", "7 - Trascurabile"),
    ("0", "0 - Approfondimento"),
]

CATEGORIE_BERSAGLIO = [
    ("",            "- Scegli / Da OSM -"),
    ("nessuna",     "Nessuna"),
    ("occupazione", "Occupazione stabile"),
    ("proprieta",   "Proprieta / beni materiali"),
    ("pedoni",      "Pedoni / ciclisti"),
    ("traf_30",     "Traffico 30 km/h"),
    ("traf_50",     "Traffico 50 km/h"),
    ("traf_70",     "Traffico 70 km/h"),
    ("traf_90",     "Traffico 90 km/h"),
    ("traf_110",    "Traffico 110 km/h"),
]

RISCHIO_STILE = {
    "1:3":    "#ca09e8", "1:20":   "#ca09e8", "1:100":  "#ca09e8",
    "1:200":  "#ca09e8", "1:800":  "#ca09e8", "1:1k":   "#ca09e8",
    "1:2k":   "#eb130c", "1:8k":   "#eb130c",
    "1:10k":  "#f0ab18", "1:12k":  "#f0ab18",
    "1:20k":  "#edea13", "1:80k":  "#edea13",
    "1:120k": "#edea13", "1:130k": "#edea13",
    "1:200k": "#16e813", "1:800k": "#16e813", "1:1M":   "#16e813",
    "<1:1M":  "#00b0f0",
    "Assente": "#ffffff",
    "Sospeso": "#9fa0a6",
    "N.D.":    "#9fa0a6",
}


# ===========================================================================
# 2. FORMULE BERSAGLIO - IDENTICHE AL SIMULATORE HTML ARETE
# ===========================================================================
# Fonte: calcTrafAlberoExact, calcTrafBrancaExact, calcPedAlbero, calcPedBranca
# decodificate dal simulatore HTML ufficiale.

def soglie_traf_albero(speed_kmh, h_m, d_chioma_m):
    """
    Soglie veicoli/giorno per classi B1-B7, settore ALBERO INTERO.
    Fedele a target_calc.php (ALIAS ATP).

    Fisica:
      tStop = tempo di frenata (v / 9.8 m/s²)
      tTrav = tempo attraversamento area pericolosa (media h+chioma)
      base  = min(86400/tStop, 86400/tTrav) × 0.76922976
              (0.76922976 = 1/1.3 = passeggeri medi per veicolo ANCI)
    Tutte le soglie in veicoli/giorno — confronto diretto con cv_vei_g.
    """
    if speed_kmh <= 0 or h_m <= 0 or d_chioma_m <= 0:
        return [0.0] * 7
    v     = speed_kmh / 3.6
    tStop = v / 9.8
    tTrav = ((h_m + d_chioma_m) / 2.0) / v
    base  = min(86400.0 / tStop, 86400.0 / tTrav) * 0.76922976
    return [
        base,
        base / 5.0,
        base / 50.0,
        base / 500.0,
        base / 5000.0,
        base / 50000.0,
        base / 500000.0,
        base / 1000000.0,
    ]


def soglie_traf_branca(speed_kmh, l_branca_m):
    """
    Soglie veicoli/giorno per classi B1-B7, settore BRANCA.
    Fedele a target_calc.php (ALIAS ATP).

    l_branca_m × 1.25 = proiezione al suolo maggiorata.
    Stesso fattore 0.76922976 del traffico albero.
    Tutte le soglie in veicoli/giorno — confronto diretto con cv_vei_g.
    """
    if speed_kmh <= 0 or l_branca_m <= 0:
        return [0.0] * 7
    v     = speed_kmh / 3.6
    tStop = v / 9.8
    tTrav = (l_branca_m * 1.25) / v
    base  = min(86400.0 / tStop, 86400.0 / tTrav) * 0.76922976
    return [
        base,
        base / 5.0,
        base / 50.0,
        base / 500.0,
        base / 5000.0,
        base / 50000.0,
        base / 500000.0,
        base / 1000000.0,
    ]


def soglie_ped_albero(h_m, d_chioma_m):
    """
    Soglie pedoni/giorno per classi B1-B7, settore ALBERO INTERO.
    Fedele a target_calc.php (ALIAS ATP).

    Fisica:
      base_ora = passaggi/ora che producono occupazione costante sotto l'albero
                 v_pedone = 1.11 m/s (4 km/h)
                 area pericolosa = media di (h/1.11) e (d_ch/1.11) secondi

    Conversione in pass/giorno per coerenza con cv_ped_g della CV:
      B1, B2:  base_ora/1  e base_ora/5     → ×24  per avere pass/giorno
      B3:      base_ora/50                  → ×24
      B4:      base_ora/500  × 24
      B5:      base_ora/5000 × 24
      B6:      base_ora/50000 × 168         → /7 per riportare a pass/giorno
      B7:      base_ora/500000 × 8760       → /365 per riportare a pass/giorno

    Tutte le soglie in pass/GIORNO per confronto diretto con cv_ped_g.
    """
    if h_m <= 0 or d_chioma_m <= 0:
        return [0.0] * 8
    base_ora = 3600.0 / (((h_m / 1.11) + (d_chioma_m / 1.11)) / 2.0)
    return [
        base_ora * 24.0,                         # B1 max
        (base_ora / 5.0) * 24.0,                 # B1 min = B2 max
        (base_ora / 50.0) * 24.0,                # B2 min = B3 max
        (base_ora / 500.0) * 24.0,               # B3 min = B4 max
        (base_ora / 5000.0) * 24.0,              # B4 min = B5 max
        (base_ora / 50000.0) * 168.0 / 7.0,      # B5 min = B6 max
        (base_ora / 500000.0) * 8760.0 / 365.0,  # B6 min = B7 max
        (base_ora / 1000000.0) * 8760.0 / 365.0, # B7 min
    ]


def soglie_ped_branca(l_branca_m):
    """
    Soglie pedoni/giorno per classi B1-B7, settore BRANCA.
    Fedele a target_calc.php (ALIAS ATP).

    v_ciclista = 1.39 m/s (5 km/h) — ciclisti più veloci del pedone.
    Stessa struttura di conversione in pass/giorno di soglie_ped_albero.
    """
    if l_branca_m <= 0:
        return [0.0] * 7
    base_ora = 3600.0 / (l_branca_m * 1.25 / 1.39)
    return [
        base_ora * 24.0,
        (base_ora / 5.0) * 24.0,
        (base_ora / 50.0) * 24.0,
        (base_ora / 500.0) * 24.0,
        (base_ora / 5000.0) * 24.0,
        (base_ora / 50000.0) * 168.0 / 7.0,
        (base_ora / 500000.0) * 8760.0 / 365.0,
        (base_ora / 1000000.0) * 8760.0 / 365.0,
    ]


def classe_B_da_soglie(n_giorno, soglie):
    """
    Assegna classe B (1-7) confrontando il flusso giornaliero con le soglie.

    Le soglie sono 8 valori in ordine DECRESCENTE:
      soglie[0] = base (massimo B1)
      soglie[1] = base/5  (minimo B1 = massimo B2)
      soglie[2] = base/50 (minimo B2 = massimo B3)
      ...
      soglie[7] = base/1000000 (minimo B7)

    Un valore n_giorno appartiene alla classe i se:
      soglie[i] <= n_giorno < soglie[i-1]

    Quindi la classe si trova cercando il primo soglie[i] <= n_giorno
    partendo da i=1 (il minimo di B1).
    """
    if n_giorno <= 0:
        return 7
    for i in range(1, 8):
        if i < len(soglie) and n_giorno >= soglie[i]:
            return i
    return 7


def classe_B_occupazione(ore_giorno):
    """Classe Bersaglio da occupazione stabile (Tab A3.4)."""
    if ore_giorno <= 0:        return 7
    if ore_giorno >= 5.0:      return 1
    if ore_giorno >= 0.5:      return 2
    if ore_giorno >= 0.05:     return 3
    if ore_giorno >= 0.0333:   return 4
    if ore_giorno >= 8.33e-4:  return 5
    if ore_giorno >= 1.67e-5:  return 6
    return 7


def classe_B_finale(*classi):
    """Classe B piu gravosa (minimo numerico tra le classi valide)."""
    valide = [c for c in classi if isinstance(c, int) and c > 0]
    return min(valide) if valide else 7


# ===========================================================================
# 3. CLASSE FISICA (CF) - Allegato 4
# ===========================================================================

def e_to_class(E):
    if E > 10000: return 1
    if E > 1000:  return 2
    if E > 100:   return 3
    if E > 50:    return 4
    if E > 10:    return 5
    if E > 5:     return 6
    return 7


def classe_fisica_albero(h_m, circonf_cm, hb_m):
    """
    Classe Fisica (CF) albero + energia cinetica (J).
    Formula identica a classeRisicaAlbero() del simulatore HTML.
    dm = diametro tronco a 130cm
    E  = energia impatto al bersaglio
    """
    if h_m <= hb_m or circonf_cm <= 0:
        return 0, 0.0
    dm = (circonf_cm / 3.14) / 100.0
    E  = (0.785 * dm * dm * h_m * 0.75 * 900.0) * math.sqrt(3.0 * 9.80665 * (h_m - hb_m))
    return e_to_class(E), E


def classe_fisica_branca(db_cm, lb_m, hi_m, hb_m):
    """
    Classe Fisica (CF) branca + energia cinetica (J).
    Formula identica a classeRisicaBranca() del simulatore HTML.
    """
    if hi_m <= hb_m or db_cm <= 0 or lb_m <= 0:
        return 0, 0.0
    d = db_cm / 100.0
    E = (0.785 * d * d * lb_m * 0.75 * 900.0) * math.sqrt(2.0 * 9.80665 * (hi_m - hb_m))
    return e_to_class(E), E


# ===========================================================================
# 4. CALCOLO RISCHIO - Allegato 5
# ===========================================================================

def d_to_rischio(d):
    """
    Mappa la probabilita' annua di danno (d) sul livello di rischio Arete.
    Replica esatta di decToRisk() del simulatore HTML ufficiale.
    Usa confronto d*N >= 1 (invece di d >= 1/N) per evitare errori floating-point.
    """
    if d == 0.0:
        return {"r": "Assente", "colore": "#ffffff",
                "giudizio": "ASSENTE", "speditiva": "ASSENTE", "gr": 0}
    # (N, ratio, colore, giudizio, speditiva, gr) -- ordine decrescente di rischio
    _SOGLIE = [
        (3,       "1:3",    "#ca09e8", "RISCHIO INACCETTABILE",
         "RISOLUZIONE DELL EMERGENZA", 6),
        (20,      "1:20",   "#ca09e8", "RISCHIO INACCETTABILE",
         "RISOLUZIONE DELL EMERGENZA", 6),
        (100,     "1:100",  "#ca09e8", "RISCHIO INACCETTABILE",
         "RISOLUZIONE DELL EMERGENZA", 6),
        (200,     "1:200",  "#ca09e8", "RISCHIO INACCETTABILE",
         "RISOLUZIONE DELL EMERGENZA", 6),
        (800,     "1:800",  "#ca09e8", "RISCHIO INACCETTABILE",
         "RISOLUZIONE DELL EMERGENZA", 6),
        (1000,    "1:1k",   "#ca09e8", "RISCHIO INACCETTABILE",
         "RISOLUZIONE DELL EMERGENZA", 6),
        (2000,    "1:2k",   "#eb130c",
         "RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA",
         "RISOLUZIONE DELL EMERGENZA", 5),
        (8000,    "1:8k",   "#eb130c",
         "RISCHIO TOLLERABILE SOLO CON TUTELA SPECIFICA",
         "RISOLUZIONE DELL EMERGENZA", 5),
        (10000,   "1:10k",  "#f0ab18",
         "RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI",
         "VALUTAZIONE URGENTE", 4),
        (12000,   "1:12k",  "#f0ab18",
         "RISCHIO TOLLERABILE PER ACCORDO MA INACCETTABILE SE IMPOSTO A TERZI",
         "VALUTAZIONE URGENTE", 4),
        (20000,   "1:20k",  "#edea13", "RISCHIO TOLLERABILE SE ALARP",
         "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
        (80000,   "1:80k",  "#edea13", "RISCHIO TOLLERABILE SE ALARP",
         "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
        (120000,  "1:120k", "#edea13", "RISCHIO TOLLERABILE SE ALARP",
         "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
        (130000,  "1:130k", "#edea13", "RISCHIO TOLLERABILE SE ALARP",
         "VALUTAZIONE OPPORTUNA ENTRO BREVE TEMPO", 3),
        (200000,  "1:200k", "#16e813", "RISCHIO TOLLERABILE",
         "VALUTAZIONE OPPORTUNA MA NON URGENTE", 2),
        (800000,  "1:800k", "#16e813", "RISCHIO TOLLERABILE",
         "VALUTAZIONE OPPORTUNA MA NON URGENTE", 2),
        (1000000, "1:1M",   "#16e813", "RISCHIO TOLLERABILE",
         "VALUTAZIONE OPPORTUNA MA NON URGENTE", 2),
    ]
    for (N, ratio, colore, giudizio, speditiva, gr) in _SOGLIE:
        if d * N >= 1.0:   # equivale a d >= 1/N, senza errori float
            return {"r": ratio, "colore": colore,
                    "giudizio": giudizio, "speditiva": speditiva, "gr": gr}
    return {"r": "<1:1M", "colore": "#00b0f0",
            "giudizio": "RISCHIO LARGAMENTE ACCETTABILE",
            "speditiva": "VALUTAZIONE PROCRASTINABILE", "gr": 1}


def calc_rischio(B, CF, POF, molt=1):
    """
    Calcola il rischio Arete per una terna (B, CF, POF).

    Casi speciali:
      B=9 o POF=9 → ASSENTE   bersaglio non presente / cedimento escluso
      CF=0        → CF=7      energia nulla: geometria non pericolosa
      POF=0       → SOSPESO   valutazione sospesa, approfondimento necessario
      B=0         → N.D.      bersaglio non determinato
    """
    # Assente: bersaglio non presente o cedimento escluso
    if B == 9 or POF == 9:
        return {"r": "Assente", "colore": "#ffffff",
                "giudizio": "RISCHIO ASSENTE",
                "speditiva": "NESSUNA AZIONE NECESSARIA", "gr": 0}

    # CF=0: energia di cedimento nulla → classe fisica minima (7)
    if CF == 0:
        CF = 7

    # POF=0: valutazione sospesa — approfondimento strumentale necessario
    if POF == 0:
        return {"r": "Sospeso", "colore": "#9fa0a6",
                "giudizio": "VALUTAZIONE SOSPESA",
                "speditiva": "APPROFONDIMENTO NECESSARIO", "gr": 7}

    # B=0: bersaglio non determinato
    if B == 0:
        return {"r": "N.D.", "colore": "#9fa0a6",
                "giudizio": "BERSAGLIO NON DETERMINATO",
                "speditiva": "APPROFONDIMENTO", "gr": 7}

    cod = str(B) + str(CF) + str(POF)
    row = RISCHIO_TAB.get(cod)
    if row is None:
        return {"r": "N.D.", "colore": "#9fa0a6",
                "giudizio": "TERNA NON IN TABELLA",
                "speditiva": "APPROFONDIMENTO", "gr": 7}

    if molt <= 1:
        return {"r": row["r"], "colore": row["colore"],
                "giudizio": row["giudizio"],
                "speditiva": row["speditiva"], "gr": row["gr"]}

    # Moltiplicatore > 1: scala d e rimappa il livello
    d_scaled = row["d"] * molt
    return d_to_rischio(d_scaled)


def rischio_peggiore(*rischi):
    """
    Restituisce il rischio più gravoso tra i settori.
    Esclude gr=0 (Assente) e gr=7 (N.D.) dal confronto dei livelli attivi.
    Se tutti i settori sono Assenti restituisce Assente.
    Se nessun settore è valutabile restituisce N.D.
    """
    # Rischi con livello definito (gr 1-6)
    validi = [r for r in rischi if r and isinstance(r.get("gr"), int)
              and 1 <= r["gr"] <= 6]
    if validi:
        return max(validi, key=lambda x: x["gr"])

    # Solo Assenti (gr=0)
    assenti = [r for r in rischi if r and r.get("gr") == 0]
    if assenti and len(assenti) == len([r for r in rischi if r]):
        return {"r": "Assente", "colore": "#ffffff",
                "giudizio": "RISCHIO ASSENTE",
                "speditiva": "NESSUNA AZIONE NECESSARIA", "gr": 0}

    # Mix di N.D. e Assenti o altri
    altri = [r for r in rischi if r and r.get("gr", 0) > 0]
    if altri:
        return max(altri, key=lambda x: x["gr"])

    return {"r": "N.D.", "colore": "#9fa0a6",
            "giudizio": "N.D.", "speditiva": "N.D.", "gr": 0}


# ===========================================================================
# 5. INTERROGAZIONE OSM - CON RAGGIO GEOMETRICO DELL'ALBERO
# ===========================================================================

_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
_OVERPASS_UA = (
    "AreteVRAPlugin/1.0 (QGIS plugin per valutazione rischio arboreo)"
)


def overpass_query(lat, lon, raggio_m):
    """
    Interroga Overpass API con raggio specificato (derivato dalla geometria albero).
    Prova piu' endpoint in sequenza; imposta User-Agent e Accept per evitare 406.
    """
    query = (
        "[out:json][timeout:25];"
        "(way[\"highway\"](around:{r},{lat},{lon});"
        "way[\"landuse\"](around:{r},{lat},{lon});"
        "way[\"leisure\"](around:{r},{lat},{lon});"
        "way[\"amenity\"](around:{r},{lat},{lon}););"
        "out tags;"
    ).format(r=int(raggio_m), lat=lat, lon=lon)

    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl, QByteArray
    from qgis.PyQt.QtNetwork import QNetworkRequest

    encoded = QByteArray(
        urllib.parse.urlencode({"data": query}).encode("utf-8")
    )

    last_err = None
    for endpoint in _OVERPASS_ENDPOINTS:
        try:
            # Solo endpoint HTTPS (lista hardcoded, difesa in profondita')
            if not endpoint.lower().startswith("https://"):
                raise ValueError("Endpoint non HTTPS rifiutato: " + endpoint)

            req = QNetworkRequest(QUrl(endpoint))
            req.setHeader(
                QNetworkRequest.KnownHeaders.ContentTypeHeader,
                "application/x-www-form-urlencoded; charset=UTF-8",
            )
            req.setRawHeader(b"Accept", b"application/json, text/json, */*")
            req.setRawHeader(b"User-Agent", _OVERPASS_UA.encode("utf-8"))

            blocking = QgsBlockingNetworkRequest()
            err = blocking.post(req, encoded)
            if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
                raise IOError(blocking.errorMessage() or f"errore rete {err}")

            result = json.loads(
                bytes(blocking.reply().content()).decode("utf-8")
            )
            highways, landuses = [], []
            for el in result.get("elements", []):
                tags = el.get("tags", {})
                if "highway" in tags:
                    highways.append(tags)
                if any(k in tags for k in ("landuse", "leisure", "amenity")):
                    landuses.append(tags)
            return highways, landuses
        except Exception as ex:
            last_err = ex
            continue

    raise IOError(
        f"Overpass non raggiungibile.\nUltimo errore: {last_err}"
    )


def miglior_strada(highways):
    """Strada con flusso veicolare piu elevato tra quelle trovate."""
    best, best_flusso, best_vel = {}, 0.0, 0
    for hw in highways:
        htype  = hw.get("highway", "default")
        if htype in ("footway", "cycleway", "path", "steps"):
            continue
        flusso = float(FLUSSI_TIPICI.get(htype, FLUSSI_TIPICI["default"]))
        try:
            lanes = int(hw.get("lanes", "1"))
            if lanes > 1:
                flusso = flusso * lanes / 2.0
        except (ValueError, TypeError):
            pass
        if flusso > best_flusso:
            best_flusso = flusso
            best        = hw
            best_vel    = VELOCITA_STRADA.get(htype, VELOCITA_STRADA["default"])
    return best, best_flusso, best_vel


def stima_flusso_pedonale(hw, landuses):
    """Stima flusso pedonale giornaliero da tipo strada + landuse."""
    htype = hw.get("highway", "") if hw else ""
    if htype in ("footway", "pedestrian", "path", "living_street"):
        base = 500.0
    elif htype in ("residential", "service", "unclassified"):
        base = 100.0
    elif htype in ("tertiary", "secondary"):
        base = 50.0
    elif htype in ("primary", "trunk", "motorway"):
        base = 10.0
    else:
        base = 0.0
    molt = 1.0
    for lt in landuses:
        for k in ("landuse", "leisure", "amenity"):
            molt = max(molt, PEDONI_MOLT_LANDUSE.get(lt.get(k, ""), 1.0))
    return base * molt


def stima_ore_occupazione(landuses):
    """
    Ore medie di occupazione giornaliera dall'uso del suolo.
    Restituisce 0.0 se nessun landuse trovato da OSM, cosi'
    classe_B_occupazione restituisce 7 (trascurabile) - nessuna
    categoria rilevata = trascurabile, non default arbitrario.
    """
    ore_max = 0.0
    for lt in landuses:
        for k in ("landuse", "leisure", "amenity"):
            ore_max = max(ore_max, LANDUSE_ORE.get(lt.get(k, ""), 0.0))
    return ore_max   # 0.0 se nessun landuse -> classe_B_occupazione -> 7


def stima_bersaglio_albero(lat, lon, h_m, d_chioma_m):
    """
    Stima classe Bersaglio per ALBERO INTERO (radici/colletto/fusto).
    Raggio OSM = altezza albero.
    Dimensione area pericolosa = media(h_m, d_chioma_m).
    Restituisce (b_veic, b_ped, b_occ, b_finale, info_dict).
    """
    raggio = max(int(round(h_m)), 5)    # raggio = altezza albero
    try:
        highways, landuses = overpass_query(lat, lon, raggio)
    except Exception as ex:
        return -1, -1, 7, 7, {"errore": str(ex), "raggio_m": raggio,
                               "strada": "N/D", "vel": 0,
                               "vei_g": 0, "ped_g": 0, "ore_occ": 0.0}

    hw, flusso_vei, vel = miglior_strada(highways)
    flusso_ped = stima_flusso_pedonale(hw, landuses) if (highways or landuses) else 0.0
    ore_occ    = stima_ore_occupazione(landuses)

    # Calcolo classi B con formule HTML - soglie dipendono da geometria albero
    # Se nessuna strada trovata i flussi sono 0 -> classe_B_da_soglie restituisce 7
    s_traf = soglie_traf_albero(vel, h_m, d_chioma_m)
    s_ped  = soglie_ped_albero(h_m, d_chioma_m)

    b_veic = classe_B_da_soglie(flusso_vei, s_traf)   # 7 se nessuna strada
    b_ped  = classe_B_da_soglie(flusso_ped, s_ped)    # 7 se nessun pedone
    b_occ  = classe_B_occupazione(ore_occ)             # 7 se nessun landuse
    b_fin  = classe_B_finale(b_veic, b_ped, b_occ)

    strada_tipo = hw.get("highway", "N/D") if hw else "N/D"
    cat         = CATEGORIA_ARETE.get(strada_tipo, "N/D")
    nome        = hw.get("name", "") if hw else ""
    nome_part   = (" - " + nome) if nome else ""

    info = {
        "raggio_m":  raggio,
        "strada":    strada_tipo + " (" + cat + ")" + nome_part,
        "vel":       vel,
        "vei_g":     int(flusso_vei),
        "ped_g":     int(flusso_ped),
        "ore_occ":   round(ore_occ, 2),
        "b_veic":    b_veic,
        "b_ped":     b_ped,
        "b_occ":     b_occ,
        "s1_traf":   round(s_traf[0], 1) if s_traf[0] else 0,
        "s1_ped":    round(s_ped[0], 1)  if s_ped[0]  else 0,
    }
    return b_veic, b_ped, b_occ, b_fin, info


def stima_bersaglio_branca(lat, lon, d_chioma_m, l_branca_m):
    """
    Stima classe Bersaglio per BRANCA pericolosa.
    Raggio OSM = raggio chioma (d_chioma_m / 2).
    Dimensione area pericolosa = l_branca_m * 1.25.
    Restituisce (b_veic, b_ped, b_occ, b_finale, info_dict).
    """
    raggio = max(int(round(d_chioma_m / 2.0)), 3)  # raggio = raggio chioma
    try:
        highways, landuses = overpass_query(lat, lon, raggio)
    except Exception as ex:
        return -1, -1, 7, 7, {"errore": str(ex), "raggio_m": raggio,
                               "strada": "N/D", "vel": 0,
                               "vei_g": 0, "ped_g": 0, "ore_occ": 0.0}

    hw, flusso_vei, vel = miglior_strada(highways)
    flusso_ped = stima_flusso_pedonale(hw, landuses) if (highways or landuses) else 0.0
    ore_occ    = stima_ore_occupazione(landuses)

    # Calcolo classi B con formule HTML - soglie dipendono da geometria branca
    s_traf = soglie_traf_branca(vel, l_branca_m)
    s_ped  = soglie_ped_branca(l_branca_m)

    b_veic = classe_B_da_soglie(flusso_vei, s_traf)
    b_ped  = classe_B_da_soglie(flusso_ped, s_ped)
    b_occ  = classe_B_occupazione(ore_occ)
    b_fin  = classe_B_finale(b_veic, b_ped, b_occ)

    strada_tipo = hw.get("highway", "N/D") if hw else "N/D"
    cat         = CATEGORIA_ARETE.get(strada_tipo, "N/D")
    nome        = hw.get("name", "") if hw else ""
    nome_part   = (" - " + nome) if nome else ""

    info = {
        "raggio_m":  raggio,
        "strada":    strada_tipo + " (" + cat + ")" + nome_part,
        "vel":       vel,
        "vei_g":     int(flusso_vei),
        "ped_g":     int(flusso_ped),
        "ore_occ":   round(ore_occ, 2),
        "b_veic":    b_veic,
        "b_ped":     b_ped,
        "b_occ":     b_occ,
        "s1_traf":   round(s_traf[0], 1) if s_traf[0] else 0,
        "s1_ped":    round(s_ped[0], 1)  if s_ped[0]  else 0,
    }
    return b_veic, b_ped, b_occ, b_fin, info


# ===========================================================================
# 6. HELPER: lettura sicura campi layer
# ===========================================================================

def get_field(feat, field_names, name, default):
    """
    Legge un campo del feature per nome, con fallback al default.
    name puo' essere il nome reale del campo nel layer (mappato dal dialog).
    """
    if name and name in field_names:
        v = feat[name]
        if v is not None and str(v) not in ("NULL", "None", ""):
            try:
                return type(default)(v)
            except (ValueError, TypeError):
                pass
    return default


def crs_utm_da_layer(layer):
    """
    Restituisce il CRS UTM WGS84 appropriato per il layer.
    Se il layer è già in un CRS proiettato metrico, lo restituisce invariato.
    Se è geografico (es. EPSG:4326), calcola il fuso UTM dal centroide del layer.
    """
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject
    crs = layer.crs()
    if not crs.isGeographic():
        return crs   # già metrico

    # Centroide bbox in gradi
    ext = layer.extent()
    if ext.isEmpty():
        return QgsCoordinateReferenceSystem("EPSG:32632")   # UTM32N fallback
    cx = (ext.xMinimum() + ext.xMaximum()) / 2.0
    cy = (ext.yMinimum() + ext.yMaximum()) / 2.0

    # Se il CRS non è 4326 riproietta il centroide
    crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")
    if crs != crs_4326:
        tr = QgsCoordinateTransform(crs, crs_4326, QgsProject.instance())
        pt = tr.transform(cx, cy)
        cx, cy = pt.x(), pt.y()

    zone = int((cx + 180) / 6) + 1
    epsg = 32600 + zone if cy >= 0 else 32700 + zone
    return QgsCoordinateReferenceSystem(f"EPSG:{epsg}")


def riproietta_layer_utm(layer, crs_utm):
    """
    Riproietta il layer in crs_utm (in memoria).
    Restituisce il layer riproiettato o il layer originale se già nel CRS corretto.
    """
    from qgis.core import (
        QgsVectorLayer, QgsCoordinateTransform, QgsProject, QgsFeature,
    )
    if layer.crs() == crs_utm:
        return layer
    tr = QgsCoordinateTransform(layer.crs(), crs_utm, QgsProject.instance())
    lyr_utm = QgsVectorLayer(
        "Point?crs=" + crs_utm.authid(), layer.name() + "_utm", "memory"
    )
    lyr_utm.dataProvider().addAttributes(layer.fields().toList())
    lyr_utm.updateFields()
    feats = []
    for feat in layer.getFeatures():
        f = QgsFeature(feat)
        g = feat.geometry()
        g.transform(tr)
        f.setGeometry(g)
        feats.append(f)
    lyr_utm.dataProvider().addFeatures(feats)
    lyr_utm.updateExtents()
    return lyr_utm


def build_output_fields(existing_fields):
    """Aggiunge i campi VRA al set di campi esistenti."""
    fields = list(existing_fields)
    extra = [
        # Bersaglio albero
        ("Ba_veicoli",  QVariant.Int),
        ("Ba_pedoni",   QVariant.Int),
        ("Ba_occupaz",  QVariant.Int),
        ("Ba_finale",   QVariant.Int),
        ("Ba_raggio_m", QVariant.Int),
        ("Ba_strada",   QVariant.String),
        ("Ba_vel_kmh",  QVariant.Int),
        ("Ba_vei_g",    QVariant.Int),
        ("Ba_ped_g",    QVariant.Int),
        # Bersaglio branca
        ("Bb_veicoli",  QVariant.Int),
        ("Bb_pedoni",   QVariant.Int),
        ("Bb_occupaz",  QVariant.Int),
        ("Bb_finale",   QVariant.Int),
        ("Bb_raggio_m", QVariant.Int),
        ("Bb_strada",   QVariant.String),
        ("Bb_vel_kmh",  QVariant.Int),
        ("Bb_vei_g",    QVariant.Int),
        ("Bb_ped_g",    QVariant.Int),
        # Classe fisica
        ("CF_albero",   QVariant.Int),
        ("CF_branca",   QVariant.Int),
        ("E_alb_J",     QVariant.Double),
        ("E_bra_J",     QVariant.Double),
        # Rischio per settore — livello (r)
        ("R_radici",       QVariant.String),
        ("R_colletto",     QVariant.String),
        ("R_fusto",        QVariant.String),
        ("R_branche",      QVariant.String),
        # Rischio per settore — giudizio ordinario
        ("Rg_radici",      QVariant.String),
        ("Rg_colletto",    QVariant.String),
        ("Rg_fusto",       QVariant.String),
        ("Rg_branche",     QVariant.String),
        # Rischio per settore — speditiva (triage)
        ("Rs_radici",      QVariant.String),
        ("Rs_colletto",    QVariant.String),
        ("Rs_fusto",       QVariant.String),
        ("Rs_branche",     QVariant.String),
        # Rischio per settore — gravita (1-7)
        ("Rv_radici",      QVariant.Int),
        ("Rv_colletto",    QVariant.Int),
        ("Rv_fusto",       QVariant.Int),
        ("Rv_branche",     QVariant.Int),
        # Rischio complessivo
        ("R_peggiore",     QVariant.String),
        ("R_colore",       QVariant.String),
        ("R_giudizio",     QVariant.String),
        ("R_speditiva",    QVariant.String),
        ("R_gravita",      QVariant.Int),
    ]
    for name, vtype in extra:
        fields.append(QgsField(name, vtype))
    return fields


# Campi VRA generati dal plugin, raggruppati per scheda del form
_VRA_TABS = (
    ("Bersaglio albero", (
        "Ba_veicoli", "Ba_pedoni", "Ba_occupaz", "Ba_finale",
        "Ba_raggio_m", "Ba_strada", "Ba_vel_kmh", "Ba_vei_g", "Ba_ped_g",
    )),
    ("Bersaglio branca", (
        "Bb_veicoli", "Bb_pedoni", "Bb_occupaz", "Bb_finale",
        "Bb_raggio_m", "Bb_strada", "Bb_vel_kmh", "Bb_vei_g", "Bb_ped_g",
    )),
    ("Impulso (CF)", (
        "CF_albero", "CF_branca", "E_alb_J", "E_bra_J",
    )),
    ("Rischio per settore", (
        "R_radici",  "Rg_radici",  "Rs_radici",  "Rv_radici",
        "R_colletto","Rg_colletto","Rs_colletto","Rv_colletto",
        "R_fusto",   "Rg_fusto",   "Rs_fusto",   "Rv_fusto",
        "R_branche", "Rg_branche", "Rs_branche", "Rv_branche",
    )),
    ("Rischio complessivo", (
        "R_peggiore", "R_giudizio", "R_speditiva", "R_gravita", "R_colore",
    )),
)

# Alias leggibili per i campi VRA
_VRA_ALIASES = {
    "Ba_veicoli":  "B veicoli (albero)",
    "Ba_pedoni":   "B pedoni (albero)",
    "Ba_occupaz":  "B occupazione (albero)",
    "Ba_finale":   "B finale albero ★",
    "Ba_raggio_m": "Raggio SPOT (m)",
    "Ba_strada":   "Bersaglio rilevato",
    "Ba_vel_kmh":  "Velocità (km/h)",
    "Ba_vei_g":    "Veicoli/giorno",
    "Ba_ped_g":    "Pedoni/giorno",
    "Bb_veicoli":  "B veicoli (branca)",
    "Bb_pedoni":   "B pedoni (branca)",
    "Bb_occupaz":  "B occupazione (branca)",
    "Bb_finale":   "B finale branca ★",
    "Bb_raggio_m": "Raggio area branca (m)",
    "Bb_strada":   "Bersaglio rilevato",
    "Bb_vel_kmh":  "Velocità (km/h)",
    "Bb_vei_g":    "Veicoli/giorno",
    "Bb_ped_g":    "Pedoni/giorno",
    "CF_albero":   "CF albero (1-7)",
    "CF_branca":   "CF branca (1-7)",
    "E_alb_J":     "Energia albero (J)",
    "E_bra_J":     "Energia branca (J)",
    "R_radici":    "Rischio radici",
    "Rg_radici":   "Giudizio radici",
    "Rs_radici":   "Triage radici",
    "Rv_radici":   "Gravità radici (1-7)",
    "R_colletto":  "Rischio colletto",
    "Rg_colletto": "Giudizio colletto",
    "Rs_colletto": "Triage colletto",
    "Rv_colletto": "Gravità colletto (1-7)",
    "R_fusto":     "Rischio fusto",
    "Rg_fusto":    "Giudizio fusto",
    "Rs_fusto":    "Triage fusto",
    "Rv_fusto":    "Gravità fusto (1-7)",
    "R_branche":   "Rischio branche",
    "Rg_branche":  "Giudizio branche",
    "Rs_branche":  "Triage branche",
    "Rv_branche":  "Gravità branche (1-7)",
    "R_peggiore":  "Rischio peggiore ★",
    "R_giudizio":  "Giudizio ordinario ★",
    "R_speditiva": "Speditiva triage ★",
    "R_gravita":   "Gravità complessiva (1-7)",
    "R_colore":    "Colore HEX",
}


def configura_form_vra(layer):
    """
    Configura il form attributi del layer VRA con schede:
      - "Dati origine": tutti i campi ereditati dal layer sorgente
      - una scheda per ogni categoria di campi elaborati dal plugin
        (Bersaglio albero, Bersaglio branca, Impulso, Rischio per settore,
         Rischio complessivo)
    Tutti i campi elaborati sono in sola lettura nel form.
    """
    try:
        from qgis.core import (
            QgsEditFormConfig, QgsAttributeEditorContainer,
            QgsAttributeEditorField, QgsEditorWidgetSetup,
        )

        # Insieme dei campi VRA generati
        campi_vra = set()
        for _, campi in _VRA_TABS:
            campi_vra.update(campi)

        # Campi ereditati = tutti gli altri
        ereditati = [
            f.name() for f in layer.fields()
            if f.name() not in campi_vra
        ]

        # Alias sui campi VRA
        for nome, alias in _VRA_ALIASES.items():
            idx = layer.fields().indexOf(nome)
            if idx >= 0:
                layer.setFieldAlias(idx, alias)

        cfg = layer.editFormConfig()
        cfg.setLayout(QgsEditFormConfig.EditorLayout.TabLayout)
        root = cfg.invisibleRootContainer()
        root.clear()

        def _scheda(titolo, nomi):
            cont = QgsAttributeEditorContainer(titolo, None)
            cont.setColumnCount(1)
            # In QGIS >= 3.32 / 4.x i container devono essere esplicitamente
            # di tipo Tab per apparire come schede (altrimenti group box)
            try:
                from qgis.core import Qgis as _Qgis
                cont.setType(_Qgis.AttributeEditorContainerType.Tab)
            except Exception:
                try:
                    cont.setIsGroupBox(False)   # API QGIS < 3.32
                except Exception:  # nosec B110 - fallback API legacy, non critico
                    pass
            aggiunti = 0
            for n in nomi:
                if layer.fields().indexOf(n) >= 0:
                    cont.addChildElement(QgsAttributeEditorField(n, -1, cont))
                    aggiunti += 1
            return cont if aggiunti else None

        # Scheda 1: dati origine (ereditati dal layer sorgente)
        s = _scheda("Dati origine", ereditati)
        if s: root.addChildElement(s)

        # Schede per categoria elaborata
        for titolo, campi in _VRA_TABS:
            s = _scheda(titolo, campi)
            if s: root.addChildElement(s)

        # Campi elaborati in sola lettura
        for nome in campi_vra:
            idx = layer.fields().indexOf(nome)
            if idx >= 0:
                ws = cfg.widgetConfig(nome)
                cfg.setReadOnly(idx, True)

        layer.setEditFormConfig(cfg)
    except Exception as ex:
        print("Form VRA non configurato: " + str(ex))


def elabora_feature(feat, fnames, params, lat, lon):
    """
    Elabora una singola feature. Chiamata da Worker e da Processing Algorithm.
    I nomi dei campi sono letti da params['fmap_*'] (mappatura drop-down dal dialog).
    Restituisce (extra_attrs, log_str).
    """
    # Mappatura campi: params["fmap_X"] contiene il nome del campo nel layer
    # scelto dall'utente. Se vuoto o assente si usa il valore default params["X"].
    def fv(key_fmap, key_default, default_val):
        """Legge il valore dal campo mappato oppure dal default numerico."""
        campo = params.get(key_fmap, "")
        if campo and campo in fnames:
            v = feat[campo]
            if v is not None and str(v) not in ("NULL", "None", ""):
                try:
                    return type(default_val)(v)
                except (ValueError, TypeError):
                    pass
        return params.get(key_default, default_val)

    # ---- Parametri settore ALBERO (radici/colletto/fusto) ----
    h    = fv("fmap_h",    "h",      12.0)   # altezza albero
    ch   = fv("fmap_ch",   "d_ch",   6.0)    # diametro chioma
    circ = fv("fmap_circ", "circonf",80.0)   # circonferenza tronco a 130cm
    hb   = fv("fmap_hb",   "h_bers", 1.8)    # altezza bersaglio

    # ---- Parametri settore BRANCA ----
    db   = fv("fmap_db",   "d_br",   10.0)   # diametro branca
    lb   = fv("fmap_lb",   "l_br",   3.0)    # lunghezza branca
    hi   = fv("fmap_hi",   "h_ins",  6.0)    # inserzione branca

    # ---- POF (campi layer o default dialog) ----
    pof1 = fv("fmap_pof1", "pof1", 9)
    pof2 = fv("fmap_pof2", "pof2", 9)
    pof3 = fv("fmap_pof3", "pof3", 9)
    pof4 = fv("fmap_pof4", "pof4", 9)
    molt = fv("fmap_molt", "molt",  1)

    # ---- Bersaglio manuale per-feature ----
    bm_campo = params.get("fmap_bman", "")
    bm = 0
    if bm_campo and bm_campo in fnames:
        v = feat[bm_campo]
        if v is not None and str(v) not in ("NULL", "None", ""):
            try:
                bm = int(v)
            except (ValueError, TypeError):
                bm = 0

    # Override globale dal dialog (ha priorita' sul campo per-feature)
    if int(params.get("b_manuale_global", 0)) in range(1, 8):
        bm = int(params["b_manuale_global"])

    # ================================================================
    # BERSAGLIO ALBERO
    # Raggio OSM = altezza albero (h)
    # Area pericolosa = media(h, d_chioma)
    # ================================================================
    if int(bm) in range(1, 8):
        ba_fin  = int(bm)
        ba_veic = ba_ped = ba_occ = -1
        ba_info = {
            "raggio_m": int(round(h)), "strada": "manuale",
            "vel": 0, "vei_g": 0, "ped_g": 0, "ore_occ": 0.0
        }
        log_a = "B_ALB manuale=" + str(ba_fin)
    else:
        ba_veic, ba_ped, ba_occ, ba_fin, ba_info = stima_bersaglio_albero(
            lat, lon, h, ch
        )
        log_a = (
            "B_ALB r=" + str(ba_info["raggio_m"]) + "m"
            + " " + str(ba_info["vel"]) + "km/h"
            + " vei=" + str(ba_info["vei_g"]) + "/g"
            + " B=" + str(ba_fin)
        )

    # ================================================================
    # BERSAGLIO BRANCA
    # Raggio OSM = raggio chioma (d_chioma / 2)
    # Area pericolosa = l_branca * 1.25
    # ================================================================
    if int(bm) in range(1, 8):
        bb_fin  = int(bm)
        bb_veic = bb_ped = bb_occ = -1
        bb_info = {
            "raggio_m": max(int(round(ch / 2)), 3), "strada": "manuale",
            "vel": 0, "vei_g": 0, "ped_g": 0, "ore_occ": 0.0
        }
        log_b = "B_BRA manuale=" + str(bb_fin)
    else:
        bb_veic, bb_ped, bb_occ, bb_fin, bb_info = stima_bersaglio_branca(
            lat, lon, ch, lb
        )
        log_b = (
            "B_BRA r=" + str(bb_info["raggio_m"]) + "m"
            + " " + str(bb_info["vel"]) + "km/h"
            + " vei=" + str(bb_info["vei_g"]) + "/g"
            + " B=" + str(bb_fin)
        )

    # ================================================================
    # CLASSE FISICA
    # ================================================================
    cfa, e_alb = classe_fisica_albero(h, circ, hb)
    cfb, e_bra = classe_fisica_branca(db, lb, hi, hb)

    # ================================================================
    # RISCHIO PER SETTORE
    # Radici, Colletto, Fusto -> bersaglio albero (Ba), CF albero
    # Branche/Rami            -> bersaglio branca (Bb), CF branca
    # ================================================================
    r_rad = calc_rischio(ba_fin, cfa, pof1, molt)
    r_col = calc_rischio(ba_fin, cfa, pof2, molt)
    r_fus = calc_rischio(ba_fin, cfa, pof3, molt)
    r_bra = calc_rischio(bb_fin, cfb, pof4, molt)
    r_peg = rischio_peggiore(r_rad, r_col, r_fus, r_bra)

    log = (
        log_a + " | " + log_b
        + " | CF_alb=" + str(cfa) + " CF_bra=" + str(cfb)
        + " | R=" + r_peg["r"]
    )

    attrs = [
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
        r_rad["giudizio"], r_col["giudizio"], r_fus["giudizio"], r_bra["giudizio"],
        # speditiva triage per settore
        r_rad["speditiva"], r_col["speditiva"], r_fus["speditiva"], r_bra["speditiva"],
        # gravita per settore
        r_rad["gr"], r_col["gr"], r_fus["gr"], r_bra["gr"],
        # complessivo
        r_peg["r"], r_peg["colore"],
        r_peg["giudizio"], r_peg["speditiva"], r_peg["gr"],
    ]
    return attrs, log


# ===========================================================================
# 7. WORKER THREAD
# ===========================================================================

class VRAWorker(QThread):
    sig_progress = pyqtSignal(int)
    sig_log      = pyqtSignal(str)
    sig_done     = pyqtSignal(object)

    def __init__(self, layer, params, parent=None):
        super().__init__(parent)
        self.layer  = layer
        self.params = params

    def run(self):
        layer = self.layer
        p     = self.params
        total = layer.featureCount()
        if total == 0:
            self.sig_log.emit("Layer vuoto.")
            self.sig_done.emit(None)
            return

        crs_src   = layer.crs()
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        tr        = QgsCoordinateTransform(crs_src, crs_wgs84,
                                           QgsProject.instance())

        out_lyr = QgsVectorLayer(
            "Point?crs=" + crs_src.authid(), "VRA_Arete_v4", "memory"
        )
        prov = out_lyr.dataProvider()

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
                    "Feature " + str(feat.id()) + " geometria vuota, saltata."
                )
                continue

            pt  = tr.transform(geom.centroid().asPoint())
            lat = pt.y()
            lon = pt.x()

            self.sig_log.emit(
                "[" + str(i + 1) + "/" + str(total) + "] "
                "id=" + str(feat.id())
            )

            extra_attrs, log = elabora_feature(feat, fnames, p, lat, lon)
            self.sig_log.emit("  " + log)

            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(feat.geometry())
            out_feat.setAttributes(feat.attributes() + extra_attrs)
            feats_out.append(out_feat)
            self.sig_progress.emit(int((i + 1) / total * 100))

        prov.addFeatures(feats_out)
        out_lyr.updateExtents()
        self.sig_log.emit(
            "\nCompletato: " + str(len(feats_out)) + " alberi elaborati."
        )
        self.sig_done.emit(out_lyr)


# ===========================================================================
# 8. STILE
# ===========================================================================

def applica_stile(layer):
    """
    Applica lo stile categorizzato su R_peggiore al layer VRA_Arete_v4.
    Etichetta legenda: livello rischio + giudizio ordinario + speditiva triage.
    """
    try:
        # Etichetta combinata: livello | giudizio ordinario | speditiva triage
        labels = {
            "1:3":    "1:3 — Rischio inaccettabile | Risoluzione dell'emergenza",
            "1:20":   "1:20 — Rischio inaccettabile | Risoluzione dell'emergenza",
            "1:100":  "1:100 — Rischio inaccettabile | Risoluzione dell'emergenza",
            "1:200":  "1:200 — Rischio inaccettabile | Risoluzione dell'emergenza",
            "1:800":  "1:800 — Rischio inaccettabile | Risoluzione dell'emergenza",
            "1:1k":   "1:1k — Rischio inaccettabile | Risoluzione dell'emergenza",
            "1:2k":   "1:2k — Tollerabile solo con tutela specifica | Risoluzione dell'emergenza",
            "1:8k":   "1:8k — Tollerabile solo con tutela specifica | Risoluzione dell'emergenza",
            "1:10k":  "1:10k — Tollerabile per accordo (se imposto a terzi) | Valutazione urgente",
            "1:12k":  "1:12k — Tollerabile per accordo (se imposto a terzi) | Valutazione urgente",
            "1:20k":  "1:20k — Tollerabile se ALARP | Valutazione opportuna entro breve",
            "1:80k":  "1:80k — Tollerabile se ALARP | Valutazione opportuna entro breve",
            "1:120k": "1:120k — Tollerabile se ALARP | Valutazione opportuna entro breve",
            "1:130k": "1:130k — Tollerabile se ALARP | Valutazione opportuna entro breve",
            "1:200k": "1:200k — Tollerabile | Valutazione opportuna ma non urgente",
            "1:800k": "1:800k — Tollerabile | Valutazione opportuna ma non urgente",
            "1:1M":   "1:1M — Tollerabile | Valutazione opportuna ma non urgente",
            "<1:1M":  "<1:1M — Largamente accettabile | Valutazione procrastinabile",
            "Assente":"Assente — Rischio assente | Nessuna azione necessaria",
            "Sospeso":"Sospeso — Valutazione sospesa | Approfondimento necessario",
            "N.D.":   "N.D. — Bersaglio non determinato | Approfondimento",
        }
        cats = []
        for ratio, colore in RISCHIO_STILE.items():
            sym = QgsMarkerSymbol.createSimple({"color": colore, "size": "5"})
            cats.append(QgsRendererCategory(
                ratio, sym, labels.get(ratio, ratio)
            ))
        renderer = QgsCategorizedSymbolRenderer("R_peggiore", cats)
        layer.setRenderer(renderer)
        layer.triggerRepaint()
    except Exception as ex:
        print("Stile non applicato: " + str(ex))


# ===========================================================================

# ===========================================================================
# 8. CARTA VULNERABILITÀ — schema, creazione layer, stima bersaglio da CV
# ===========================================================================

# Campi canonici del layer Carta Vulnerabilità
CV_FIELDS = [
    # (nome, tipo QVariant, lunghezza, precisione, commento)
    ("cv_id",          "Integer",  0,  0, "ID automatico"),
    ("cv_nome",        "String",  80,  0, "Nome zona"),
    ("cv_tipo",        "String",  20,  0, "veicolare|pedonale|occupazione|manufatto|misto"),
    ("cv_prob",        "Integer",  0,  0, "Probabilita presenza 1=raro 5=sempre"),
    ("cv_vei_g",       "Integer",  0,  0, "Veicoli/giorno"),
    ("cv_vel_kmh",     "Integer",  0,  0, "Velocita riferimento km/h"),
    ("cv_ped_g",       "Integer",  0,  0, "Pedoni+ciclisti/giorno"),
    ("cv_ore_flat",    "Double",   0,  2, "Ore/giorno occupazione FLAT diretta (metodo semplice)"),
    # --- METODO GEOMETRICO (SPOT/SDAN) ---
    ("cv_metodo",      "String",  12,  0, "flat | geometrico"),
    ("cv_ore_occ",     "Double",   0,  2, "Ore/giorno permanenza per persona (metodo geometrico)"),
    ("cv_sup_mq",      "Double",   0,  1, "Superficie frequentata mq (0=usa area poligono)"),
    ("cv_per_giorno",  "Integer",  0,  0, "Persone/giorno nell area (metodo geometrico)"),
    ("cv_giorni_anno", "Integer",  0,  0, "Giorni/anno di frequentazione (default 365)"),
    # ------------------------------------
    ("cv_valore_eu",   "Double",   0,  2, "Valore manufatto euro"),
    ("cv_b_alb",       "Integer",  0,  0, "Classe B albero (0=calcola dai campi)"),
    ("cv_b_bra",       "Integer",  0,  0, "Classe B branca (0=calcola dai campi)"),
    ("cv_fonte",       "String", 120,  0, "Fonte del dato"),
    ("cv_note",        "String", 254,  0, "Note libere"),
]

# Colori per lo stile del layer CV per cv_tipo
CV_STILE_TIPO = {
    "veicolare":   "#e53935",
    "pedonale":    "#fb8c00",
    "occupazione": "#8e24aa",
    "manufatto":   "#3949ab",
    "misto":       "#00897b",
}


def _configura_form_cv(lyr):
    """
    Configura il form di inserimento attributi del layer Carta Vulnerabilita'.
    Ogni campo riceve il widget piu' adatto e i valori predefiniti corretti.
    Il form e' organizzato in gruppi tematici con visibilita' condizionale
    dei campi numerici in base al cv_tipo scelto.
    """
    try:
        from qgis.core import (
            QgsEditFormConfig, QgsAttributeEditorContainer,
            QgsAttributeEditorField, QgsAttributeEditorElement,
            QgsEditorWidgetSetup, QgsDefaultValue,
        )
        from qgis.PyQt.QtCore import Qt

        cfg = lyr.editFormConfig()
        cfg.setLayout(QgsEditFormConfig.TabLayout)

        # ── Helper: indice campo per nome ──────────────────────────────
        def fi(nome):
            return lyr.fields().indexFromName(nome)

        # ── Widget per ogni campo ──────────────────────────────────────

        # cv_id: nascosto (autoincrement)
        lyr.setEditorWidgetSetup(fi("cv_id"),
            QgsEditorWidgetSetup("Hidden", {}))

        # cv_nome: testo libero
        lyr.setEditorWidgetSetup(fi("cv_nome"),
            QgsEditorWidgetSetup("TextEdit", {"IsMultiline": False}))
        lyr.setDefaultValueDefinition(fi("cv_nome"),
            QgsDefaultValue("''"))

        # cv_tipo: dropdown con valori fissi
        lyr.setEditorWidgetSetup(fi("cv_tipo"),
            QgsEditorWidgetSetup("ValueMap", {"map": [
                {"veicolare":   "veicolare"},
                {"pedonale":    "pedonale"},
                {"occupazione": "occupazione"},
                {"manufatto":   "manufatto"},
                {"misto":       "misto"},
            ]}))
        lyr.setDefaultValueDefinition(fi("cv_tipo"),
            QgsDefaultValue("'misto'"))

        # cv_prob: dropdown descrittivo 1-5
        lyr.setEditorWidgetSetup(fi("cv_prob"),
            QgsEditorWidgetSetup("ValueMap", {"map": [
                {"1 - Raro (area verde isolata, parco periurbano)":      "1"},
                {"2 - Occasionale (parco urbano, giardino)":             "2"},
                {"3 - Frequente (centro storico, ZTL, marciapiede)":     "3"},
                {"4 - Spesso (strada comunale, pista ciclabile urbana)": "4"},
                {"5 - Sempre (strada principale, area residenziale)":    "5"},
            ]}))
        lyr.setDefaultValueDefinition(fi("cv_prob"),
            QgsDefaultValue("3"))

        # cv_vei_g: numero intero con suffisso
        lyr.setEditorWidgetSetup(fi("cv_vei_g"),
            QgsEditorWidgetSetup("Range", {
                "Min": 0, "Max": 200000, "Step": 10,
                "Suffix": " veic/giorno", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_vei_g"),
            QgsDefaultValue("0"))

        # cv_vel_kmh: dropdown velocita' di riferimento
        lyr.setEditorWidgetSetup(fi("cv_vel_kmh"),
            QgsEditorWidgetSetup("ValueMap", {"map": [
                {"10 km/h — Zona 10 / passo carrabile":   "10"},
                {"30 km/h — Zona 30 / strada locale":     "30"},
                {"50 km/h — Strada comunale":             "50"},
                {"70 km/h — Strada provinciale":          "70"},
                {"90 km/h — Strada statale":              "90"},
                {"110 km/h — Superstrada":                "110"},
                {"130 km/h — Autostrada":                 "130"},
            ]}))
        lyr.setDefaultValueDefinition(fi("cv_vel_kmh"),
            QgsDefaultValue("50"))

        # cv_ped_g: numero intero con suffisso
        lyr.setEditorWidgetSetup(fi("cv_ped_g"),
            QgsEditorWidgetSetup("Range", {
                "Min": 0, "Max": 500000, "Step": 10,
                "Suffix": " ped/giorno", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_ped_g"),
            QgsDefaultValue("0"))

        # cv_ore_flat: ore/giorno flat (metodo semplice, come versione precedente)
        lyr.setEditorWidgetSetup(fi("cv_ore_flat"),
            QgsEditorWidgetSetup("Range", {
                "Min": 0.0, "Max": 24.0, "Step": 0.5,
                "Suffix": " ore/gg (flat)", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_ore_flat"),
            QgsDefaultValue("0"))

        # cv_metodo: scelta del metodo di calcolo B occupazione
        lyr.setEditorWidgetSetup(fi("cv_metodo"),
            QgsEditorWidgetSetup("ValueMap", {"map": [
                {"flat — usa cv_ore_flat direttamente":    "flat"},
                {"geometrico — usa SPOT/SDAN (cv_per_giorno > 0)": "geometrico"},
            ]}))
        lyr.setDefaultValueDefinition(fi("cv_metodo"),
            QgsDefaultValue("'geometrico'", True))

        # cv_ore_occ: ore/giorno permanenza per persona (metodo geometrico)
        lyr.setEditorWidgetSetup(fi("cv_ore_occ"),
            QgsEditorWidgetSetup("Range", {
                "Min": 0.0, "Max": 24.0, "Step": 0.25,
                "Suffix": " ore/gg per persona", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_ore_occ"),
            QgsDefaultValue("0.5"))

        # cv_sup_mq: superficie frequentata (metodo geometrico)
        lyr.setEditorWidgetSetup(fi("cv_sup_mq"),
            QgsEditorWidgetSetup("Range", {
                "Min": 0.0, "Max": 10_000_000.0, "Step": 10,
                "Suffix": " mq (0=area poligono)", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_sup_mq"),
            QgsDefaultValue("0"))

        # cv_per_giorno: persone/giorno (attiva metodo geometrico)
        lyr.setEditorWidgetSetup(fi("cv_per_giorno"),
            QgsEditorWidgetSetup("Range", {
                "Min": 0, "Max": 100_000, "Step": 10,
                "Suffix": " pers/giorno (0=metodo flat)", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_per_giorno"),
            QgsDefaultValue("0"))

        # cv_giorni_anno
        lyr.setEditorWidgetSetup(fi("cv_giorni_anno"),
            QgsEditorWidgetSetup("Range", {
                "Min": 1, "Max": 365, "Step": 1,
                "Suffix": " giorni/anno", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_giorni_anno"),
            QgsDefaultValue("365"))

        # cv_valore_eu: numero intero (euro)
        lyr.setEditorWidgetSetup(fi("cv_valore_eu"),
            QgsEditorWidgetSetup("Range", {
                "Min": 0, "Max": 100000000, "Step": 1000,
                "Suffix": " €", "Style": "SpinBox",
            }))
        lyr.setDefaultValueDefinition(fi("cv_valore_eu"),
            QgsDefaultValue("0"))

        # cv_b_alb / cv_b_bra: dropdown classe B manuale
        _mappa_b = {"map": [
            {"0 — Calcola automatico dai campi": "0"},
            {"1 — Costante (1:1)":               "1"},
            {"2 — Molto alta (1:10)":             "2"},
            {"3 — Alta (1:100)":                  "3"},
            {"4 — Media (1:1k)":                  "4"},
            {"5 — Bassa (1:10k)":                 "5"},
            {"6 — Molto bassa (1:100k)":          "6"},
            {"7 — Trascurabile (1:1M)":           "7"},
        ]}
        lyr.setEditorWidgetSetup(fi("cv_b_alb"),
            QgsEditorWidgetSetup("ValueMap", _mappa_b))
        lyr.setDefaultValueDefinition(fi("cv_b_alb"),
            QgsDefaultValue("0"))
        lyr.setEditorWidgetSetup(fi("cv_b_bra"),
            QgsEditorWidgetSetup("ValueMap", _mappa_b))
        lyr.setDefaultValueDefinition(fi("cv_b_bra"),
            QgsDefaultValue("0"))

        # cv_fonte / cv_note: testo libero
        lyr.setEditorWidgetSetup(fi("cv_fonte"),
            QgsEditorWidgetSetup("TextEdit", {"IsMultiline": False}))
        lyr.setEditorWidgetSetup(fi("cv_note"),
            QgsEditorWidgetSetup("TextEdit", {"IsMultiline": True}))

        # ── Alias leggibili per le intestazioni del form ───────────────
        alias = {
            "cv_id":        "ID",
            "cv_nome":      "Nome zona",
            "cv_tipo":      "Tipo bersaglio",
            "cv_prob":      "Probabilità presenza",
            "cv_vei_g":     "Traffico veicolare",
            "cv_vel_kmh":   "Velocità riferimento",
            "cv_ped_g":     "Flusso pedonale/ciclisti",
            "cv_ore_flat":  "Ore/giorno flat (metodo semplice)",
            "cv_metodo":    "Metodo calcolo B occ (flat/geometrico)",
            "cv_ore_occ":   "Ore/gg per persona (metodo geometrico)",
            "cv_sup_mq":    "Superficie frequentata (mq)",
            "cv_per_giorno":"Persone/giorno (metodo geom.)",
            "cv_giorni_anno":"Giorni/anno frequentazione",
            "cv_valore_eu": "Valore manufatto",
            "cv_b_alb":     "Classe B Albero (manuale)",
            "cv_b_bra":     "Classe B Branca (manuale)",
            "cv_fonte":     "Fonte del dato",
            "cv_note":      "Note",
        }
        for nome, etichetta in alias.items():
            idx = fi(nome)
            if idx >= 0:
                lyr.setFieldAlias(idx, etichetta)

        # ── Layout a schede ───────────────────────────────────────────
        root = cfg.invisibleRootContainer()
        root.clear()

        def _scheda(titolo, campi):
            tab = QgsAttributeEditorContainer(titolo, root)
            try:
                from qgis.core import Qgis as _Qgis
                tab.setType(_Qgis.AttributeEditorContainerType.Tab)
            except Exception:
                try:
                    tab.setIsGroupBox(False)
                except Exception:  # nosec B110 - fallback API legacy, non critico
                    pass
            # setIsGroupBox rimosso (deprecato in QGIS 4)
            tab.setColumnCount(1)
            for nome in campi:
                idx = fi(nome)
                if idx >= 0:
                    tab.addChildElement(
                        QgsAttributeEditorField(nome, idx, tab)
                    )
            return tab

        root.addChildElement(_scheda("Identificazione", [
            "cv_nome", "cv_tipo", "cv_prob",
        ]))
        root.addChildElement(_scheda("Traffico veicolare", [
            "cv_vei_g", "cv_vel_kmh",
        ]))
        root.addChildElement(_scheda("Pedoni / Ciclisti", [
            "cv_ped_g",
        ]))
        root.addChildElement(_scheda("Occupazione stabile", [
            "cv_metodo",
            "cv_ore_flat",
            "cv_ore_occ",
            "cv_per_giorno",
            "cv_giorni_anno",
            "cv_sup_mq",
        ]))
        root.addChildElement(_scheda("Manufatti / Beni", [
            "cv_valore_eu",
        ]))
        root.addChildElement(_scheda("Classe B manuale", [
            "cv_b_alb", "cv_b_bra",
        ]))
        root.addChildElement(_scheda("Fonte e note", [
            "cv_fonte", "cv_note",
        ]))

        lyr.setEditFormConfig(cfg)

    except Exception as ex:
        # Non blocca la creazione del layer se il form fallisce
        print("Configurazione form CV non applicata: " + str(ex))



def crea_layer_cv(percorso_gpkg=None, crs=None):
    """
    Crea un layer poligonale vuoto con lo schema Carta Vulnerabilita'.
    Se percorso_gpkg e' None il layer e' in memoria ("memory").
    Se crs e' None viene usato EPSG:4326 come fallback; passare il CRS UTM
    del layer alberi per output metrici corretti.
    Restituisce (QgsVectorLayer, messaggio_errore).

    Strategia di scrittura GeoPackage:
      Usa QgsVectorLayerExporter (API stabile su QGIS 3.x e 4.x) invece di
      writeAsVectorFormat (deprecata). Il layer viene prima costruito in memoria,
      poi esportato, poi ricaricato da file tramite OGR con il percorso esplicito.
    """
    from qgis.core import (
        QgsVectorLayer, QgsField, QgsFields,
        QgsVectorLayerExporter,
        QgsCoordinateReferenceSystem,
        QgsFillSymbol, QgsCategorizedSymbolRenderer, QgsRendererCategory,
        QgsWkbTypes,
    )
    from qgis.PyQt.QtCore import QVariant

    LAYER_NAME = "carta_vulnerabilita"
    _crs = crs if crs is not None else QgsCoordinateReferenceSystem("EPSG:4326")
    _crs_str = _crs.authid()

    tipo_qvar = {
        "Integer": QVariant.Int,
        "Double":  QVariant.Double,
        "String":  QVariant.String,
    }

    # ── Costruisce lo schema campi ─────────────────────────────────────
    qfields = QgsFields()
    for nome, tipo, lungh, prec, _ in CV_FIELDS:
        f = QgsField(nome, tipo_qvar[tipo])
        if tipo == "String" and lungh:
            f.setLength(lungh)
        if tipo == "Double" and prec:
            f.setPrecision(prec)
        qfields.append(f)

    crs = QgsCoordinateReferenceSystem("EPSG:4326")

    # ── Salva su GeoPackage (se richiesto) ────────────────────────────
    if percorso_gpkg:
        # Normalizza il percorso (barra dritta, niente trailing slash)
        gpkg = percorso_gpkg.replace("\\", "/").rstrip("/")
        # Assicura estensione .gpkg
        if not gpkg.lower().endswith(".gpkg"):
            gpkg += ".gpkg"

        uri = "GPKG:" + gpkg + ":" + LAYER_NAME

        error_code, error_msg = QgsVectorLayerExporter.exportLayer(
            # layer sorgente vuoto in memoria
            QgsVectorLayer(
                "Polygon?crs=" + _crs_str, LAYER_NAME, "memory"
            ),
            uri,           # destinazione
            "ogr",         # provider
            crs,
            False,         # onlySelected
            {
                "driverName": "GPKG",
                "layerName":  LAYER_NAME,
                "overwrite":  True,
            },
        )

        # In alcune versioni exportLayer restituisce solo il codice
        if isinstance(error_code, tuple):
            error_code, error_msg = error_code

        if error_code != QgsVectorLayerExporter.NoError:
            # Fallback: prova con writeAsVectorFormatV3 se disponibile
            try:
                from qgis.core import QgsVectorFileWriter
                opts = QgsVectorFileWriter.SaveVectorOptions()
                opts.driverName    = "GPKG"
                opts.layerName     = LAYER_NAME
                opts.fileEncoding  = "UTF-8"
                opts.actionOnExistingFile = (
                    QgsVectorFileWriter.CreateOrOverwriteFile
                )
                tmp_lyr = QgsVectorLayer(
                    "Polygon?crs=" + _crs_str, LAYER_NAME, "memory"
                )
                tmp_lyr.dataProvider().addAttributes(list(qfields))
                tmp_lyr.updateFields()
                res = QgsVectorFileWriter.writeAsVectorFormatV3(
                    tmp_lyr, gpkg, tmp_lyr.transformContext(), opts
                )
                fb_code = res[0] if isinstance(res, tuple) else res
                if fb_code != QgsVectorFileWriter.NoError:
                    return None, (
                        "Impossibile creare il GeoPackage.\n"
                        "Errore esportazione: " + str(fb_code) + "\n"
                        "Percorso: " + gpkg
                    )
            except Exception as ex:
                return None, (
                    "Impossibile creare il GeoPackage: " + str(ex)
                    + "\nPercorso: " + gpkg
                )

        # Ricarica dal file OGR
        lyr = QgsVectorLayer(
            gpkg + "|layername=" + LAYER_NAME,
            LAYER_NAME, "ogr"
        )
        if not lyr.isValid():
            # Prova senza |layername= (alcuni driver lo ignorano)
            lyr = QgsVectorLayer(gpkg, LAYER_NAME, "ogr")
        if not lyr.isValid():
            return None, (
                "GeoPackage scritto correttamente ma QGIS non riesce ad "
                "aprirlo.\nPercorso: " + gpkg + "\n"
                "Prova ad aprirlo manualmente con Layer > Aggiungi layer "
                "> Aggiungi layer vettoriale."
            )

    else:
        # Layer solo in memoria
        lyr = QgsVectorLayer("Polygon?crs=" + _crs_str, LAYER_NAME, "memory")
        lyr.dataProvider().addAttributes(list(qfields))
        lyr.updateFields()

    # Inizializza editing + indice spaziale
    _attiva_editing_layer(lyr)

    # ── Stile categorizzato per cv_tipo ────────────────────────────────
    try:
        cats = []
        for tipo, colore in CV_STILE_TIPO.items():
            sym = QgsFillSymbol.createSimple({
                "color":         colore + "60",
                "outline_color": colore,
                "outline_width": "0.5",
            })
            cats.append(QgsRendererCategory(tipo, sym, tipo.capitalize()))
        sym_def = QgsFillSymbol.createSimple({
            "color": "#aaaaaa40", "outline_color": "#888888",
            "outline_width": "0.4",
        })
        cats.append(QgsRendererCategory("", sym_def, "(altro)"))
        lyr.setRenderer(QgsCategorizedSymbolRenderer("cv_tipo", cats))
    except Exception:  # nosec B110 - lo stile e' cosmetico, il layer resta valido
        pass

    # ── Form attributi: widget personalizzati per ogni campo ──────────
    _configura_form_cv(lyr)

    return lyr, ""


def stima_bersaglio_da_cv(geom_pt, cv_layer, crs_alberi,
                           h_m, d_chioma_m, l_branca_m):
    """
    Stima le classi bersaglio Albero e Branca interrogando il layer
    Carta Vulnerabilita'.

    La ricerca replica ESATTAMENTE la logica OSM/Overpass:
      - Settore ALBERO  : area circolare con raggio = h_m
                          (proiettata nel CRS del layer CV)
      - Settore BRANCA  : area circolare con raggio = d_chioma_m / 2

    Per ciascun cerchio:
      1. Trova i poligoni CV che intersecano l'area circolare.
      2. Sceglie quello con cv_prob piu' alto
         (in parita': classe B piu' gravosa = valore numerico minore).
      3. Se nessun poligono interseca il cerchio -> B = 7.

    Restituisce (ba_fin, bb_fin, info_dict).
    """
    from qgis.core import (
        QgsCoordinateTransform, QgsCoordinateReferenceSystem,
        QgsProject, QgsGeometry, QgsPointXY, QgsRectangle,
    )

    crs_cv    = cv_layer.crs()
    crs_src   = crs_alberi

    # ── Riproietta il punto nel CRS del layer CV ──────────────────────
    if crs_src != crs_cv:
        tr = QgsCoordinateTransform(crs_src, crs_cv, QgsProject.instance())
        pt_cv = tr.transform(geom_pt.asPoint())
    else:
        pt_cv = geom_pt.asPoint()

    pt_x, pt_y = pt_cv.x(), pt_cv.y()

    # ── Converte raggi metrici in gradi (o unità del CRS) ─────────────
    raggio_alb = max(h_m, 5.0)              # minimo 5 m (come OSM)
    raggio_bra = max(d_chioma_m / 2.0, 3.0) # minimo 3 m (come OSM)

    # ── Costruisce i cerchi con buffer metrico preciso ────────────────
    # Riproietta in UTM locale per il buffer, poi riproietta in CRS CV.
    # Questo elimina l'errore dell'approssimazione gradi/metri che
    # a 44°N vale circa il 28% (cos(44°) = 0.719).
    pt_geom = QgsGeometry.fromPointXY(QgsPointXY(pt_x, pt_y))

    def _buf_metrico_cv(geom_pt, raggio_m):
        if not crs_cv.isGeographic():
            return geom_pt.buffer(raggio_m, 48)
        # UTM locale dal centroide
        lon_c = geom_pt.centroid().asPoint().x()
        lat_c = geom_pt.centroid().asPoint().y()
        zone  = int((lon_c + 180) / 6) + 1
        epsg  = 32600 + zone if lat_c >= 0 else 32700 + zone
        crs_utm  = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        tr_to    = QgsCoordinateTransform(crs_cv, crs_utm, QgsProject.instance())
        tr_back  = QgsCoordinateTransform(crs_utm, crs_cv, QgsProject.instance())
        g = QgsGeometry(geom_pt); g.transform(tr_to)
        b = g.buffer(raggio_m, 48); b.transform(tr_back)
        return b

    cerchio_alb = _buf_metrico_cv(pt_geom, raggio_alb)
    cerchio_bra = _buf_metrico_cv(pt_geom, raggio_bra)

    # ── Ricerca spaziale per ALBERO ───────────────────────────────────
    _crs_cv = cv_layer.crs()   # CRS del layer CV (UTM o 4326)

    def _cerca_candidati(cerchio, calc_b_fn):
        bbox = cerchio.boundingBox()
        cands = []
        for feat in cv_layer.getFeatures(bbox):
            if not feat.geometry().intersects(cerchio):
                continue
            prob  = int(feat["cv_prob"]  or 0)
            b_pre = int(feat["cv_b_alb"] or 0) if calc_b_fn is _cv_calcola_b_albero \
                    else int(feat["cv_b_bra"] or 0)
            if b_pre == 0:
                # Passa la geometria con il CRS del layer come attributo
                geom_f = feat.geometry()
                geom_f._crs_layer = _crs_cv   # attach CRS per il calcolo area
                b_pre = calc_b_fn(feat, h_m, d_chioma_m, geom_cv=geom_f) \
                        if calc_b_fn is _cv_calcola_b_albero \
                        else calc_b_fn(feat, d_chioma_m, l_branca_m, geom_cv=geom_f)
            cands.append({
                "prob":  prob,
                "b":     b_pre,
                "nome":  str(feat["cv_nome"]  or ""),
                "tipo":  str(feat["cv_tipo"]  or ""),
                "fonte": str(feat["cv_fonte"] or ""),
            })
        # Ordina: prob DESC, poi classe B ASC (piu' gravosa)
        cands.sort(key=lambda x: (-x["prob"], x["b"]))
        return cands

    cands_alb = _cerca_candidati(cerchio_alb, _cv_calcola_b_albero)
    cands_bra = _cerca_candidati(cerchio_bra, _cv_calcola_b_branca)

    ba_fin = cands_alb[0]["b"] if cands_alb else 7
    bb_fin = cands_bra[0]["b"] if cands_bra else 7

    zona_alb = cands_alb[0]["nome"] if cands_alb else "nessuna"
    zona_bra = cands_bra[0]["nome"] if cands_bra else "nessuna"

    info = {
        "sorgente":  "CV",
        "zona_alb":  zona_alb,
        "zona_bra":  zona_bra,
        "prob_alb":  cands_alb[0]["prob"] if cands_alb else 0,
        "prob_bra":  cands_bra[0]["prob"] if cands_bra else 0,
        "n_alb":     len(cands_alb),
        "n_bra":     len(cands_bra),
        "r_alb_m":   raggio_alb,
        "r_bra_m":   raggio_bra,
    }
    return ba_fin, bb_fin, info


# Mapping cv_tipo -> quali componenti B sono attive
# Ogni tipo abilita solo i campi semanticamente coerenti.
# "misto" usa tutto.
_CV_TIPO_COMPONENTI = {
    "veicolare":   {"veic": True,  "ped": False, "occ": False, "man": False},
    "pedonale":    {"veic": False, "ped": True,  "occ": False, "man": False},
    "occupazione": {"veic": False, "ped": False, "occ": True,  "man": False},
    "manufatto":   {"veic": False, "ped": False, "occ": False, "man": True},
    "misto":       {"veic": True,  "ped": True,  "occ": True,  "man": True},
}
_CV_TIPO_DEFAULT = {"veic": True, "ped": True, "occ": True, "man": True}


def _cv_componenti(feat):
    """Restituisce il dict componenti attive in base a cv_tipo del poligono."""
    tipo = str(feat["cv_tipo"] or "").strip().lower()
    return _CV_TIPO_COMPONENTI.get(tipo, _CV_TIPO_DEFAULT)


def _cv_b_occ_geometrico(feat, h_m, d_chioma_m, geom_cv=None):
    """
    Calcola la classe B occupazione con metodo FLAT o GEOMETRICO.

    METODO FLAT (cv_metodo='flat' o cv_per_giorno=0):
        B = classe_B_occupazione(cv_ore_flat)

    METODO GEOMETRICO — fedele a targetk.php (ALIAS ATP):

        SPOT      = h² × π × (pp2/100)      pp2=100 sempre (tutta la SPOT)
        SDAN      = (h/2) × (d_chioma/2) × π   ellisse semi-assi h/2 e d_ch/2
        r         = SDAN / (h² × π)            rapporto SDAN/SPOT_totale
        hpy       = per_g × giorni × ore_pp    ore·persone/anno nell'area
        k         = hpy / 8760                 frazione temporale annua
        pmqspot   = k / sup                    densità persone/m²
        pspot     = pmqspot × SPOT             persone nella SPOT
        psdan     = pspot × r                  prob. di colpire una persona [0-1]

    Soglie su psdan (identiche a targetk.php):
        psdan ≥ 0.2       → B1
        psdan ≥ 0.02      → B2
        psdan ≥ 0.002     → B3
        psdan ≥ 0.0002    → B4
        psdan ≥ 0.00002   → B5
        psdan ≥ 0.000002  → B6
        psdan ≥ 0.0000002 → B7
    """
    metodo   = str(feat["cv_metodo"] or "geometrico").strip().lower()
    per_g    = int(feat["cv_per_giorno"] or 0)
    ore_flat = float(feat["cv_ore_flat"] or 0)

    # ── METODO FLAT ───────────────────────────────────────────────────────────
    if metodo == "flat" or per_g <= 0:
        if ore_flat <= 0:
            return None
        return classe_B_occupazione(ore_flat)

    # ── METODO GEOMETRICO ─────────────────────────────────────────────────────
    ore_pp    = float(feat["cv_ore_occ"]    or 0.5)
    giorni    = int(feat["cv_giorni_anno"]  or 365)
    sup_field = float(feat["cv_sup_mq"]    or 0)

    if h_m <= 0:
        return None

    # SPOT totale (pp2=100 → moltiplicatore 1.0)
    SPOT = h_m ** 2 * math.pi

    # SDAN: ellisse con semi-assi h/2 e d_chioma/2  (formula PHP esatta)
    SDAN = (h_m / 2.0) * (d_chioma_m / 2.0) * math.pi

    # Rapporto SDAN/SPOT_totale
    if SPOT <= 0:
        return None
    r = SDAN / SPOT

    # Superficie frequentata: campo esplicito o area poligono CV in UTM
    if sup_field > 0:
        sup = sup_field
    elif geom_cv is not None:
        try:
            from qgis.core import (
                QgsCoordinateReferenceSystem as _CRS,
                QgsCoordinateTransform as _TR,
                QgsProject as _PRJ,
            )
            crs_cv_layer = getattr(geom_cv, '_crs_layer', None)
            if crs_cv_layer is None:
                pt = geom_cv.centroid().asPoint()
                is_geo = (abs(pt.x()) <= 180 and abs(pt.y()) <= 90)
                crs_cv_layer = _CRS("EPSG:4326") if is_geo else None

            if crs_cv_layer is not None and crs_cv_layer.isGeographic():
                lat_c = geom_cv.centroid().asPoint().y()
                lon_c = geom_cv.centroid().asPoint().x()
                zone  = int((lon_c + 180) / 6) + 1
                epsg  = 32600 + zone if lat_c >= 0 else 32700 + zone
                tr    = _TR(crs_cv_layer, _CRS(f"EPSG:{epsg}"), _PRJ.instance())
                g     = QgsGeometry(geom_cv)
                g.transform(tr)
                sup   = g.area()
            else:
                sup = geom_cv.area()

            if sup <= 0:
                raise ValueError("area zero")
        except Exception:
            sup = SPOT
    else:
        sup = SPOT

    if sup <= 0:
        return None

    # Formula targetk.php
    hpy     = per_g * giorni * ore_pp   # ore·persone/anno
    k       = hpy / 8760.0              # frazione temporale annua
    pmqspot = k / sup                   # densità persone/m²
    pspot   = pmqspot * SPOT            # persone nella SPOT
    psdan   = pspot * r                 # probabilità di colpire [0-1]

    # Soglie identiche a targetk.php
    if psdan >= 0.2:        return 1
    if psdan >= 0.02:       return 2
    if psdan >= 0.002:      return 3
    if psdan >= 0.0002:     return 4
    if psdan >= 0.00002:    return 5
    if psdan >= 0.000002:   return 6
    if psdan >= 0.0000002:  return 7
    return 7


def _cv_calcola_b_albero(feat, h_m, d_chioma_m, geom_cv=None):
    """
    Calcola classe B albero dai campi numerici del poligono CV,
    attivando solo i componenti coerenti con cv_tipo.
    geom_cv: geometria con attributo _crs_layer per calcolo area corretto.
    """
    comp    = _cv_componenti(feat)
    vel_kmh = float(feat["cv_vel_kmh"] or 50)

    classi = []

    if comp["veic"]:
        vei_g  = float(feat["cv_vei_g"] or 0)
        s_traf = soglie_traf_albero(vel_kmh, h_m, d_chioma_m)
        classi.append(classe_B_da_soglie(vei_g, s_traf))

    if comp["ped"]:
        ped_g = float(feat["cv_ped_g"] or 0)
        s_ped = soglie_ped_albero(h_m, d_chioma_m)
        classi.append(classe_B_da_soglie(ped_g, s_ped))

    if comp["occ"]:
        # Usa geom_cv passata (con _crs_layer) se disponibile, altrimenti feat.geometry()
        _geom = geom_cv if geom_cv is not None else (
            feat.geometry() if hasattr(feat, "geometry") else None
        )
        b_occ_geom = _cv_b_occ_geometrico(feat, h_m, d_chioma_m, geom_cv=_geom)
        if b_occ_geom is not None:
            classi.append(b_occ_geom)
        else:
            ore_occ = float(feat["cv_ore_occ"] or 0)
            classi.append(classe_B_occupazione(ore_occ))

    if comp["man"]:
        valore = float(feat["cv_valore_eu"] or 0)
        classi.append(_valore_a_classe_B(valore))

    return classe_B_finale(*classi) if classi else 7


def _cv_calcola_b_branca(feat, d_chioma_m, l_branca_m, geom_cv=None):
    """
    Calcola classe B branca dai campi numerici del poligono CV,
    attivando solo i componenti coerenti con cv_tipo.
    geom_cv: geometria con attributo _crs_layer per calcolo area corretto.
    """
    comp    = _cv_componenti(feat)
    vel_kmh = float(feat["cv_vel_kmh"] or 50)

    classi = []

    if comp["veic"]:
        vei_g  = float(feat["cv_vei_g"] or 0)
        s_traf = soglie_traf_branca(vel_kmh, l_branca_m)
        classi.append(classe_B_da_soglie(vei_g, s_traf))

    if comp["ped"]:
        ped_g = float(feat["cv_ped_g"] or 0)
        s_ped = soglie_ped_branca(l_branca_m)
        classi.append(classe_B_da_soglie(ped_g, s_ped))

    if comp["occ"]:
        _geom = geom_cv if geom_cv is not None else (
            feat.geometry() if hasattr(feat, "geometry") else None
        )
        b_occ_geom = _cv_b_occ_geometrico(
            feat, l_branca_m, d_chioma_m, geom_cv=_geom
        )
        if b_occ_geom is not None:
            classi.append(b_occ_geom)
        else:
            ore_occ = float(feat["cv_ore_occ"] or 0)
            classi.append(classe_B_occupazione(ore_occ))

    if comp["man"]:
        valore = float(feat["cv_valore_eu"] or 0)
        classi.append(_valore_a_classe_B(valore))

    return classe_B_finale(*classi) if classi else 7


def _valore_a_classe_B(euro):
    """
    Converte il valore economico in classe B bersaglio.
    Soglie fedeli a target_calc.php (ALIAS ATP):
      B1: >= 600.000 euro
      B2: >= 60.000
      B3: >= 6.000
      B4: >= 600
      B5: >= 60
      B6: >= 6
      B7: >= 3
    """
    if euro <= 0:     return 7
    if euro >= 600_000: return 1
    if euro >= 60_000:  return 2
    if euro >= 6_000:   return 3
    if euro >= 600:     return 4
    if euro >= 60:      return 5
    if euro >= 6:       return 6
    if euro >= 3:       return 7
    return 7



# ===========================================================================
# FUNZIONE SPOT — layer poligonale area di potenziale caduta
# ===========================================================================

def crea_layer_spot(vra_layer, nome="VRA_SPOT"):
    """
    Crea un layer poligonale con la SPOT (area di potenziale caduta) per ogni
    albero elaborato. Raggio SPOT = altezza albero (h_m), che corrisponde al
    raggio usato per la stima del bersaglio OSM.

    Per ogni feature del layer VRA in ingresso genera un cerchio con:
      - raggio = campo Ba_raggio_m (altezza albero approssimata)
      - attributi: R_peggiore, R_colore, R_giudizio, R_speditiva, R_gravita
        piu' i campi R per i 4 settori

    Il layer risultante usa lo stesso CRS del layer VRA.
    Restituisce il QgsVectorLayer in memoria o None se errore.
    """
    from qgis.core import (
        QgsVectorLayer, QgsField, QgsFeature, QgsGeometry, QgsPointXY,
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
        QgsMarkerSymbol, QgsCategorizedSymbolRenderer, QgsRendererCategory,
        QgsFillSymbol, QgsSimpleLineSymbolLayer, QgsSimpleFillSymbolLayer,
        QgsSymbolLayer,
    )
    from qgis.PyQt.QtCore import QVariant
    import math

    crs_src   = vra_layer.crs()
    crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    is_geo    = crs_src.isGeographic()

    # Layer SPOT poligonale
    spot_lyr = QgsVectorLayer(
        "Polygon?crs=" + crs_src.authid(), nome, "memory"
    )
    prov = spot_lyr.dataProvider()

    # Campi: id originale + campi rischio
    spot_fields = [
        QgsField("vra_fid",     QVariant.Int),
        QgsField("h_m",         QVariant.Double),
        QgsField("raggio_m",    QVariant.Double),
        QgsField("R_peggiore",  QVariant.String),
        QgsField("R_colore",    QVariant.String),
        QgsField("R_giudizio",  QVariant.String),
        QgsField("R_speditiva", QVariant.String),
        QgsField("R_gravita",   QVariant.Int),
        QgsField("R_radici",    QVariant.String),
        QgsField("Rg_radici",   QVariant.String),
        QgsField("R_colletto",  QVariant.String),
        QgsField("Rg_colletto", QVariant.String),
        QgsField("R_fusto",     QVariant.String),
        QgsField("Rg_fusto",    QVariant.String),
        QgsField("R_branche",   QVariant.String),
        QgsField("Rg_branche",  QVariant.String),
    ]
    prov.addAttributes(spot_fields)
    spot_lyr.updateFields()

    fields = spot_lyr.fields()

    # Trasformazione per il buffer: se CRS geografico (gradi) serve convertire m->gradi
    # Usiamo sempre CRS proiettato UTM se disponibile, altrimenti approssimazione locale
    # Trasformazione src->wgs84 (mantenuta per compatibilita' futura)
    tr_to_wgs = None
    if is_geo and crs_src != crs_wgs84:
        tr_to_wgs = QgsCoordinateTransform(crs_src, crs_wgs84, QgsProject.instance())

    feats_spot = []
    fi = {f.name(): i for i, f in enumerate(vra_layer.fields())}

    for feat in vra_layer.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue

        # Raggio dalla classe Ba_raggio_m; fallback 10 m
        try:
            raggio = float(feat["Ba_raggio_m"] or 10)
        except (KeyError, TypeError, ValueError):
            raggio = 10.0
        if raggio <= 0:
            raggio = 10.0

        pt = geom.asPoint()

        # Buffer nel CRS del layer
        pt_geom_s = QgsGeometry.fromPointXY(QgsPointXY(pt.x(), pt.y()))
        if is_geo:
            # Buffer metrico preciso via UTM locale
            lon_s = pt.x(); lat_s = pt.y()
            zone_s = int((lon_s + 180) / 6) + 1
            epsg_s = 32600 + zone_s if lat_s >= 0 else 32700 + zone_s
            crs_utm_s = QgsCoordinateReferenceSystem(f"EPSG:{epsg_s}")
            tr_to_s   = QgsCoordinateTransform(crs_src,   crs_utm_s, QgsProject.instance())
            tr_back_s = QgsCoordinateTransform(crs_utm_s, crs_src,   QgsProject.instance())
            g_utm_s = QgsGeometry(pt_geom_s)
            g_utm_s.transform(tr_to_s)
            buf_utm = g_utm_s.buffer(raggio, 48)
            buf_utm.transform(tr_back_s)
            spot_geom = buf_utm
        else:
            spot_geom = pt_geom_s.buffer(raggio, 48)

        def gf(name, default=""):
            try:
                return feat[name]
            except (KeyError, TypeError):
                return default

        sf = QgsFeature(fields)
        sf.setGeometry(spot_geom)
        sf.setAttributes([
            feat.id(),
            raggio,
            raggio,
            gf("R_peggiore",""),
            gf("R_colore","#cccccc"),
            gf("R_giudizio",""),
            gf("R_speditiva",""),
            gf("R_gravita", 0),
            gf("R_radici",""),
            gf("Rg_radici",""),
            gf("R_colletto",""),
            gf("Rg_colletto",""),
            gf("R_fusto",""),
            gf("Rg_fusto",""),
            gf("R_branche",""),
            gf("Rg_branche",""),
        ])
        feats_spot.append(sf)

    prov.addFeatures(feats_spot)
    spot_lyr.updateExtents()

    # Stile SPOT: riempimento data-defined dal campo R_colore (HEX già calcolato),
    # bordo perimetrale leggero nero. Nessun dizionario di colori — prende
    # direttamente il valore del campo per ogni feature.
    try:
        from qgis.core import (
            QgsProperty, QgsSingleSymbolRenderer,
            QgsSymbol, QgsSymbolLayer,
        )

        sym = QgsFillSymbol.createSimple({
            "color":         "#cccccc99",   # colore base (sovrascritto dal data-defined)
            "outline_color": "#1a1a1a",
            "outline_width": "0.26",
            "outline_style": "solid",
        })

        # Imposta il colore di riempimento data-defined dal campo R_colore
        # con opacità 60% (99 in HEX → ~153/255 = 60%)
        fill_layer = sym.symbolLayer(0)
        fill_layer.setDataDefinedProperty(
            QgsSymbolLayer.PropertyFillColor,
            QgsProperty.fromExpression(
                "color_rgba("
                "  color_part(\"R_colore\", 'red'),"
                "  color_part(\"R_colore\", 'green'),"
                "  color_part(\"R_colore\", 'blue'),"
                "  153"           # 153/255 ≈ 60% opacità
                ")"
            )
        )

        spot_lyr.setRenderer(QgsSingleSymbolRenderer(sym))
    except Exception:  # nosec B110 - lo stile e' cosmetico, il layer resta valido
        pass

    return spot_lyr
