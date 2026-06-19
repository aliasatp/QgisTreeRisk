# -*- coding: utf-8 -*-
"""
Arete VRA — Algoritmo "Genera Carta Vulnerabilita' da OSM"

Scarica da Overpass API strade, edifici, landuse e punti di interesse
all'interno dell'estensione scelta (layer utente o vista corrente) e
costruisce un layer Carta Vulnerabilita' precompilato con poligoni
pronti per la revisione.

Logica di costruzione (priorità crescente):
  1. Landuse / uso del suolo      → pedonale / occupazione
  2. Edifici (building=*)         → manufatto (danno da impatto)
  3. Strade — carreggiata         → veicolare  (buffer larghezza corsie)
  4. Strade — marciapiede         → pedonale   (buffer 1.5 m laterale)
  5. Punti OSM buffered           → SOSTITUISCONO la strada sottostante
                                    (difference geometrica sulla strada)
"""

import json
import math
import urllib.parse

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterEnum,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingOutputVectorLayer,
    QgsMapLayer,
    QgsProject,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsFeature,
    QgsFields,
    QgsField,
    QgsVectorLayer,
    QgsRectangle,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QVariant


# ---------------------------------------------------------------------------
# TABELLE DI CONVERSIONE OSM → CV
# ---------------------------------------------------------------------------

# Strade: (cv_vei_g, cv_vel_kmh, cv_prob, cv_ped_g_marciapiede, larghezza_m_per_corsia)
STRADA_PARAMS = {
    #                vei_g   vel  prob  ped_marc  m/corsia
    # ── Strade veicolari ──────────────────────────────────────────────────────
    "motorway":     (50000,  130,  5,     0,       3.75),
    "motorway_link":(10000,  90,   5,     0,       3.50),
    "trunk":        (20000,  110,  5,     0,       3.75),
    "trunk_link":   ( 5000,  90,   5,     0,       3.50),
    "primary":      (10000,   90,  5,   200,       3.50),
    "primary_link": ( 2000,   70,  4,   100,       3.25),
    "secondary":    ( 5000,   70,  5,   300,       3.25),
    "secondary_link":( 1000,  50,  4,   150,       3.00),
    "tertiary":     ( 2000,   50,  4,   400,       3.00),
    "tertiary_link":( 500,    50,  3,   200,       3.00),
    "unclassified": ( 1000,   50,  4,   200,       3.00),
    "residential":  (  300,   30,  3,   150,       3.00),
    "living_street":(   50,   10,  3,   200,       2.75),
    "service":      (  100,   30,  3,    50,       2.50),
    "busway":       (  200,   50,  4,   100,       3.50),  # corsia bus dedicata
    "raceway":      (    0,    0,  1,     0,       4.00),  # pista da corsa: accesso limitato
    # ── Strade miste / agricole / forestali ───────────────────────────────────
    "track":        (    0,   30,  2,    30,       3.00),  # pista campestre: pedonale
    # ── Percorsi pedonali e ciclabili ─────────────────────────────────────────
    "cycleway":     (    0,    0,  3,   300,       1.50),
    "footway":      (    0,    0,  3,   500,       2.00),
    "pedestrian":   (    0,    0,  4,   600,       4.00),
    "path":         (    0,    0,  2,   100,       1.50),
    "steps":        (    0,    0,  3,   400,       2.00),  # scale: solo pedonale
    "bridleway":    (    0,    0,  2,    30,       2.00),  # ippovie: pedonale/equestre
    "corridor":     (    0,    0,  3,   300,       2.00),  # corridoi interni edifici
}
STRADA_DEFAULT = (200, 30, 2, 50, 3.00)  # conservativo per tag non mappati

# Numero di corsie di default per tipo (se OSM non ha lanes=)
CORSIE_DEFAULT = {
    "motorway": 4, "motorway_link": 2,
    "trunk": 2,    "trunk_link": 1,
    "primary": 2,  "primary_link": 1,
    "secondary": 2,"secondary_link": 1,
    "tertiary": 2, "tertiary_link": 1,
    "unclassified": 1, "residential": 1,
    "living_street": 1, "service": 1, "busway": 1,
    "cycleway": 1, "footway": 1, "pedestrian": 1,
    "path": 1, "steps": 1, "bridleway": 1, "corridor": 1,
    "track": 1, "raceway": 2,
}
CORSIE_DEFAULT_VAL = 1

# Tipi classificati come pedonali (no traffico veicolare)
_TIPI_PEDONALI = frozenset({
    "footway", "pedestrian", "cycleway", "path",
    "steps", "bridleway", "corridor", "track",
})

# Larghezza marciapiede standard (m)
MARCIAPIEDE_M = 1.5

# Edifici: (cv_valore_eu, nota)
EDIFICIO_DANNO = {
    "residential":  15_000,
    "apartments":   15_000,
    "house":        15_000,
    "detached":     15_000,
    "terrace":      15_000,
    "commercial":   30_000,
    "retail":       30_000,
    "office":       30_000,
    "industrial":   50_000,
    "warehouse":    50_000,
    "school":       80_000,
    "university":   80_000,
    "hospital":     80_000,
    "church":      150_000,
    "cathedral":   150_000,
    "historic":    150_000,
    "monument":    150_000,
}
EDIFICIO_DANNO_DEFAULT = 10_000

# Landuse → (cv_tipo, cv_ore_flat, cv_ore_occ, cv_ped_g, cv_prob, cv_per_g, cv_gg)
# cv_ore_flat = ore/giorno flat (metodo semplice)
# cv_ore_occ  = ore/giorno per persona (metodo geometrico)
LANDUSE_CV = {
    #                   tipo          ore_flat ore_pp ped_g prob  per_g   gg
    "park":             ("pedonale",    1.0, 1.0,  500, 3,  200, 365),
    "recreation_ground":("pedonale",    1.0, 1.0,  300, 3,  150, 365),
    "playground":       ("pedonale",    1.0, 1.0,  200, 3,  100, 365),
    "garden":           ("pedonale",    0.5, 0.5,  200, 2,   50, 365),
    "grass":            ("pedonale",    0.5, 0.5,   50, 2,   20, 365),
    "forest":           ("pedonale",    0.5, 0.5,   20, 1,   10, 365),
    "cemetery":         ("pedonale",    0.5, 0.5,   30, 2,   15, 365),
    "meadow":           ("pedonale",    0.5, 0.5,   10, 1,    5, 365),
    "allotments":       ("pedonale",    0.5, 0.5,   50, 2,   20, 365),
    "residential":      ("occupazione", 8.0, 0.25,   0, 4,  100, 365),
    "commercial":       ("misto",       4.0, 0.25, 300, 4,  300, 365),
    "retail":           ("misto",       6.0, 0.25, 500, 4,  500, 365),
    "industrial":       ("manufatto",   0.0, 0.0,    0, 3,    0, 365),
    "school":           ("occupazione", 6.0, 2.0,  200, 4,  400, 210),
    "university":       ("occupazione", 6.0, 2.0,  300, 4,  300, 210),
    "hospital":         ("occupazione",12.0, 0.5,  200, 5,  500, 365),
    "sports_centre":    ("pedonale",    6.0, 1.5,  400, 3,  200, 365),
    "stadium":          ("pedonale",    4.0, 1.5, 2000, 4, 1000, 200),
    "pitch":            ("pedonale",    4.0, 1.5,  200, 3,  100, 365),
    "pedestrian":       ("pedonale",    8.0, 0.5, 1000, 4,  500, 365),
}
LANDUSE_CV_DEFAULT = ("misto", 2.0, 0.5, 50, 2, 50, 365)

# Punti OSM → (buf, tipo, prob, ped_g, ore_flat, ore_occ, nome, per_g, gg)
# ore_flat = ore/giorno flat | ore_occ = ore/giorno per persona
PUNTI_CV = {
    #                                buf  tipo          prob  ped  o_fl  o_occ  nome                per_g   gg
    ("highway","traffic_signals"):  (8,  "pedonale",    5, 200,  0.5,  0.0, "Semaforo",           200, 365),
    ("highway","crossing"):         (5,  "pedonale",    5, 300,  0.5,  0.0, "Attraversamento",    300, 365),
    ("highway","bus_stop"):         (6,  "pedonale",    4, 150,  0.5,  0.1, "Fermata bus",        150, 365),
    ("highway","give_way"):         (4,  "pedonale",    4, 100,  0.2,  0.0, "Dare precedenza",    100, 365),
    ("amenity","bench"):            (3,  "occupazione", 4,   0,  1.0,  1.0, "Panchina",            20, 365),
    ("amenity","shelter"):          (4,  "occupazione", 4,  50,  1.0,  0.5, "Pensilina",           30, 365),
    ("amenity","parking"):          (5,  "veicolare",   4, 200,  0.0,  0.0, "Parcheggio",           0, 365),
    ("amenity","cafe"):             (10, "occupazione", 4,   0,  4.0,  0.5, "Bar/Caffe",           40, 365),
    ("amenity","restaurant"):       (10, "occupazione", 4,   0,  4.0,  0.5, "Ristorante",          30, 365),
    ("amenity","fast_food"):        (6,  "pedonale",    4, 100,  1.0,  0.25,"Fast food",           50, 365),
    ("amenity","bar"):              (8,  "occupazione", 4,   0,  3.0,  0.5, "Bar",                 40, 365),
    ("shop",   "any"):              (5,  "pedonale",    4, 200,  0.0,  0.0, "Negozio",              0, 365),
    ("amenity","school"):           (15, "occupazione", 5,   0,  6.0,  2.0, "Scuola",             400, 210),
    ("amenity","hospital"):         (15, "occupazione", 5,   0, 12.0,  0.5, "Ospedale",           500, 365),
    ("amenity","pharmacy"):         (6,  "pedonale",    4, 150,  0.0,  0.0, "Farmacia",             0, 365),
    ("amenity","place_of_worship"): (10, "occupazione", 3,   0,  2.0,  0.5, "Luogo di culto",      50, 100),
    ("amenity","community_centre"): (10, "occupazione", 3,   0,  4.0,  1.0, "Centro sociale",      50, 365),
    ("leisure","playground"):       (8,  "pedonale",    4, 100,  3.0,  1.0, "Area giochi",         50, 365),
    ("leisure","pitch"):            (15, "pedonale",    3, 200,  4.0,  1.5, "Campo sportivo",     100, 200),
    ("leisure","bench"):            (3,  "occupazione", 4,   0,  1.0,  1.0, "Panchina",            20, 365),
}


# ---------------------------------------------------------------------------
# QUERY OVERPASS
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FATTORE DI PONDERAZIONE DEMOGRAFICA
# ---------------------------------------------------------------------------

FASCE_K = [
    (           0,           500, 0.05, "XXS — borgata / nucleo isolato  (< 500 ab.)"),
    (         500,         2_000, 0.15, "XS  — frazione / paese piccolo (500-2k ab.)"),
    (        2000,        10_000, 0.35, "S   — piccolo comune          (2k-10k ab.)"),
    (       10000,        30_000, 0.65, "M-  — comune minore          (10k-30k ab.)"),
    (       30000,        80_000, 1.00, "M   — comune medio (baseline)(30k-80k ab.)"),
    (       80000,       200_000, 1.40, "M+  — comune grande          (80k-200k ab.)"),
    (      200000,       500_000, 1.80, "L   — citta media           (200k-500k ab.)"),
    (      500000,     2_000_000, 2.50, "XL  — grande citta         (500k-2M ab.)"),
    (     2000000,          None, 3.50, "XXL — metropoli                  (> 2M ab.)"),
]

# cv_vei_g, cv_ped_g, cv_ore_flat, cv_per_giorno dipendono dalla densita' urbana.
# cv_ore_occ (ore/persona) e cv_giorni_anno NON dipendono dalla densita'.
_CAMPI_K = ("cv_vei_g", "cv_ped_g", "cv_ore_flat", "cv_per_giorno")


def pop_to_k(abitanti):
    """Converte il numero di abitanti nel moltiplicatore k della fascia."""
    for lo, hi, k, _ in FASCE_K:
        if hi is None or abitanti < hi:
            return k
    return FASCE_K[-1][2]


def _k_da_layer(geom_centroide, k_layer, crs_cv):
    """
    Restituisce il moltiplicatore k dal layer di zonizzazione.

    Campi letti dal layer:
      dz_k       (Double, obbligatorio) — k base da popolazione residente
      dz_k_extra (Double, opzionale)   — k aggiuntivo per flussi extra
                                         (turismo, pendolari, eventi)
      dz_k_modo  (String, opzionale)   — come combinare i due k:
                   'somma'   (default): k = dz_k + dz_k_extra - 1.0
                   'massimo':           k = max(dz_k, dz_k_extra)
      dz_nome    (String, opzionale)   — etichetta descrittiva

    Se piu' poligoni si sovrappongono prende il k_finale piu' alto.
    Se nessun poligono copre il centroide restituisce 1.0.
    """
    from qgis.core import QgsCoordinateTransform, QgsPointXY, QgsRectangle
    crs_k = k_layer.crs()
    if crs_cv != crs_k:
        tr = QgsCoordinateTransform(crs_cv, crs_k, QgsProject.instance())
        pt = tr.transform(geom_centroide.asPoint())
    else:
        pt = geom_centroide.asPoint()
    geom_pt = QgsGeometry.fromPointXY(QgsPointXY(pt.x(), pt.y()))
    eps = 1e-7
    bbox = QgsRectangle(pt.x()-eps, pt.y()-eps, pt.x()+eps, pt.y()+eps)
    best_k = None
    for feat in k_layer.getFeatures(bbox):
        if not feat.geometry().intersects(geom_pt):
            continue
        try:
            k_base  = float(feat["dz_k"]       or 0)
        except (TypeError, KeyError):
            continue
        if k_base <= 0:
            continue

        # Flusso extra (opzionale)
        try:
            k_extra = float(feat["dz_k_extra"] or 0)
        except (TypeError, KeyError):
            k_extra = 0.0

        # Modalita' di combinazione (opzionale, default = somma)
        try:
            modo = str(feat["dz_k_modo"] or "somma").strip().lower()
        except (TypeError, KeyError):
            modo = "somma"

        if k_extra > 0:
            if modo == "massimo":
                k_val = max(k_base, k_extra)
            else:   # somma (default)
                # I flussi si sommano sulla baseline 1.0
                # es. k_base=0.65 + k_extra=1.80 - 1.0 = 1.45
                k_val = k_base + k_extra - 1.0
        else:
            k_val = k_base

        k_val = max(0.1, k_val)   # minimo assoluto per evitare valori negativi

        if best_k is None or k_val > best_k:
            best_k = k_val

    return best_k if best_k is not None else 1.0


def applica_moltiplicatore_k(features, k_fn):
    """
    Applica il moltiplicatore k demografico alle feature CV.

    Scala:
      cv_vei_g      — piu' abitanti = piu' traffico
      cv_ped_g      — piu' abitanti = piu' pedoni
      cv_ore_flat   — piu' abitanti = area piu' frequentata (cap 24h)
      cv_per_giorno — piu' abitanti = piu' persone nell'area

    NON scala:
      cv_ore_occ    — ore/persona: comportamento individuale, non densita'
      cv_giorni_anno — giorni apertura: caratteristica dell'attivita'
      cv_vel_kmh, cv_valore_eu, cv_prob, cv_b_alb/bra
    """
    for fd in features:
        geom = fd.get("geom")
        if geom is None or geom.isEmpty():
            continue
        k = k_fn(geom.centroid())
        if abs(k - 1.0) < 1e-6:
            continue
        for campo in _CAMPI_K:
            val = fd.get(campo, 0)
            if not val:
                continue
            try:
                nuovo = float(val) * k
                if campo == "cv_ore_flat":
                    fd[campo] = round(min(nuovo, 24.0), 2)
                elif campo in ("cv_vei_g", "cv_ped_g", "cv_per_giorno"):
                    fd[campo] = max(0, int(round(nuovo)))
                else:
                    fd[campo] = round(nuovo, 4)
            except (TypeError, ValueError):
                pass
        nota = fd.get("cv_note", "") or ""
        fd["cv_note"] = (nota + f" | k={k:.2f}").strip(" |")
    return features


OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OVERPASS_URL = OVERPASS_ENDPOINTS[0]
TIMEOUT_S    = 60

_OVERPASS_UA = (
    "AreteVRAPlugin/1.0 (QGIS plugin per valutazione rischio arboreo; "
    "https://github.com/arete-vra)"
)


def _overpass(query: str) -> dict:
    """
    Invia la query Overpass con POST + fallback su endpoint alternativi.
    Usa QgsBlockingNetworkRequest (stack di rete QGIS: rispetta proxy,
    certificati e impostazioni di rete dell'applicazione).
    """
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl, QByteArray
    from qgis.PyQt.QtNetwork import QNetworkRequest

    encoded = QByteArray(
        urllib.parse.urlencode({"data": query}).encode("utf-8")
    )

    last_err = None
    for endpoint in OVERPASS_ENDPOINTS:
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

            reply = blocking.reply()
            return json.loads(bytes(reply.content()).decode("utf-8"))
        except Exception as ex:
            last_err = ex
            # Prova endpoint successivo
            continue

    raise IOError(
        f"Overpass non raggiungibile su nessun endpoint.\n"
        f"Ultimo errore: {last_err}\n"
        f"Endpoint tentati: {OVERPASS_ENDPOINTS}"
    )


def _bbox_str(bbox_4326):
    """(minx, miny, maxx, maxy) → 'S,W,N,E' per Overpass."""
    return f"{bbox_4326[1]},{bbox_4326[0]},{bbox_4326[3]},{bbox_4326[2]}"


def scarica_osm(bbox_4326, feedback=None):
    """
    Scarica strade, edifici, landuse e punti di interesse da Overpass API.
    Restituisce il dict GeoJSON-like con elementi per categoria.
    """
    bb = _bbox_str(bbox_4326)

    def _log(msg):
        if feedback:
            feedback.pushInfo(msg)

    _log(f"Overpass query bbox: {bb}")

    query = f"""
[out:json][timeout:{TIMEOUT_S}];
(
  way["highway"]({bb});
  way["building"]({bb});
  way["landuse"]({bb});
  way["leisure"]({bb});
  way["amenity"]({bb});
  node["highway"~"traffic_signals|crossing|bus_stop|give_way"]({bb});
  node["amenity"~"bench|shelter|parking|cafe|restaurant|fast_food|bar|school|hospital|pharmacy|place_of_worship|community_centre"]({bb});
  node["shop"]({bb});
  node["leisure"~"playground|pitch|bench"]({bb});
);
out body geom;
"""
    raw = _overpass(query)
    elementi = raw.get("elements", [])
    _log(f"Elementi OSM ricevuti: {len(elementi)}")
    return elementi


# ---------------------------------------------------------------------------
# BUFFER METRICO PRECISO (con riproiezione UTM locale)
# ---------------------------------------------------------------------------

def _buffer_metrico(geom: QgsGeometry, crs_src: QgsCoordinateReferenceSystem,
                    raggio_m: float, segmenti: int = 32) -> QgsGeometry:
    """
    Esegue un buffer di raggio_m metri con precisione metrica,
    indipendentemente dal CRS sorgente (geografico o proiettato).

    Strategia:
      1. Calcola il CRS UTM locale dal centroide della geometria
      2. Riproietta la geometria in UTM (unita' metriche)
      3. Esegue il buffer in metri
      4. Riproietta il risultato nel CRS originale

    Funziona correttamente per EPSG:4326 (gradi) e per qualsiasi
    CRS proiettato non metrico.
    """
    from qgis.core import (
        QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
    )

    # Se il CRS e' gia' proiettato in metri, buffer diretto
    if not crs_src.isGeographic():
        return geom.buffer(raggio_m, segmenti)

    # Ricava il CRS UTM locale dal centroide
    centroide = geom.centroid().asPoint()
    lon, lat  = centroide.x(), centroide.y()
    zone      = int((lon + 180) / 6) + 1
    epsg_utm  = 32600 + zone if lat >= 0 else 32700 + zone
    crs_utm   = QgsCoordinateReferenceSystem(f"EPSG:{epsg_utm}")

    tr_to   = QgsCoordinateTransform(crs_src, crs_utm, QgsProject.instance())
    tr_back = QgsCoordinateTransform(crs_utm, crs_src, QgsProject.instance())

    geom_utm    = QgsGeometry(geom)
    geom_utm.transform(tr_to)
    geom_buf    = geom_utm.buffer(raggio_m, segmenti)
    geom_buf.transform(tr_back)
    return geom_buf


def _larghezza_strada(htype: str, tags: dict) -> float:
    """
    Larghezza totale carreggiata in metri.
    Usa width= se disponibile, altrimenti lanes= × larghezza_corsia, altrimenti default.
    """
    params = STRADA_PARAMS.get(htype, STRADA_DEFAULT)
    m_corsia = params[4]

    # width= tag diretto
    if "width" in tags:
        try:
            return float(tags["width"])
        except ValueError:
            pass

    # lanes= tag
    corsie_default = CORSIE_DEFAULT.get(htype, CORSIE_DEFAULT_VAL)
    if "lanes" in tags:
        try:
            corsie = int(tags["lanes"])
        except ValueError:
            corsie = corsie_default
    else:
        corsie = corsie_default

    return corsie * m_corsia


# ---------------------------------------------------------------------------
# COSTRUZIONE GEOMETRIE
# ---------------------------------------------------------------------------

def _way_to_linestring(element: dict) -> QgsGeometry | None:
    """Converte un way OSM (con geometry) in QgsGeometry LineString."""
    geom_nodes = element.get("geometry", [])
    if len(geom_nodes) < 2:
        return None
    pts = [QgsPointXY(n["lon"], n["lat"]) for n in geom_nodes]
    return QgsGeometry.fromPolylineXY(pts)


def _way_to_polygon(element: dict) -> QgsGeometry | None:
    """Converte un way OSM chiuso in QgsGeometry Polygon."""
    geom_nodes = element.get("geometry", [])
    if len(geom_nodes) < 3:
        return None
    pts = [QgsPointXY(n["lon"], n["lat"]) for n in geom_nodes]
    return QgsGeometry.fromPolygonXY([pts])


def _node_to_point(element: dict) -> QgsGeometry | None:
    lat = element.get("lat")
    lon = element.get("lon")
    if lat is None or lon is None:
        return None
    return QgsGeometry.fromPointXY(QgsPointXY(lon, lat))


# _buffer_deg rimossa — usare _buffer_metrico(geom, crs, raggio_m)


def _lat_centro(element: dict) -> float:
    """Latitudine centrale dell'elemento (per conversione m→gradi)."""
    geom = element.get("geometry", [])
    if geom:
        lats = [n["lat"] for n in geom]
        return sum(lats) / len(lats)
    return element.get("lat", 45.0)


# ---------------------------------------------------------------------------
# FUNZIONE PRINCIPALE
# ---------------------------------------------------------------------------

_crs_4326_osm = QgsCoordinateReferenceSystem("EPSG:4326")


def genera_cv_da_osm(bbox_4326, feedback=None):
    """
    Genera la lista di feature (dict) per il layer Carta Vulnerabilita'
    a partire dai dati OSM nell'area bbox_4326 = (minx, miny, maxx, maxy).

    Restituisce lista di dict con chiavi compatibili con CV_FIELDS:
      geom, cv_nome, cv_tipo, cv_prob, cv_vei_g, cv_vel_kmh,
      cv_ped_g, cv_ore_occ, cv_valore_eu, cv_b_alb, cv_b_bra,
      cv_fonte, cv_note
    """

    def _log(msg):
        if feedback:
            feedback.pushInfo(msg)

    elementi = scarica_osm(bbox_4326, feedback)

    # Separazione per categoria
    strade   = [e for e in elementi if e.get("type") == "way"
                and "highway" in e.get("tags", {})]
    edifici  = [e for e in elementi if e.get("type") == "way"
                and "building" in e.get("tags", {})]
    landuses = [e for e in elementi if e.get("type") == "way"
                and any(k in e.get("tags", {})
                        for k in ("landuse", "leisure", "amenity"))
                and "highway" not in e.get("tags", {})
                and "building" not in e.get("tags", {})]
    nodi_poi = [e for e in elementi if e.get("type") == "node"]

    _log(f"  Strade:  {len(strade)}")
    _log(f"  Edifici: {len(edifici)}")
    _log(f"  Landuse: {len(landuses)}")
    _log(f"  POI:     {len(nodi_poi)}")

    features   = []   # risultato finale
    geom_strade_carr = []  # (geom_carreggiata, indice in features) per sottrazione punti

    lat_centro_area = (bbox_4326[1] + bbox_4326[3]) / 2.0

    # ── 1. LANDUSE ──────────────────────────────────────────────────────────
    _log("Elaborazione landuse...")
    for el in landuses:
        tags   = el.get("tags", {})
        geom_p = _way_to_polygon(el)
        if geom_p is None or geom_p.isEmpty():
            continue

        lu = (tags.get("landuse") or tags.get("leisure")
              or tags.get("amenity") or "")
        params = LANDUSE_CV.get(lu, LANDUSE_CV_DEFAULT)
        cv_tipo, cv_ore_flat, cv_ore, cv_ped, cv_prob, cv_per_g, cv_gg = params

        features.append({
            "geom":           geom_p,
            "cv_nome":        tags.get("name", lu.capitalize() or "Landuse"),
            "cv_tipo":        cv_tipo,
            "cv_prob":        cv_prob,
            "cv_vei_g":       0,
            "cv_vel_kmh":     0,
            "cv_ped_g":       cv_ped,
            "cv_ore_flat":    cv_ore_flat,
            "cv_metodo":      "geometrico",
            "cv_ore_occ":     cv_ore,
            "cv_sup_mq":      0,
            "cv_per_giorno":  cv_per_g,
            "cv_giorni_anno": cv_gg,
            "cv_valore_eu":   0,
            "cv_b_alb":       0,
            "cv_b_bra":       0,
            "cv_fonte":       "OSM landuse",
            "cv_note":        lu,
        })

    # ── 2. EDIFICI ──────────────────────────────────────────────────────────
    _log("Elaborazione edifici...")
    for el in edifici:
        tags   = el.get("tags", {})
        geom_p = _way_to_polygon(el)
        if geom_p is None or geom_p.isEmpty():
            continue

        btype  = tags.get("building", "yes")
        danno  = EDIFICIO_DANNO.get(btype, EDIFICIO_DANNO_DEFAULT)
        nome   = tags.get("name", tags.get("addr:street",
                 "Edificio " + btype.capitalize()))

        features.append({
            "geom":           geom_p,
            "cv_nome":        nome,
            "cv_tipo":        "manufatto",
            "cv_prob":        4,
            "cv_vei_g":       0,
            "cv_vel_kmh":     0,
            "cv_ped_g":       0,
            "cv_ore_flat":    0.0,
            "cv_metodo":      "geometrico",
            "cv_ore_occ":     0.0,
            "cv_sup_mq":      0,
            "cv_per_giorno":  0,
            "cv_giorni_anno": 365,
            "cv_valore_eu":   danno,
            "cv_b_alb":       0,
            "cv_b_bra":       0,
            "cv_fonte":       "OSM building",
            "cv_note":        f"building={btype}",
        })

    # ── 3. STRADE — carreggiata + marciapiede ───────────────────────────────
    _log("Elaborazione strade...")
    for el in strade:
        tags   = el.get("tags", {})
        htype  = tags.get("highway", "")
        params = STRADA_PARAMS.get(htype, STRADA_DEFAULT)
        vei_g, vel_kmh, prob, ped_marc, _ = params

        # Salta highway non stradali (footway/cycleway trattati sotto)
        geom_l = _way_to_linestring(el)
        if geom_l is None or geom_l.isEmpty():
            continue

        lat = _lat_centro(el)
        larg_m = _larghezza_strada(htype, tags)

        # Velocità da maxspeed se presente
        if "maxspeed" in tags:
            try:
                vel_kmh = int(tags["maxspeed"])
            except (ValueError, TypeError):
                pass

        nome = tags.get("name", htype.capitalize())

        # Carreggiata (veicolare o pedonale)
        half_m = larg_m / 2.0
        geom_carr = _buffer_metrico(geom_l, _crs_4326_osm, half_m)
        if not geom_carr.isEmpty():
            if htype in _TIPI_PEDONALI:
                tipo_carr  = "pedonale"
                vei_g_carr = 0
                ped_g_carr = params[3]
            else:
                tipo_carr  = "veicolare"
                vei_g_carr = vei_g
                ped_g_carr = 0

            idx_carr = len(features)
            features.append({
                "geom":           geom_carr,
                "cv_nome":        nome,
                "cv_tipo":        tipo_carr,
                "cv_prob":        prob,
                "cv_vei_g":       vei_g_carr,
                "cv_vel_kmh":     vel_kmh,
                "cv_ped_g":       ped_g_carr,
                "cv_ore_flat":    0.0,
                "cv_metodo":      "geometrico",
                "cv_ore_occ":     0.0,
                "cv_sup_mq":      0,
                "cv_per_giorno":  0,
                "cv_giorni_anno": 365,
                "cv_valore_eu":   0,
                "cv_b_alb":       0,
                "cv_b_bra":       0,
                "cv_fonte":       "OSM highway",
                "cv_note":        f"highway={htype} larghezza={larg_m:.1f}m",
            })
            if tipo_carr == "veicolare":
                geom_strade_carr.append((geom_carr, idx_carr))

        # Marciapiedi rimossi: il flusso pedonale viene catturato dai POI.

    # ── 4. PUNTI OSM — buffer + sottrazione dalla strada ───────────────────
    _log("Elaborazione POI...")
    for el in nodi_poi:
        tags   = el.get("tags", {})
        geom_n = _node_to_point(el)
        if geom_n is None:
            continue

        lat = el.get("lat", lat_centro_area)
        params_poi = None
        nome_poi   = tags.get("name", "")

        # Cerca corrispondenza in PUNTI_CV
        for (k, v), p in PUNTI_CV.items():
            tag_val = tags.get(k, "")
            if v == "any":
                if tag_val:
                    params_poi = p
                    break
            elif tag_val == v:
                params_poi = p
                break

        if params_poi is None:
            continue

        buf_m, cv_tipo, cv_prob, cv_ped, cv_ore_flat, cv_ore, nome_def, cv_per_g, cv_gg = params_poi
        nome_poi = nome_poi or nome_def

        geom_buf = _buffer_metrico(geom_n, _crs_4326_osm, buf_m)
        if geom_buf.isEmpty():
            continue

        # Sottrai il buffer dalla/e carreggiata/e che interseca
        for geom_carr, idx in geom_strade_carr:
            if geom_carr.intersects(geom_buf):
                nuova_carr = features[idx]["geom"].difference(geom_buf)
                if not nuova_carr.isEmpty():
                    features[idx]["geom"] = nuova_carr

        features.append({
            "geom":           geom_buf,
            "cv_nome":        nome_poi,
            "cv_tipo":        cv_tipo,
            "cv_prob":        cv_prob,
            "cv_vei_g":       0,
            "cv_vel_kmh":     0,
            "cv_ped_g":       cv_ped,
            "cv_ore_flat":    cv_ore_flat,
            "cv_metodo":      "geometrico",
            "cv_ore_occ":     cv_ore,
            "cv_sup_mq":      0,
            "cv_per_giorno":  cv_per_g,
            "cv_giorni_anno": cv_gg,
            "cv_valore_eu":   0,
            "cv_b_alb":       0,
            "cv_b_bra":       0,
            "cv_fonte":       "OSM node",
            "cv_note":        f"{list(tags.items())[:3]}",
        })

    _log(f"Feature CV generate: {len(features)}")
    return features


# ---------------------------------------------------------------------------
# ALGORITMO PROCESSING
# ---------------------------------------------------------------------------

class AreteGeneraCVdaOSMAlgorithm(QgsProcessingAlgorithm):

    SORGENTE_ESTENSIONE = "SORGENTE_ESTENSIONE"
    LAYER_ESTENSIONE    = "LAYER_ESTENSIONE"
    SORGENTE_K          = "SORGENTE_K"
    ABITANTI            = "ABITANTI"
    LAYER_K             = "LAYER_K"
    OUTPUT              = "OUTPUT"

    def name(self):
        return "arete_genera_cv_da_osm"

    def displayName(self):
        return "Genera Carta Vulnerabilita' da OSM"

    def group(self):
        return "QgisTreeRisk"

    def groupId(self):
        return "qgistreerisck"

    def shortHelpString(self):
        return (
            "Scarica da OpenStreetMap (Overpass API) strade, edifici, "
            "uso del suolo e punti di interesse nell'area scelta e "
            "costruisce un layer <b>Carta Vulnerabilita'</b> precompilato "
            "con poligoni pronti per la revisione.<br><br>"
            "<b>Sorgente estensione:</b><br>"
            "<ul>"
            "<li><i>Estensione layer</i> — usa il bounding box del layer "
            "selezionato (es. layer alberi)</li>"
            "<li><i>Vista corrente della mappa</i> — usa l'area visibile "
            "nella finestra principale di QGIS</li>"
            "</ul>"
            "<b>Struttura poligoni generati (priorita' crescente):</b><br>"
            "<ol>"
            "<li>Landuse / uso del suolo</li>"
            "<li>Edifici (manufatto — danno da impatto)</li>"
            "<li>Strade — carreggiata (veicolare)</li>"
            "<li>Strade — marciapiede (pedonale, adiacente)</li>"
            "<li>Punti OSM buffered (semafori, panchine, ecc.) — "
            "sottraggono la loro area dalla strada sottostante</li>"
            "</ol>"
            "I valori precompilati sono medie standard: "
            "I valori precompilati sono medie standard calibrate su una citta' "
            "di riferimento (30k-80k ab., k=1.0).<br><br>"
            "<b>Fattore demografico (cv_vei_g, cv_ped_g, cv_ore_occ):</b><br>"
            "<ul>"
            "<li><i>Manuale</i> — inserisci gli abitanti del comune</li>"
            "<li><i>Layer comuni</i> — campo popolazione, fascia ricavata automaticamente</li>"
            "<li><i>Layer zonizzazione</i> — campo <b>dz_k</b> per modulare zona per zona.<br>"
            "Campi opzionali: <b>dz_k_extra</b> (flussi extra: turismo/pendolari/eventi), "
            "<b>dz_k_modo</b> ('somma' o 'massimo'), <b>dz_nome</b> (etichetta)</li>"
            "</ul>"
            "<b>Revisiona i valori nel layer prima dell'uso.</b>"
        )

    def createInstance(self):
        return AreteGeneraCVdaOSMAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterEnum(
                self.SORGENTE_ESTENSIONE,
                "Sorgente estensione area di scarico",
                options=[
                    "Estensione del layer selezionato",
                    "Vista corrente della mappa (canvas)",
                ],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterMapLayer(
                self.LAYER_ESTENSIONE,
                "Layer di riferimento (usato se sorgente = layer)",
                optional=True,
            )
        )
        # ── Fattore demografico ──────────────────────────────────────
        self.addParameter(
            QgsProcessingParameterEnum(
                self.SORGENTE_K,
                "Fattore demografico — sorgente",
                options=[
                    "Valore manuale (numero abitanti)",
                    "Layer comuni (campo popolazione)",
                    "Layer zonizzazione (campo dz_k per zona)",
                    "Nessuno (k=1, valori invariati)",
                ],
                defaultValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ABITANTI,
                "Abitanti del comune (se sorgente = manuale)",
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=50000,
                minValue=0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterMapLayer(
                self.LAYER_K,
                "Layer comuni / zonizzazione (se sorgente = layer)",
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.OUTPUT,
                "GeoPackage di destinazione Carta Vulnerabilita'",
                fileFilter="GeoPackage (*.gpkg)",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        import os

        sorg    = self.parameterAsEnum(parameters, self.SORGENTE_ESTENSIONE, context)
        sorg_k  = self.parameterAsEnum(parameters, self.SORGENTE_K, context)
        abitanti = self.parameterAsInt(parameters, self.ABITANTI, context) or 50000
        lyr_k   = self.parameterAsLayer(parameters, self.LAYER_K, context)
        gpkg    = self.parameterAsFileOutput(parameters, self.OUTPUT, context)
        gpkg  = gpkg.replace("\\", "/")
        if not gpkg.lower().endswith(".gpkg"):
            gpkg += ".gpkg"

        cartella = os.path.dirname(gpkg)
        if cartella and not os.path.isdir(cartella):
            feedback.reportError("Cartella non esistente: " + cartella, True)
            return {}

        # ── Determina bbox in EPSG:4326 ──────────────────────────────
        crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")

        if sorg == 0:
            # Estensione layer
            lyr_ref = self.parameterAsLayer(
                parameters, self.LAYER_ESTENSIONE, context
            )
            if lyr_ref is None:
                feedback.reportError(
                    "Seleziona un layer di riferimento oppure scegli "
                    "'Vista corrente della mappa'.", True
                )
                return {}
            extent = lyr_ref.extent()
            crs_src = lyr_ref.crs()
        else:
            # Vista corrente canvas
            try:
                from qgis.utils import iface
                canvas  = iface.mapCanvas()
                extent  = canvas.extent()
                crs_src = canvas.mapSettings().destinationCrs()
            except Exception as ex:
                feedback.reportError(
                    "Impossibile leggere la vista corrente: " + str(ex), True
                )
                return {}

        # Riproietta bbox in 4326
        if crs_src != crs_4326:
            tr = QgsCoordinateTransform(
                crs_src, crs_4326, QgsProject.instance()
            )
            extent = tr.transformBoundingBox(extent)

        # Aggiunge un margine del 10% per includere elementi ai bordi
        dx = extent.width()  * 0.10
        dy = extent.height() * 0.10
        bbox = (
            extent.xMinimum() - dx,
            extent.yMinimum() - dy,
            extent.xMaximum() + dx,
            extent.yMaximum() + dy,
        )

        feedback.pushInfo(
            f"Area di scarico: "
            f"W={bbox[0]:.5f} S={bbox[1]:.5f} "
            f"E={bbox[2]:.5f} N={bbox[3]:.5f}"
        )

        # ── Scarica e genera feature CV ──────────────────────────────
        try:
            feature_dicts = genera_cv_da_osm(bbox, feedback)
        except Exception as ex:
            feedback.reportError("Errore Overpass: " + str(ex), True)
            return {}

        if not feature_dicts:
            feedback.pushInfo("Nessuna feature generata nell'area.")

        # ── Applica fattore demografico k ────────────────────────────
        _crs_4326 = QgsCoordinateReferenceSystem("EPSG:4326")

        if sorg_k == 3:
            feedback.pushInfo("Fattore demografico: disabilitato (k=1.0)")

        elif sorg_k == 0:
            k_glob = pop_to_k(abitanti)
            fascia = next((d for lo,hi,k,d in FASCE_K if k==k_glob), "?")
            feedback.pushInfo(
                "Fattore demografico: manuale "
                + f"{abitanti:,} ab. -> k={k_glob:.2f} ({fascia.strip()})"
            )
            feature_dicts = applica_moltiplicatore_k(
                feature_dicts, lambda _: k_glob
            )

        elif sorg_k == 1:
            if lyr_k is None:
                feedback.pushInfo("Layer comuni non selezionato - k=1.0")
            else:
                pop_field = None
                for _f in lyr_k.fields():
                    if any(x in _f.name().lower()
                           for x in ("pop","abitanti","resid","tot_res","pop_res")):
                        pop_field = _f.name(); break
                if pop_field is None:
                    for _f in lyr_k.fields():
                        if _f.isNumeric():
                            pop_field = _f.name(); break
                if pop_field is None:
                    feedback.pushInfo("Nessun campo pop trovato - k=1.0")
                else:
                    feedback.pushInfo("Layer comuni: campo pop = '" + pop_field + "'")
                    cx = (bbox[0]+bbox[2])/2; cy = (bbox[1]+bbox[3])/2
                    pt_c = QgsGeometry.fromPointXY(QgsPointXY(cx, cy))
                    crs_com = lyr_k.crs()
                    if crs_com != _crs_4326:
                        tr_c = QgsCoordinateTransform(
                            _crs_4326, crs_com, QgsProject.instance()
                        )
                        pt_c = QgsGeometry.fromPointXY(
                            QgsPointXY(tr_c.transform(QgsPointXY(cx,cy)))
                        )
                    k_com = 1.0
                    for fc in lyr_k.getFeatures():
                        if fc.geometry().intersects(pt_c):
                            try:
                                k_com = pop_to_k(float(fc[pop_field] or 0))
                                feedback.pushInfo(
                                    "Comune: pop="
                                    + str(int(fc[pop_field] or 0))
                                    + " -> k=" + str(k_com)
                                )
                            except (TypeError, ValueError):
                                pass
                            break
                    feature_dicts = applica_moltiplicatore_k(
                        feature_dicts, lambda _: k_com
                    )

        elif sorg_k == 2:
            if lyr_k is None:
                feedback.pushInfo("Layer zonizzazione non selezionato - k=1.0")
            else:
                fnames_k = [_f.name() for _f in lyr_k.fields()]
                if "dz_k" not in fnames_k:
                    feedback.reportError(
                        "Il layer zonizzazione non ha il campo 'dz_k'. "
                        "Aggiungilo con un Double (es. 0.35, 1.0, 1.60).\n"
                        "Campi opzionali supportati:\n"
                        "  dz_k_extra (Double) — k aggiuntivo per turismo/pendolari/eventi\n"
                        "  dz_k_modo  (String) — 'somma' (default) o 'massimo'\n"
                        "  dz_nome    (String) — etichetta descrittiva della zona", False
                    )
                    feedback.pushInfo("Continuo con k=1.0")
                else:
                    has_extra = "dz_k_extra" in fnames_k
                    has_modo  = "dz_k_modo"  in fnames_k
                    feedback.pushInfo(
                        "Layer zonizzazione: '" + lyr_k.name()
                        + "' (" + str(lyr_k.featureCount()) + " zone)"
                    )
                    feedback.pushInfo(
                        "  dz_k_extra: " + ("presente" if has_extra else "assente (k_extra=0)")
                        + " | dz_k_modo: " + ("presente" if has_modo else "assente (default=somma)")
                    )
                    feature_dicts = applica_moltiplicatore_k(
                        feature_dicts,
                        lambda g: _k_da_layer(g, lyr_k, _crs_4326)
                    )

        # ── Crea layer in MEMORIA, popola le feature, poi salva su GPKG ────
        # Strategia: memory layer -> addFeatures -> writeAsVectorFormatV3/Exporter
        # Evita il problema startEditing/commitChanges su layer OGR appena creato.
        from .arete_engine import crea_layer_cv, _configura_form_cv

        # Determina CRS UTM dal centro del bbox (geometrie OSM sono in 4326)
        _cx_utm = (bbox[0] + bbox[2]) / 2
        _cy_utm = (bbox[1] + bbox[3]) / 2
        _zone   = int((_cx_utm + 180) / 6) + 1
        _epsg   = 32600 + _zone if _cy_utm >= 0 else 32700 + _zone
        _crs_out = QgsCoordinateReferenceSystem(f"EPSG:{_epsg}")
        feedback.pushInfo("CRS output CV: EPSG:" + str(_epsg))

        # Crea layer in memoria nel CRS UTM
        lyr_mem, err = crea_layer_cv(None, crs=_crs_out)
        if err:
            feedback.reportError(err, True)
            return {}

        CAMPI_CV = (
            "cv_nome", "cv_tipo", "cv_prob", "cv_vei_g", "cv_vel_kmh",
            "cv_ped_g",
            "cv_ore_flat", "cv_metodo", "cv_ore_occ",
            "cv_sup_mq", "cv_per_giorno", "cv_giorni_anno",
            "cv_valore_eu",
            "cv_b_alb", "cv_b_bra", "cv_fonte", "cv_note",
        )

        # Trasformazione 4326 → UTM per le geometrie OSM
        _tr_to_utm = QgsCoordinateTransform(
            _crs_4326_osm, _crs_out, QgsProject.instance()
        )
        feats_qgs = []
        for fd in feature_dicts:
            if feedback.isCanceled():
                break
            geom = fd.get("geom")
            if geom is None or geom.isEmpty():
                continue
            # Normalizza geometria (rimuove anelli invalidi da difference())
            geom = geom.makeValid() if hasattr(geom, "makeValid") else geom
            if geom is None or geom.isEmpty():
                continue
            # Riproietta da 4326 a UTM
            geom.transform(_tr_to_utm)
            feat = QgsFeature(lyr_mem.fields())
            feat.setGeometry(geom)
            for campo in CAMPI_CV:
                if campo in fd:
                    feat.setAttribute(campo, fd[campo])
            feats_qgs.append(feat)

        lyr_mem.dataProvider().addFeatures(feats_qgs)
        lyr_mem.updateExtents()
        feedback.pushInfo(f"Feature in memoria: {lyr_mem.featureCount()}")

        # Salva su GeoPackage
        LAYER_NAME = "carta_vulnerabilita"
        saved = False
        save_err = ""

        # Tentativo 1: writeAsVectorFormatV3 (QGIS >= 3.20)
        try:
            from qgis.core import QgsVectorFileWriter
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName   = "GPKG"
            opts.layerName    = LAYER_NAME
            opts.fileEncoding = "UTF-8"
            opts.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile
            )
            res = QgsVectorFileWriter.writeAsVectorFormatV3(
                lyr_mem, gpkg, lyr_mem.transformContext(), opts
            )
            code = res[0] if isinstance(res, tuple) else res
            if code == QgsVectorFileWriter.NoError:
                saved = True
            else:
                save_err = f"writeAsVectorFormatV3 code={code}"
        except Exception as ex:
            save_err = str(ex)

        # Tentativo 2: QgsVectorLayerExporter
        if not saved:
            try:
                from qgis.core import QgsVectorLayerExporter
                uri = "GPKG:" + gpkg + ":" + LAYER_NAME
                code2, msg2 = QgsVectorLayerExporter.exportLayer(
                    lyr_mem, uri, "ogr",
                    lyr_mem.crs(), False,
                    {"driverName": "GPKG", "layerName": LAYER_NAME,
                     "overwrite": True},
                )
                if isinstance(code2, tuple):
                    code2, msg2 = code2
                if code2 == QgsVectorLayerExporter.NoError:
                    saved = True
                else:
                    save_err += f" | Exporter code={code2} {msg2}"
            except Exception as ex:
                save_err += " | " + str(ex)

        if not saved:
            feedback.reportError(
                "Impossibile salvare il GeoPackage: " + save_err
                + " - Percorso: " + gpkg, True
            )
            return {}

        # Ricarica dal file e aggiunge al progetto
        lyr = QgsVectorLayer(
            gpkg + "|layername=" + LAYER_NAME, LAYER_NAME, "ogr"
        )
        if not lyr.isValid():
            lyr = QgsVectorLayer(gpkg, LAYER_NAME, "ogr")
        if not lyr.isValid():
            feedback.reportError(
                f"GeoPackage salvato ma non apribile: {gpkg}", True
            )
            return {}

        _configura_form_cv(lyr)

        # Inizializza editing + indice spaziale (fix snap/selezione/salva)
        if lyr.providerType() == "ogr":
            lyr.startEditing()
            lyr.commitChanges()
            prov = lyr.dataProvider()
            if prov:
                prov.createSpatialIndex()
            lyr.updateExtents()

        QgsProject.instance().addMapLayer(lyr)

        n = lyr.featureCount()
        feedback.pushInfo("")
        feedback.pushInfo("Carta Vulnerabilita' generata: " + str(n) + " poligoni")
        feedback.pushInfo("Percorso: " + gpkg)
        feedback.pushInfo("")
        feedback.pushInfo("REVISIONE CONSIGLIATA:")
        feedback.pushInfo("  - Controlla cv_vei_g e cv_ped_g sulle strade principali")
        feedback.pushInfo("  - Verifica cv_valore_eu sugli edifici importanti")
        feedback.pushInfo("  - Aggiusta cv_prob per zone con orari particolari")
        feedback.pushInfo("  - Rimuovi i poligoni non pertinenti agli alberi")
        if sorg_k == 2 and lyr_k is not None:
            feedback.pushInfo("")
            feedback.pushInfo(
                "  FATTORE K zonizzazione: cv_note di ogni poligono riporta il k applicato."
            )

        return {self.OUTPUT: gpkg}
