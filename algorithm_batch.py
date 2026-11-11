# -*- coding: utf-8 -*-
"""
Arete VRA - Algoritmo batch Processing
Appare nella galleria come:
  Arete VRA > Valutazione Rischio Arboreo (Batch / Modello)

Usa i nomi canonici dei campi (h, d_ch, circonf, ecc.) letti
direttamente dal layer - adatto per Modellatore e Batch Processing.
"""

from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsProcessing,
    QgsFeature,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFields,
    QgsWkbTypes,
)


class AreteVRABatchAlgorithm(QgsProcessingAlgorithm):

    INPUT   = "INPUT"
    OUTPUT  = "OUTPUT"
    H       = "H"
    D_CH    = "D_CH"
    CIRCONF = "CIRCONF"
    H_BERS  = "H_BERS"
    D_BR    = "D_BR"
    L_BR    = "L_BR"
    H_INS   = "H_INS"
    B_MAN   = "B_MANUALE"

    def name(self):
        return "arete_vra_batch"

    def displayName(self):
        return "Valutazione Rischio Arboreo (Batch / Modello)"

    def group(self):
        return "Arete VRA"

    def groupId(self):
        return "arete_vra"

    def shortHelpString(self):
        return (
            "Calcola il Rischio Arboreo (Protocollo Arete v4.0) in modalita' "
            "batch, adatta al Modellatore grafico e all'elaborazione in serie.\n\n"
            "I parametri biometrici vengono letti dai campi del layer "
            "se i campi hanno i nomi canonici:\n"
            "  h, d_ch, circonf, h_bers, d_br, l_br, h_ins\n"
            "  pof_radici, pof_colletto, pof_fusto, pof_branche\n"
            "  molt, B_manuale\n\n"
            "Se i campi non esistono si usano i valori default inseriti "
            "nei parametri qui sotto.\n\n"
            "RAGGI OSM:\n"
            "  Albero (radici/colletto/fusto): raggio = altezza albero\n"
            "  Branca: raggio = diametro chioma / 2\n\n"
            "Per il dialog interattivo con dropdown usare:\n"
            "  Arete VRA > Valutazione Rischio Arboreo (Dialog interattivo)\n\n"
            "Riferimento: Protocollo Arete(r) v4.0 - ARBORETE(r)\n"
            "www.protocolloarete.it - CC BY-NC-ND 4.0"
        )

    def createInstance(self):
        return AreteVRABatchAlgorithm()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, "Layer alberi (punti)",
            types=[QgsProcessing.TypeVectorPoint]
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.H, "Altezza albero default - campo: h (m)",
            defaultValue=12.0, minValue=0.1,
            type=QgsProcessingParameterNumber.Double
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.D_CH, "Diametro chioma default - campo: d_ch (m)",
            defaultValue=6.0, minValue=0.1,
            type=QgsProcessingParameterNumber.Double
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.CIRCONF, "Circonferenza tronco default - campo: circonf (cm)",
            defaultValue=80.0, minValue=1.0,
            type=QgsProcessingParameterNumber.Double
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.H_BERS, "Altezza bersaglio default - campo: h_bers (m)",
            defaultValue=1.8, minValue=0.0,
            type=QgsProcessingParameterNumber.Double
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.D_BR, "Diametro branca default - campo: d_br (cm)",
            defaultValue=10.0, minValue=0.1,
            type=QgsProcessingParameterNumber.Double
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.L_BR, "Lunghezza branca default - campo: l_br (m)",
            defaultValue=3.0, minValue=0.1,
            type=QgsProcessingParameterNumber.Double
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.H_INS, "Inserzione branca default - campo: h_ins (m)",
            defaultValue=6.0, minValue=0.0,
            type=QgsProcessingParameterNumber.Double
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.B_MAN,
            "Classe Bersaglio globale (0=da OSM, 1-7=manuale, 9=assente)",
            defaultValue=0, minValue=0, maxValue=9,
            type=QgsProcessingParameterNumber.Integer
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "VRA Arete - risultati"
        ))

    def processAlgorithm(self, parameters, context, feedback):
        from .arete_engine import (
            stima_bersaglio_albero, stima_bersaglio_branca,
            classe_fisica_albero, classe_fisica_branca,
            calc_rischio, rischio_peggiore,
            build_output_fields,
        )

        layer    = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        h_def    = self.parameterAsDouble(parameters, self.H, context)
        d_ch_def = self.parameterAsDouble(parameters, self.D_CH, context)
        circ_def = self.parameterAsDouble(parameters, self.CIRCONF, context)
        hb_def   = self.parameterAsDouble(parameters, self.H_BERS, context)
        d_br_def = self.parameterAsDouble(parameters, self.D_BR, context)
        l_br_def = self.parameterAsDouble(parameters, self.L_BR, context)
        h_ins_def = self.parameterAsDouble(parameters, self.H_INS, context)
        b_man    = self.parameterAsInt(parameters, self.B_MAN, context)

        if layer is None:
            raise QgsProcessingException("Layer non trovato.")

        crs_src   = layer.crs()
        crs_wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        tr        = QgsCoordinateTransform(crs_src, crs_wgs84, context.project())

        existing_fields = layer.fields()
        out_field_list  = build_output_fields(existing_fields)
        out_qgs_fields  = QgsFields()
        for f in out_field_list:
            out_qgs_fields.append(f)

        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context,
            out_qgs_fields, QgsWkbTypes.Point, crs_src
        )

        fnames = [f.name() for f in existing_fields]

        def fv(feat, campo, default):
            if campo in fnames:
                v = feat[campo]
                if v is not None and str(v) not in ("NULL", "None", ""):
                    try:
                        return type(default)(v)
                    except (ValueError, TypeError):
                        pass
            return default

        total = layer.featureCount()

        for i, feat in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                break
            if total > 0:
                feedback.setProgress(int(i / total * 100))

            geom = feat.geometry()
            if geom.isEmpty():
                continue

            pt  = tr.transform(geom.centroid().asPoint())
            lat = pt.y()
            lon = pt.x()

            h    = fv(feat, "h",      h_def)
            d_ch = fv(feat, "d_ch",   d_ch_def)
            circ = fv(feat, "circonf",circ_def)
            hb   = fv(feat, "h_bers", hb_def)
            db   = fv(feat, "d_br",   d_br_def)
            lb   = fv(feat, "l_br",   l_br_def)
            hi   = fv(feat, "h_ins",  h_ins_def)
            pof1 = int(fv(feat, "pof_radici",   9))
            pof2 = int(fv(feat, "pof_colletto", 9))
            pof3 = int(fv(feat, "pof_fusto",    9))
            pof4 = int(fv(feat, "pof_branche",  9))
            molt = int(fv(feat, "molt",          1))
            bm   = int(fv(feat, "B_manuale",     b_man))

            if int(bm) in range(1, 8):
                ba_fin = bb_fin = int(bm)
                ba_veic = ba_ped = ba_occ = bb_veic = bb_ped = bb_occ = -1
                ba_info = {"raggio_m": int(round(h)),
                           "strada": "manuale", "vel": 0, "vei_g": 0, "ped_g": 0}
                bb_info = {"raggio_m": max(int(round(d_ch / 2)), 3),
                           "strada": "manuale", "vel": 0, "vei_g": 0, "ped_g": 0}
            else:
                ba_veic, ba_ped, ba_occ, ba_fin, ba_info = \
                    stima_bersaglio_albero(lat, lon, h, d_ch)
                bb_veic, bb_ped, bb_occ, bb_fin, bb_info = \
                    stima_bersaglio_branca(lat, lon, d_ch, lb)

            cfa, e_alb = classe_fisica_albero(h, circ, hb)
            cfb, e_bra = classe_fisica_branca(db, lb, hi, hb)

            r_rad = calc_rischio(ba_fin, cfa, pof1, molt)
            r_col = calc_rischio(ba_fin, cfa, pof2, molt)
            r_fus = calc_rischio(ba_fin, cfa, pof3, molt)
            r_bra = calc_rischio(bb_fin, cfb, pof4, molt)
            r_peg = rischio_peggiore(r_rad, r_col, r_fus, r_bra)

            feedback.pushInfo(
                "id=" + str(feat.id())
                + " B_alb=" + str(ba_fin)
                + " B_bra=" + str(bb_fin)
                + " R=" + r_peg["r"]
            )

            out_feat = QgsFeature(out_qgs_fields)
            out_feat.setGeometry(feat.geometry())
            out_feat.setAttributes(feat.attributes() + [
                ba_veic, ba_ped, ba_occ, ba_fin,
                ba_info["raggio_m"], ba_info["strada"],
                ba_info["vel"], ba_info["vei_g"], ba_info["ped_g"],
                bb_veic, bb_ped, bb_occ, bb_fin,
                bb_info["raggio_m"], bb_info["strada"],
                bb_info["vel"], bb_info["vei_g"], bb_info["ped_g"],
                cfa, cfb, round(e_alb, 1), round(e_bra, 1),
                r_rad["r"], r_col["r"], r_fus["r"], r_bra["r"],
                r_peg["r"], r_peg["colore"],
                r_peg["giudizio"], r_peg["speditiva"], r_peg["gr"],
            ])
            sink.addFeature(out_feat)

        return {self.OUTPUT: dest_id}
