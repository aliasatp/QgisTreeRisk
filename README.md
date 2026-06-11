# 🌳 QgisTreeRisk

**Valutazione del Rischio Arboreo secondo il Protocollo Areté® v4.0**

Plugin QGIS open source per la stima del rischio di cedimento arboreo su grandi dataset. Calcola la classe di rischio per i quattro settori analitici (radici, colletto, fusto, branche) con giudizio ordinario VRA e speditiva triage.
> Guida online:  [manuale](https://aliasatp.github.io/QgisTreeRisk/).
> 
> Tutorial: [video](https://youtube.com/playlist?list=PL8Ge4eRAVkxZ3ik1aOr48S_wDr5ZC6i2J&si=9n2qngdckBv7UzOK).
> 
> Guida per Carta della vulnerabilità da OSM: [manuale](https://aliasatp.github.io/QgisTreeRisk/help.html).
> 

> ⚠️ **STRUMENTO AD USO ESCLUSIVAMENTE DIDATTICO — NON OPERATIVO.**  
> I risultati non costituiscono perizia professionale né hanno valenza legale o assicurativa.  
> Per valutazioni ufficiali consultare un tecnico abilitato: [protocolloarete.it](http://www.protocolloarete.it).


---

## A cosa serve

| | Funzionalità | Descrizione |
|---|---|---|
| 🗺️ | **Quadro conoscitivo pianificatorio** | Elabora interi patrimoni arborei con stima automatica del bersaglio da OpenStreetMap (tramite Overpass API) o da carta della vulnerabilità personalizzata (schema standard per metodo Areté). Utile per definire priorità di intervento in fase di piano del verde. |
| 🗄️ | **Elaborazione massiva di dataset** | Importa il tuo inventario arboreo (CSV, Shapefile, GeoPackage), mappa i campi biometrici e il POF e ottieni livello di rischio, giudizio ordinario e triage per ogni albero utilizzando come fonte per definire la classe di bersaglio tre possibili sorgenti: dati presenti nel tuo inventario, derivazione della CV (carta vulnerabilità) eleaborata a partire da OSM, derivazione da tua CV. |
| 📐 | **Carta della Vulnerabilità probabilistica** | Predispone un Layer poligonale personalizzabile (CV - carta vulnerabilità) con parametri reali di frequentazione o precompilati da OSM (tramite Overpass API). Novità: Supporto metodo geometrico SPOT/SDAN per la stima dell'occupazione. Il metodo SPOT/SDAN proporziona l'occupazione stabile all'area di caduta reale — parchi e cortili ricevono un bersaglio geometricamente corretto, non una stima piatta sull'intera superficie. |
| 👥 | **Fattore k per aree turistiche/pendolari** | La CV può essere zonizzata in funzione di fattori quali: affluenza turistica, pendolari, eventi culturali/sportivi che vanno a sommarsi col parametro demografico. Sono disponibili 9 fasce demografiche (XXS <500 ab. → XXL >2M ab.) con baseline a 30.000–80.000 ab. Il campo `dz_k_extra` scala il bersaglio per turismo, pendolari ed eventi stagionali. |

---

## Installazione

**Requisiti:** QGIS 3.22 o superiore (incluso QGIS 4.x) · connessione internet per la stima OSM

### Da file ZIP (metodo consigliato)

1. Scarica `arete_vra_plugin.zip` dalla pagina [Releases](https://github.com/aliasatp/QgisTreeRisk/releases)
2. In QGIS: `Plugin → Gestisci e Installa Plugin → Installa da ZIP`
3. Seleziona il file scaricato e clicca **Installa plugin**
4. Apri `Processing → Cassetta degli Strumenti → QgisTreeRisk`

---

## Algoritmi disponibili

Il plugin aggiunge il gruppo **QgisTreeRisk** nella Cassetta degli Strumenti di Processing:

| Algoritmo | Descrizione |
|---|---|
| `Valutazione Rischio Arboreo` | Dialog interattivo principale — calcola il rischio per ogni albero del layer |
| `Crea Carta Vulnerabilità` | Genera un GeoPackage vuoto con schema standard e form attributi preconfigurato |
| `Genera CV da OSM` | (tramite Overpass API) Scarica strade, edifici, landuse e POI da OpenStreetMap e precompila la CV |
| `Crea layer Comuni` | Genera il layer poligonale per i confini comunali con campo `pop_res` |
| `Crea layer Zonizzazione` | Genera il layer poligonale per la zonizzazione demografica (`dz_k`, `dz_k_extra`) |

---

## Campi di output principali

Il layer `VRA_Arete_v4` (punti) e il layer `SPOT_*` (poligoni, raggio = altezza albero) vengono generati automaticamente nel **CRS UTM** coerente con il layer sorgente.

| Campo | Descrizione |
|---|---|
| `R_radici / R_colletto / R_fusto / R_branche` | Livello di rischio per settore (es. `1:10k`, `<1:1M`) |
| `Rg_radici / … / Rg_branche` | Giudizio ordinario VRA per settore |
| `Rs_radici / … / Rs_branche` | Speditiva triage per settore |
| `Rv_radici / … / Rv_branche` | Gravità numerica per settore (1–6) |
| `R_peggiore` | Livello di rischio complessivo più gravoso |
| `R_giudizio` | Giudizio ordinario complessivo |
| `R_speditiva` | Speditiva triage complessiva |
| `Ba_finale / Bb_finale` | Classe bersaglio albero / branca (1–7, 9=assente) |
| `CF_albero / CF_branca` | Classe di Impulso (energia cedimento) |

---

## Metodo SPOT/SDAN — stima probabilistica dell'occupazione

Per aree estese (parchi, cortili, pertinenze) il metodo flat sovrastima il bersaglio. Il plugin implementa il calcolo geometrico che proporziona l'occupazione all'area di influenza reale dell'albero:

```
SPOT_netta = (15² × π) × 1.00   = 706.86 m²
SDAN       = (15/2) × (8/2) × π = 7.5 × 4 × π = 94.25 m²
r          = 94.25 / 706.86     = 0.1334
hpy        = 400 × 210 × 2      = 168.000
k          = 168.000 / 8760     = 19.178
pmqspot    = 19.178 / 2800      = 0.006849
pspot      = 0.006849 × 706.86 = 4.842
psdan      = 4.842 × 0.1334    = 0.6459  →  64.59%  →  classe 1
```

---

## Fattore k demografico — 9 fasce

| Fascia | Abitanti | k |
|---|---|---|
| XXS | < 500 | 0.05 |
| XS | 500 – 2.000 | 0.15 |
| S | 2.000 – 10.000 | 0.35 |
| M- | 10.000 – 30.000 | 0.65 |
| **M (baseline)** | **30.000 – 80.000** | **1.00** |
| M+ | 80.000 – 200.000 | 1.40 |
| L | 200.000 – 500.000 | 1.80 |
| XL | 500.000 – 2.000.000 | 2.50 |
| XXL | > 2.000.000 | 3.50 |

Campi del layer zonizzazione: `dz_k` (obbligatorio) · `dz_k_extra` (turismo/eventi) · `dz_k_modo` (`somma` o `massimo`)

---

## Licenze

**Plugin QgisTreeRisk** — © ALIAS ATP — [GPL 2.0](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html)

**Protocollo Areté® v4.0** — Il plugin riproduce parzialmente la logica di calcolo del Protocollo Areté® — ARBORETE® a fini esclusivamente dimostrativi e formativi.  
Il Protocollo è distribuito con licenza **CC BY-NC-ND 4.0** (Attribuzione – Non commerciale – Non opere derivate).  
L'utilizzo non è consentito per scopi commerciali e non è ammessa la distribuzione di opere derivate senza autorizzazione degli autori.

**Citazione obbligatoria:**
> *"Protocollo Areté® per la Valutazione del Rischio Arboreo [ver. 4.0] — ARBORETE® (http://www.protocolloarete.it)"*

---

## Link utili

- 🌐 [Pagina GitHub Pages](https://aliasatp.github.io/QgisTreeRisk)
- 📋 [Protocollo Areté® ufficiale](http://www.protocolloarete.it)
- 🐛 [Segnala un problema](https://github.com/aliasatp/QgisTreeRisk/issues)
- 🏢 [ALIAS ATP](https://www.aliasinfo.it)

---

<sub>Strumento ad uso esclusivamente didattico · Non sostituisce la perizia di un tecnico abilitato</sub>

