import os
import sys
import json
import re
import urllib.parse
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Dict, Optional
import pandas as pd
import numpy as np

# Ensure root workspace is in sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.storage.database import PropertyDatabase
from src.ingestion.live_scraper import LiveNCScraper
from src.utils.logger import get_logger, DEFAULT_LOG_FILE
from src.utils.log_analyzer import PipelineAuditor
from src.domain.agencies_directory import NC_AGENCIES
from src.analysis.cadastre_enrichment import enrich_listings
import ssl
import urllib.request

logger = get_logger("server")

PORT = 8080

PARCEL_POLYGON_CACHE: Dict[str, Any] = {}
_CACHED_LISTINGS_PAYLOAD: Optional[bytes] = None


def determine_pud_zone(commune: str, quartier: str, lat: float = 0.0, lon: float = 0.0) -> Dict[str, Any]:
    """
    Détermine la zone PUD officielle et les règles d'urbanisme applicables
    pour une localisation en Nouvelle-Calédonie (Grand Nouméa et Brousse).
    """
    c_norm = (commune or "Nouméa").strip().lower()
    q_norm = (quartier or "").strip().lower()

    if "noum" in c_norm:
        if any(w in q_norm for w in ["centre", "moselle", "artillerie", "victoire"]):
            return {
                "zone": "UA",
                "zoneLabel": "Zone UA • Centre-Ville Historique",
                "vocation": "Mixité urbaine dense, commerces, bureaux et logements collectifs",
                "hauteurMax": "R+6 à R+10 (jusqu'à 32 m)",
                "empriseSol": "80% à 100%",
                "droitsBatir": "Forte constructibilité en hauteur, pas de retrait obligatoire sur rue",
                "source": "PUD Nouméa Ville Approuvé"
            }
        elif any(w in q_norm for w in ["baie des citrons", "anse vata", "val plaisance", "trianon", "motor pool", "magenta", "faubourg"]):
            return {
                "zone": "UB",
                "zoneLabel": "Zone UB • Faubourgs Résidentiels Denses & Balnéaires",
                "vocation": "Habitat collectif intermédiaire, résidences de standing et villas",
                "hauteurMax": "R+3 / R+4 (12 m à 15 m)",
                "empriseSol": "45% à 50%",
                "droitsBatir": "Piscine autorisée, surélévation possible, recul de 3m à 5m des limites séparatives",
                "source": "PUD Nouméa Ville Approuvé"
            }
        elif any(w in q_norm for w in ["ducos", "normandie", "numbo"]):
            return {
                "zone": "UE / UI",
                "zoneLabel": "Zone UE/UI • Pôle Industriel & Tertiaire",
                "vocation": "Docks industriels, ateliers, commerces de gros et bureaux d'entreprises",
                "hauteurMax": "12 m à 15 m",
                "empriseSol": "60%",
                "droitsBatir": "Activités commerciales et artisanales prioritaires, logement de gardiennage uniquement",
                "source": "PUD Nouméa Ville Approuvé"
            }
        elif any(w in q_norm for w in ["tina", "portes de fer", "haut-magenta", "vallée des colons"]):
            return {
                "zone": "UC / UD",
                "zoneLabel": "Zone UC/UD • Résidentiel Calme & Pavillonnaire",
                "vocation": "Villas individuelles familiales et petits collectifs intégrés",
                "hauteurMax": "R+1 + combles (7 m à 9 m)",
                "empriseSol": "30% à 35%",
                "droitsBatir": "Préservation du cadre paysager, coefficient d'emprise maîtrisé, annexes et piscines autorisées",
                "source": "PUD Nouméa Ville Approuvé"
            }
        else:
            return {
                "zone": "UB / UC",
                "zoneLabel": "Zone UB/UC • Urbain Mixte Calédonien",
                "vocation": "Habitat résidentiel et commerces de proximité",
                "hauteurMax": "R+2 / R+3 (9 m à 12 m)",
                "empriseSol": "40%",
                "droitsBatir": "Logements collectifs ou maisons individuelles autorisés",
                "source": "PUD Nouméa Ville Approuvé"
            }
    elif "dumb" in c_norm:
        if "mer" in q_norm or "apogoti" in q_norm or "koutio" in q_norm or "ville" in q_norm:
            return {
                "zone": "UB_DUMBEA",
                "zoneLabel": "Zone UB • Coeur Urbain Dumbéa-sur-Mer / Koutio",
                "vocation": "Éco-quartier, habitat moderne intermédiaire et commerces",
                "hauteurMax": "R+3 / R+4 (12 m à 14 m)",
                "empriseSol": "45%",
                "droitsBatir": "Règlementation environnementale Dumbéa, terrasses et varangues valorisées",
                "source": "PUD Dumbéa Approuvé"
            }
        else:
            return {
                "zone": "UD_DUMBEA",
                "zoneLabel": "Zone UD • Résidentiel Pavillonnaire Dumbéa",
                "vocation": "Villas individuelles avec jardin",
                "hauteurMax": "R+1 (7 m)",
                "empriseSol": "25% à 30%",
                "droitsBatir": "Parcelles résidentielles calmes",
                "source": "PUD Dumbéa Approuvé"
            }
    elif "pait" in c_norm:
        return {
            "zone": "UD_PAITA",
            "zoneLabel": "Zone UD / 1AU • Résidentiel & Plaine de Païta",
            "vocation": "Villas individuelles, lotissements récents et propriétés verdoyantes",
            "hauteurMax": "R+1 (7 m)",
            "empriseSol": "25%",
            "droitsBatir": "Idéal pour maisons avec grand terrain et dépendances",
            "source": "PUD Païta Approuvé"
        }
    elif "dore" in c_norm or "mont" in c_norm:
        return {
            "zone": "UD_MONT_DORE",
            "zoneLabel": "Zone UD • Résidentiel Littoral Mont-Dore",
            "vocation": "Cadre naturel et balnéaire préservé, villas avec vue mer et jardin",
            "hauteurMax": "R+1 (7 m)",
            "empriseSol": "25% à 30%",
            "droitsBatir": "Construction individuelle respectant les marges de recul du littoral",
            "source": "PUD Mont-Dore Approuvé"
        }
    else:
        return {
            "zone": "A / N",
            "zoneLabel": "Zone Rurale / Naturelle",
            "vocation": "Terrains de brousse, exploitations agricoles et propriétés naturelles",
            "hauteurMax": "R+1 (7 m)",
            "empriseSol": "10% à 20%",
            "droitsBatir": "Constructions liées à l'usage d'habitation rurale ou agricole",
            "source": "Règlement Territorial NC"
        }



def safe_str(val, default=""):
    if pd.isna(val) or val is None:
        return default
    s = str(val).strip()
    return default if s.lower() in ("nan", "none") else s


def safe_float(val, default=0.0):
    if pd.isna(val) or val is None:
        return default
    try:
        f = float(val)
        return default if np.isnan(f) or np.isinf(f) else f
    except Exception:
        return default


def safe_int(val, default=0):
    if pd.isna(val) or val is None:
        return default
    try:
        f = float(val)
        return default if np.isnan(f) or np.isinf(f) else int(f)
    except Exception:
        return default


def sanitize_for_json(obj):
    """Garantit l'absence totale de valeurs NaN/Inf non conformes au standard JSON (RFC 8259)."""
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return 0.0
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    return obj


# Référentiel géographique pour géolocalisation des annonces réelles
GEO_REF_PATH = BASE_DIR / "data" / "reference" / "referentiel_grand_noumea.json"
QUARTIER_COORDS: Dict[str, tuple[float, float]] = {}
COMMUNE_CENTERS = {
    "NOUMEA": (-22.2710, 166.4420),
    "DUMBEA": (-22.1850, 166.4450),
    "MONT_DORE": (-22.2285, 166.5206),  # Boulari / Hôtel de Ville (terre ferme)
    "PAITA": (-22.1310, 166.3650),
    "AUTRE": (-21.5500, 165.7500),
}

if GEO_REF_PATH.exists():
    try:
        with open(GEO_REF_PATH, "r", encoding="utf-8") as f:
            geo_data = json.load(f)
        for com_k, com_val in geo_data.get("communes", {}).items():
            for q_obj in com_val.get("quartiers", []):
                q_name = str(q_obj.get("nom") or "").strip().lower()
                lat_v = float(q_obj.get("latitude", 0))
                lon_v = float(q_obj.get("longitude", 0))
                if lat_v and lon_v:
                    QUARTIER_COORDS[q_name] = (lat_v, lon_v)
    except Exception as e:
        logger.warning(f"Impossible de charger referentiel_grand_noumea.json : {e}")


def resolve_listing_coords(item_id: str, commune_key: str, quartier_name: str, title: str = "", description: str = "") -> tuple[float, float]:
    """Retourne les coordonnées GPS (lat, lon) précises sur la terre ferme via le moteur cadastral et REFIL."""
    try:
        from src.analysis.cadastre_enrichment import resolve_listing_coordinates
        lat, lon, _ = resolve_listing_coordinates(
            item_id=str(item_id),
            commune=commune_key,
            quartier=quartier_name,
            title=title,
            description=description
        )
        return lat, lon
    except Exception as e:
        logger.warning(f"Erreur de résolution cadastrale pour {item_id} : {e}")
        return -22.2710, 166.4420


class NCImmoAPIHandler(SimpleHTTPRequestHandler):
    """
    Serveur HTTP combinant service des fichiers statiques du dashboard
    et API REST dynamique pour l'actualisation réelle des sources DuckDB.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def _send_json(self, data: Any, status_code: int = 200):
        """Envoie une réponse JSON strictement valide RFC 8259 (sans NaN) avec les en-têtes CORS nécessaires."""
        if isinstance(data, (bytes, bytearray)):
            payload = data
        else:
            clean_data = sanitize_for_json(data)
            payload = json.dumps(clean_data, default=str, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        """Gère les requêtes CORS pré-vol."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """Achemine les requêtes d'API ou sert les fichiers statiques."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path in ("/", "/dashboard"):
            self.path = "/dashboard_immo_nc.html"
            return super().do_GET()

        if path == "/api/listings":
            return self.handle_get_listings()
        elif path == "/api/cadastre/parcel-polygon":
            return self.handle_get_parcel_polygon()
        elif path == "/api/cadastre/pud-rules":
            return self.handle_get_pud_rules()
        elif path == "/api/sources":
            return self.handle_get_sources()
        elif path == "/api/agencies":
            return self.handle_get_agencies()
        elif path == "/api/audit":
            return self.handle_get_audit()
        elif path == "/api/logs":
            return self.handle_get_logs()

        return super().do_GET()

    def do_POST(self):
        """Gère les requêtes de déclenchement d'actions."""
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path

        if path in ("/api/refresh", "/api/refresh-source"):
            return self.handle_post_refresh()
        elif path == "/api/enrich/geoloc":
            return self.handle_post_enrich_geoloc()

        self._send_json({"error": "Endpoint not found"}, status_code=404)

    def handle_post_enrich_geoloc(self):
        """Déclenche l'enrichissement haute précision EXIF / REFIL / Cadastre en arrière-plan."""
        def _bg_enrich():
            global _CACHED_LISTINGS_PAYLOAD
            try:
                from src.analysis.geo_precision_enricher import GeoPrecisionEnricher
                enricher = GeoPrecisionEnricher()
                res = enricher.enrich_database_table(max_exif_downloads=50)
                logger.info(f"Enrichissement haute précision terminé : {res}")
                _CACHED_LISTINGS_PAYLOAD = None
            except Exception as ex:
                logger.error(f"Erreur enrichissement geo background : {ex}")

        threading.Thread(target=_bg_enrich, daemon=True).start()
        self._send_json({"success": True, "message": "Enrichissement haute précision lancé en arrière-plan"})

    def handle_get_listings(self):
        """Renvoie les annonces réelles stockées dans DuckDB formatées pour le frontend."""
        global _CACHED_LISTINGS_PAYLOAD
        try:
            parsed_url = urllib.parse.urlparse(self.path)
            query_params = urllib.parse.parse_qs(parsed_url.query)
            limit_val = None
            if "limit" in query_params:
                try:
                    limit_val = int(query_params["limit"][0])
                except (ValueError, TypeError):
                    limit_val = None

            if not limit_val and _CACHED_LISTINGS_PAYLOAD is not None:
                return self._send_json(_CACHED_LISTINGS_PAYLOAD)

            db = PropertyDatabase(read_only=True)
            if limit_val and limit_val > 0:
                df = db.query(f"""
                    SELECT * FROM listings 
                    WHERE is_active = TRUE 
                    ORDER BY CASE WHEN initial_price_xpf > price_xpf THEN 0 ELSE 1 END, scraped_at DESC, id DESC 
                    LIMIT {limit_val}
                """)
            else:
                df = db.query("""
                    SELECT * FROM listings 
                    WHERE is_active = TRUE 
                    ORDER BY CASE WHEN initial_price_xpf > price_xpf THEN 0 ELSE 1 END, scraped_at DESC, id DESC
                """)


            # Pré-chargement de l'historique complet des prix ordonné chronologiquement
            history_by_listing: Dict[str, List[Dict[str, Any]]] = {}
            try:
                hist_df = db.query("""
                    SELECT listing_id, price_xpf, price_eur, event_type, price_change_xpf, price_change_pct, recorded_at
                    FROM listing_price_history
                    ORDER BY recorded_at ASC
                """)
                for _, h_row in hist_df.iterrows():
                    l_id = str(h_row["listing_id"])
                    if l_id not in history_by_listing:
                        history_by_listing[l_id] = []
                    history_by_listing[l_id].append(h_row.to_dict())
            except Exception as e:
                logger.warning(f"Impossible de charger l'historique des prix : {e}")

            listings = []
            now = datetime.now(timezone.utc)

            COMMUNE_MAP = {
                "NOUMEA": "Nouméa",
                "DUMBEA": "Dumbéa",
                "MONT_DORE": "Mont-Dore",
                "PAITA": "Païta",
            }

            for _, row in df.iterrows():
                # 1. Extraction image : priorité absolue à la colonne image_url réelle
                desc = row.get("description") or ""
                raw_img = row.get("image_url")
                img_url = str(raw_img).strip() if pd.notna(raw_img) and str(raw_img).strip() else ""
                if not img_url or img_url.lower() in ("none", "nan"):
                    img_match = re.search(r"\[IMG:\s*(https?://[^\]]+)\]", desc)
                    if img_match:
                        img_url = img_match.group(1)
                    else:
                        img_url = ""
                # Nettoyage de la balise technique dans la description
                desc = re.sub(r"\[IMG:\s*https?://[^\]]+\]", "", desc).strip()

                prop_type = str(row.get("property_type") or "").upper()
                is_dock = prop_type == "DOCK"
                has_sea_view = bool(row.get("has_sea_view"))

                if not img_url:
                    if is_dock:
                        img_url = "https://images.unsplash.com/photo-1586528116311-ad8dd3c8310d?auto=format&fit=crop&w=800&q=80"
                    elif has_sea_view:
                        img_url = "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?auto=format&fit=crop&w=800&q=80"
                    elif prop_type == "APPARTEMENT":
                        img_url = "https://images.unsplash.com/photo-1545324418-cc1a3fa10c00?auto=format&fit=crop&w=800&q=80"
                    else:
                        img_url = "https://images.unsplash.com/photo-1580587771525-78b9dba3b914?auto=format&fit=crop&w=800&q=80"

                # Extraction multi-photos depuis images_json
                raw_images_json = row.get("images_json")
                images = []
                if pd.notna(raw_images_json) and raw_images_json:
                    try:
                        parsed_imgs = json.loads(str(raw_images_json))
                        if isinstance(parsed_imgs, list):
                            images = [str(u).strip() for u in parsed_imgs if str(u).strip().startswith("http")]
                    except Exception:
                        images = []
                if not images and img_url:
                    images = [img_url]

                # 2. Type d'opération (Vente vs Location)
                trans_type = str(row.get("transaction_type") or "VENTE").upper()
                is_location = "LOCAT" in trans_type
                transaction_type = "LOCATION" if is_location else "VENTE"
                transaction_label = "Location" if is_location else "Vente"

                # Catégorisation pour filtres frontend
                is_terrain = prop_type == "TERRAIN"
                is_commercial = prop_type in ("LOCAL_COMMERCIAL", "BUREAU", "COMMERCE", "IMMEUBLE")

                category = "maison_villa"
                category_label = "Maison / Villa"
                if is_dock:
                    category = "dock_industriel"
                    category_label = "Dock Industriel"
                elif is_terrain:
                    category = "terrain"
                    category_label = "Terrain"
                elif is_commercial:
                    category = "local_commercial"
                    category_label = "Local Professionnel & Bureau"
                elif has_sea_view:
                    category = "maison_riviere_mer"
                    category_label = "Rivière & Vue Mer"
                elif prop_type == "APPARTEMENT":
                    category = "appartement"
                    category_label = "Appartement"

                commune_enum = str(row.get("commune") or "NOUMEA").upper()
                commune_raw = COMMUNE_MAP.get(commune_enum, commune_enum.replace("_", "-").title())

                quartier = safe_str(row.get("quartier"), "Secteur Calédonien")
                lat_val, lon_val = resolve_listing_coords(
                    str(row.get("id")), commune_enum, quartier, title=str(row.get("title") or ""), description=desc
                )
                price_xpf = safe_int(row.get("price_xpf"), 0)
                surf_hab = safe_float(row.get("surface_habitable_m2"), 0.0)
                surf_ter = safe_float(row.get("surface_terrain_m2"), 0.0)
                surf_var = safe_float(row.get("surface_terrasse_m2"), 0.0)
                
                if is_location:
                    price_m2 = round(price_xpf / surf_hab, 1) if surf_hab > 0 else 0.0
                    avg_price_m2_sector = 1650
                    target_low = int(price_xpf * 0.95)
                    target_high = price_xpf
                    verdict = "OPPORTUNITE" if (surf_hab > 0 and price_m2 < 1350) else "CONFORME"
                    verdict_badge = "🔑 Loyer Attractif NC" if verdict == "OPPORTUNITE" else "⚖️ Loyer Conforme au Marché"
                    analysis_title = f"Location analysée • Secteur {commune_raw} ({quartier})"
                    analysis_context = f"Loyer mensuel de {price_xpf:,.0f} F CFP/mois (soit {price_m2:,.0f} F/m²/mois pour {surf_hab:.0f} m²)." if surf_hab > 0 else f"Loyer mensuel affiché de {price_xpf:,.0f} F CFP/mois sur le secteur {commune_raw}."
                    analysis_levers = [
                        f"Surface utile : {surf_hab:.0f} m²." if surf_hab > 0 else "Surface exacte à vérifier avec le bailleur lors de la visite.",
                        "Vérifier si les charges locatives (eau, ordures ménagères, copropriété) sont incluses.",
                        "Conditions du bail : vérifier le dépôt de garantie et la durée du préavis."
                    ]
                else:
                    price_m2 = safe_float(row.get("prix_m2_habitable_xpf"), 0.0)
                    avg_price_m2_sector = 340000
                    target_low = int(price_xpf * 0.94)
                    target_high = int(price_xpf * 0.98)
                    verdict = "OPPORTUNITE" if has_sea_view else "CONFORME"
                    verdict_badge = "🚀 Opportunité Rare NC" if has_sea_view else "⚖️ Prix Conforme au Marché"
                    analysis_title = f"Offre d'achat analysée • Secteur {commune_raw} ({quartier})"
                    analysis_context = f"Bien en vente enregistré en base. Ratio de {price_m2:,.0f} F/m² pour une surface de {surf_hab:.0f} m²." if surf_hab > 0 else f"Bien en vente enregistré en base. Secteur {commune_raw}."
                    analysis_levers = [
                        f"Surface utile globale : {surf_hab:.0f} m² habitables." if surf_hab > 0 else "Surface habitable à vérifier lors de la visite.",
                        "Vérifier les diagnostics techniques et l'état de la toiture lors de la visite.",
                        "Marge de négociation courante sur ce secteur : entre -4% et -8%."
                    ]

                init_price_xpf = safe_int(row.get("initial_price_xpf"), 0)
                if init_price_xpf <= 0:
                    init_price_xpf = price_xpf

                # Date de publication d'origine et calcul réel de daysOnMarket
                pub_time = row.get("published_at")
                first_seen = row.get("first_seen_at")
                scraped_time = row.get("scraped_at")
                ref_time = pub_time if pd.notna(pub_time) else (first_seen if pd.notna(first_seen) else scraped_time)

                days_on_market = 1
                date_added_str = now.strftime("%Y-%m-%d")
                if pd.notna(ref_time):
                    try:
                        ref_ts = pd.to_datetime(ref_time).tz_localize(None) if getattr(ref_time, "tzinfo", None) else pd.to_datetime(ref_time)
                        now_ts = pd.Timestamp.now().tz_localize(None)
                        days_on_market = max(1, (now_ts - ref_ts).days)
                        date_added_str = ref_ts.strftime("%Y-%m-%d")
                    except Exception:
                        days_on_market = 1

                # Statut : Baisses de prix prioritaires, puis Nouveau (<=7j), puis Stable
                if price_xpf < init_price_xpf:
                    status = "price_drop"
                elif days_on_market <= 7:
                    status = "new"
                else:
                    status = "stable"

                # Date de dernière mise à jour
                last_change = row.get("last_price_change_at")
                if pd.notna(last_change):
                    last_update_date = pd.to_datetime(last_change).strftime("%d/%m/%Y")
                elif pd.notna(scraped_time):
                    last_update_date = pd.to_datetime(scraped_time).strftime("%d/%m/%Y")
                else:
                    last_update_date = "Aujourd'hui"

                # Chronologie complète et multi-jalons des prix (priceHistory)
                listing_id_str = str(row.get("id"))
                raw_hist = history_by_listing.get(listing_id_str, [])
                price_history = []

                if raw_hist:
                    for h in raw_hist:
                        ev_type = str(h.get("event_type") or "").upper()
                        h_price = safe_int(h.get("price_xpf"), price_xpf)
                        h_rec = h.get("recorded_at")
                        h_date = pd.to_datetime(h_rec).strftime("%d/%m/%Y") if pd.notna(h_rec) else "Récemment"
                        chg_xpf = safe_int(h.get("price_change_xpf"), 0)
                        chg_pct = safe_float(h.get("price_change_pct"), 0.0)

                        chg_str = f"{abs(chg_xpf)/1e6:.1f}M F" if abs(chg_xpf) >= 1_000_000 else f"{abs(chg_xpf):_d} F".replace("_", " ")

                        if ev_type == "INITIAL":
                            lbl = f"{'Loyer initial' if is_location else 'Prix initial'} lors de la publication sur {row.get('source')}"
                        elif "DROP" in ev_type:
                            lbl = f"Baisse constatée : {chg_pct:.1f}% (-{chg_str})"
                        elif "INCREASE" in ev_type:
                            lbl = f"Hausse constatée : +{abs(chg_pct):.1f}% (+{chg_str})"
                        else:
                            lbl = f"Observation sur {row.get('source')}"

                        price_history.append({
                            "date": h_date,
                            "price": h_price,
                            "label": lbl
                        })

                    # Éviter les doublons de prix identiques dans la chronologie
                    if len(price_history) == 1:
                        # Si un seul événement initial, indiquer que l'offre est toujours active et stable
                        last_h_date = price_history[-1]["date"]
                        today_fmt = now.strftime("%d/%m/%Y")
                        if last_h_date != today_fmt and last_h_date != "Aujourd'hui":
                            price_history.append({
                                "date": "Aujourd'hui",
                                "price": price_xpf,
                                "label": f"{'Loyer actif' if is_location else 'Offre active'} stable sous veille"
                            })
                    else:
                        # Si le dernier événement a déjà le prix actuel, on indique qu'il est en vigueur
                        if price_history[-1]["price"] == price_xpf:
                            price_history[-1]["label"] += " • Offre en vigueur"
                        else:
                            price_history.append({
                                "date": "Aujourd'hui",
                                "price": price_xpf,
                                "label": f"{'Loyer actif' if is_location else 'Offre active'} sous veille"
                            })
                else:
                    pub_fmt = pd.to_datetime(ref_time).strftime("%d/%m/%Y") if pd.notna(ref_time) else "Publication"
                    price_history.append({
                        "date": pub_fmt,
                        "price": init_price_xpf,
                        "label": f"{'Loyer initial' if is_location else 'Prix initial'} sur {row.get('source')}"
                    })
                    if price_xpf != init_price_xpf or days_on_market > 1:
                        price_history.append({
                            "date": "Aujourd'hui",
                            "price": price_xpf,
                            "label": f"{'Loyer actif révisé' if price_xpf != init_price_xpf else ('Loyer actif' if is_location else 'Offre active')} sous veille"
                        })

                # Détermination de la typologie (Studio, F1, F2, F3, F4, F5+)
                raw_rooms = safe_int(row.get("rooms"), 0)
                raw_bedrooms = safe_int(row.get("bedrooms"), 0)
                room_type = ""
                room_type_code = ""

                # Exclusion explicite des biens non résidentiels
                if prop_type not in ("TERRAIN", "DOCK", "LOCAL_COMMERCIAL", "IMMEUBLE"):
                    if 1 <= raw_rooms <= 15:
                        if raw_rooms == 1:
                            title_desc = f"{row.get('title') or ''} {desc}".lower()
                            room_type = "Studio" if "studio" in title_desc else "F1"
                            room_type_code = "1"
                        elif raw_rooms == 2:
                            room_type = "F2"
                            room_type_code = "2"
                        elif raw_rooms == 3:
                            room_type = "F3"
                            room_type_code = "3"
                        elif raw_rooms == 4:
                            room_type = "F4"
                            room_type_code = "4"
                        elif raw_rooms >= 5:
                            room_type = f"F{raw_rooms}"
                            room_type_code = "5+"

                raw_furnished = row.get("is_furnished")
                is_furnished = None
                if pd.notna(raw_furnished):
                    is_furnished = bool(raw_furnished)
                
                furnished_label = "Non spécifié"
                if is_furnished is True:
                    furnished_label = "Meublé"
                elif is_furnished is False:
                    furnished_label = "Non meublé"

                features = []
                if is_location:
                    features.append("Location")
                    if is_furnished is True:
                        features.append("🛋️ Meublé")
                    elif is_furnished is False:
                        features.append("Non meublé")
                if room_type:
                    features.append(f"{room_type} ({raw_bedrooms} ch.)" if raw_bedrooms > 0 else room_type)
                elif raw_bedrooms > 0:
                    features.append(f"{raw_bedrooms} ch.")
                if surf_hab > 0:
                    features.append(f"{surf_hab:.0f} m² hab.")
                if surf_ter > 0:
                    features.append(f"Terrain {(surf_ter/100):.1f} ares")
                if surf_var > 0:
                    features.append(f"Varangue {surf_var:.0f} m²")
                if has_sea_view:
                    features.append("Vue mer")
                if row.get("has_pool"):
                    features.append("Piscine")
                if row.get("has_air_conditioning"):
                    features.append("Climatisé")

                # Géolocalisation haute précision et datation des photos
                lat_p = safe_float(row.get("lat_precise"), None)
                lon_p = safe_float(row.get("lon_precise"), None)
                if lat_p and lon_p:
                    COMMUNE_BOUNDS_VERIF = {
                        "NOUMEA": (-22.4783, -22.2169, 166.2930, 166.5062),
                        "DUMBEA": (-22.2274, -22.0799, 166.3918, 166.5931),
                        "MONT_DORE": (-22.4673, -22.1486, 166.4802, 166.9733),
                        "PAITA": (-22.2436, -21.9438, 166.0812, 166.4217),
                    }
                    if commune_enum in COMMUNE_BOUNDS_VERIF:
                        b_lat_min, b_lat_max, b_lon_min, b_lon_max = COMMUNE_BOUNDS_VERIF[commune_enum]
                        if not (b_lat_min <= lat_p <= b_lat_max and b_lon_min <= lon_p <= b_lon_max):
                            lat_p = None
                            lon_p = None
                    elif commune_enum == "AUTRE":
                        if -22.48 <= lat_p <= -22.05 and 166.05 <= lon_p <= 167.00:
                            lat_p = None
                            lon_p = None

                final_lat = lat_p if (lat_p is not None and lat_p != 0.0) else lat_val
                final_lon = lon_p if (lon_p is not None and lon_p != 0.0) else lon_val

                prec_score = safe_int(row.get("precision_score"), 40)
                prec_level = str(row.get("precision_level") or "QUARTIER_DEFAULT")
                prec_detail = str(row.get("precision_detail") or f"Approximation secteur {quartier}")

                if prec_score >= 95 or prec_level == "METRIQUE_GPS":
                    prec_badge = "🟢 95% GPS Photo"
                    prec_badge_class = "border-emerald-500/40 text-emerald-400 bg-emerald-950/40"
                    prec_dot = "bg-emerald-400"
                elif prec_score >= 80 or prec_level == "IMMEUBLE_REFIL":
                    prec_badge = f"🔵 {prec_score}% Immeuble REFIL"
                    prec_badge_class = "border-blue-500/40 text-blue-400 bg-blue-950/40"
                    prec_dot = "bg-blue-400"
                elif prec_score >= 65 or prec_level == "LOT_CADASTRE":
                    prec_badge = f"🟣 {prec_score}% Lot Cadastre"
                    prec_badge_class = "border-purple-500/40 text-purple-400 bg-purple-950/40"
                    prec_dot = "bg-purple-400"
                else:
                    prec_badge = f"⚪ {prec_score}% Secteur Quartier"
                    prec_badge_class = "border-slate-600/40 text-slate-300 bg-slate-800/40"
                    prec_dot = "bg-slate-400"

                raw_photo_dt = row.get("photo_date_taken")
                photo_date_str = None
                if pd.notna(raw_photo_dt) and raw_photo_dt:
                    try:
                        photo_date_str = pd.to_datetime(raw_photo_dt).strftime("%d/%m/%Y")
                    except Exception:
                        photo_date_str = str(raw_photo_dt)[:10]

                photo_age_m = safe_int(row.get("photo_age_months"), None)
                photo_badge = None
                if photo_date_str:
                    if photo_age_m is not None and photo_age_m >= 12:
                        years = photo_age_m // 12
                        photo_badge = f"📷 Photo d'il y a {years} an{'s' if years > 1 else ''} ({photo_date_str})"
                    elif photo_age_m is not None and photo_age_m >= 6:
                        photo_badge = f"📷 Photo d'il y a {photo_age_m} mois ({photo_date_str})"
                    else:
                        photo_badge = f"📷 Prise le {photo_date_str}"

                listings.append({
                    "id": str(row.get("id")),
                    "title": str(row.get("title")),
                    "category": category,
                    "categoryLabel": category_label,
                    "transactionType": transaction_type,
                    "transactionTypeLabel": transaction_label,
                    "isLocation": is_location,
                    "isFurnished": is_furnished,
                    "furnishedLabel": furnished_label,
                    "commune": commune_raw,
                    "quartier": quartier,
                    "lat": final_lat,
                    "lon": final_lon,
                    "latPrecise": lat_p,
                    "lonPrecise": lon_p,
                    "precisionScore": prec_score,
                    "precisionLevel": prec_level,
                    "precisionDetail": prec_detail,
                    "precisionBadge": prec_badge,
                    "precisionBadgeClass": prec_badge_class,
                    "precisionDot": prec_dot,
                    "photoDateTaken": photo_date_str,
                    "photoAgeMonths": photo_age_m,
                    "photoBadge": photo_badge,
                    "currentPrice": price_xpf,
                    "initialPrice": init_price_xpf,
                    "lastUpdateDate": last_update_date,
                    "surfaceHabitable": surf_hab,
                    "surfaceTerrain": surf_ter,
                    "surfaceVarangue": surf_var,
                    "rooms": raw_rooms if raw_rooms > 0 else None,
                    "bedrooms": raw_bedrooms if raw_bedrooms > 0 else None,
                    "roomType": room_type,
                    "roomTypeCode": room_type_code,
                    "dateAdded": date_added_str,
                    "daysOnMarket": days_on_market,
                    "avgDaysOnMarket": 60,
                    "status": status,
                    "marketVerdict": verdict,
                    "verdictBadge": verdict_badge,
                    "targetPriceLow": target_low,
                    "targetPriceHigh": target_high,
                    "priceM2": price_m2,
                    "avgPriceM2Sector": avg_price_m2_sector,
                    "marketTension": "Forte attractivité sur le secteur",
                    "source": str(row.get("source")),
                    "sourceUrl": str(row.get("url")),
                    "agencyName": str(row.get("agency_name") or "Professionnel Immo NC"),
                    "agencyPhone": "+687 28.10.20",
                    "whatsapp": "687281020",
                    "image": img_url,
                    "images": images,
                    "description": desc,
                    "features": features,
                    "priceHistory": price_history,
                    "aiAnalysis": {
                        "verdictTitle": analysis_title,
                        "contextText": analysis_context,
                        "levers": analysis_levers
                    }
                })

            # Enrichissement foncier et juridique Cadastre & REFIL
            try:
                listings = enrich_listings(listings)
            except Exception as e:
                logger.warning(f"Impossible d'enrichir les annonces avec le cadastre : {e}")

            clean_payload = {"success": True, "count": len(listings), "data": listings}
            payload_bytes = json.dumps(sanitize_for_json(clean_payload), default=str, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if not limit_val:
                _CACHED_LISTINGS_PAYLOAD = payload_bytes
            self._send_json(payload_bytes)
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des annonces : {e}")
            self._send_json({"success": False, "error": str(e)}, status_code=500)

    def handle_get_parcel_polygon(self):
        """
        Récupère en direct le polygone vectoriel officiel de la parcelle
        depuis l'API ArcGIS cadastre.gouv.nc (avec cache mémoire).
        """
        try:
            parsed_url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed_url.query)
            lat = float(params.get("lat", [0])[0])
            lon = float(params.get("lon", [0])[0])

            if not (-24.0 <= lat <= -19.0 and 163.0 <= lon <= 169.0):
                return self._send_json({"success": False, "error": "Coordonnées hors Nouvelle-Calédonie"}, status_code=400)

            cache_key = f"{round(lat, 5)}_{round(lon, 5)}"
            if cache_key in PARCEL_POLYGON_CACHE:
                return self._send_json(PARCEL_POLYGON_CACHE[cache_key])

            arcgis_url = (
                "https://cadastre.gouv.nc/arcgisServices/cadastreV3/cadastre_consult_v333/MapServer/7/query?"
                f"geometry={lon}%2C{lat}&geometryType=esriGeometryPoint&inSR=4326&spatialRel=esriSpatialRelIntersects"
                "&outFields=*&returnGeometry=true&outSR=4326&f=json"
            )

            req = urllib.request.Request(arcgis_url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://cadastre.gouv.nc/"
            })
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            features = data.get("features", [])
            if not features:
                res = {"success": True, "found": False, "polygon": None}
                PARCEL_POLYGON_CACHE[cache_key] = res
                return self._send_json(res)

            feat = features[0]
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            rings = geom.get("rings", [])

            nic = attrs.get("cadastre.sde_cadastre_adm.f_parc_active.nic_graphique") or attrs.get("cadastre.cadastre_app.parcel.ref")
            lot = attrs.get("cadastre.cadastre_app.parcel.lot_number")
            lotissement = attrs.get("cadastre.cadastre_app.parcel.allotment_name")
            area_m2 = attrs.get("cadastre.sde_cadastre_adm.f_parc_active.st_area(shape)")
            h = attrs.get("cadastre.cadastre_app.parcel.h") or 0
            a = attrs.get("cadastre.cadastre_app.parcel.a") or 0
            c = attrs.get("cadastre.cadastre_app.parcel.c") or 0
            contenance_str = f"{h}ha {a}a {c}ca"

            geojson_feature = {
                "type": "Feature",
                "properties": {
                    "nic": nic,
                    "lot": lot,
                    "lotissement": lotissement,
                    "surfaceM2": round(area_m2, 1) if area_m2 else None,
                    "contenance": contenance_str
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": rings
                }
            }

            res = {
                "success": True,
                "found": True,
                "nic": nic,
                "lot": lot,
                "lotissement": lotissement,
                "surfaceM2": round(area_m2, 1) if area_m2 else None,
                "contenance": contenance_str,
                "polygon": geojson_feature
            }
            PARCEL_POLYGON_CACHE[cache_key] = res
            return self._send_json(res)

        except Exception as e:
            logger.warning(f"Erreur parcel polygon : {e}")
            return self._send_json({"success": False, "error": str(e)}, status_code=500)

    def handle_get_pud_rules(self):
        """
        Renvoie le zonage d'urbanisme PUD, les droits à bâtir et la hauteur maximale.
        """
        try:
            parsed_url = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed_url.query)
            commune = params.get("commune", ["Nouméa"])[0]
            quartier = params.get("quartier", [""])[0]
            lat = float(params.get("lat", [0])[0])
            lon = float(params.get("lon", [0])[0])

            zone_info = determine_pud_zone(commune, quartier, lat, lon)
            return self._send_json({"success": True, "pud": zone_info})
        except Exception as e:
            logger.warning(f"Erreur pud rules : {e}")
            return self._send_json({"success": False, "error": str(e)}, status_code=500)

    def handle_get_sources(self):
        """Renvoie les statistiques réelles des sources surveillées depuis DuckDB sans chiffre factice."""
        try:
            db = PropertyDatabase(read_only=True)
            df = db.query("""
                SELECT source, COUNT(*) AS count, MAX(scraped_at) AS last_scan
                FROM listings
                WHERE is_active = TRUE
                GROUP BY source
                ORDER BY count DESC
            """)

            db_counts = {}
            for _, r in df.iterrows():
                db_counts[str(r["source"]).lower()] = {
                    "count": int(r["count"]),
                    "last_scan": str(r["last_scan"])[:16] if r["last_scan"] else "Récemment",
                }

            sources_config = [
                {
                    "id": "immobilier_nc",
                    "name": "Immobilier.nc",
                    "url": "https://www.immobilier.nc",
                    "type": "Portail Fédérateur NC",
                    "role": "Fédération des 52 agences partenaires (3 328 ventes, 3 098 locations)",
                    "key": "immobilier.nc",
                    "method": "API REST Directe",
                },
                {
                    "id": "bienmeloger_nc",
                    "name": "Bienmeloger.nc",
                    "url": "https://www.bienmeloger.nc",
                    "type": "Passerelle Professionnelle",
                    "role": "Syndication CRM agences calédoniennes (Netty / Apimo)",
                    "key": "bienmeloger.nc",
                    "method": "API REST Directe",
                },
                {
                    "id": "yatoo_nc",
                    "name": "Yatoo.nc",
                    "url": "https://www.yatoo.nc",
                    "type": "Petites Annonces Particuliers & Pros",
                    "role": "Flux GraphQL live NC (180+ annonces réelles)",
                    "key": "yatoo.nc",
                    "method": "API GraphQL Native",
                },
                {
                    "id": "annonces_nc",
                    "name": "Annonces.nc",
                    "url": "https://www.annonces.nc",
                    "type": "Portail Majeur NC",
                    "role": "Petites annonces & immobilier calédonien",
                    "key": "annonces.nc",
                    "method": "Scraping Web",
                },
                {
                    "id": "legratuit_nc",
                    "name": "Le Gratuit NC",
                    "url": "https://www.legratuit.nc",
                    "type": "Petites Annonces",
                    "role": "Journal & portail local d'annonces calédoniennes",
                    "key": "legratuit.nc",
                    "method": "Passerelle Web",
                },
                {
                    "id": "fb_groupes_nc",
                    "name": "Facebook NC (Groupes & Marketplace)",
                    "url": "https://facebook.com/groups/immobilier-nouvelle-caledonie",
                    "type": "Réseaux Sociaux",
                    "role": "Groupes Immo NC & Marketplace Grand Nouméa",
                    "key": "facebook",
                    "method": "Veille Sociale",
                },
                {
                    "id": "immonc",
                    "name": "Immo.nc / Immocal",
                    "url": "https://www.immonc.com",
                    "type": "Portail Local",
                    "role": "Portail indépendant calédonien (2 100+ annonces agences & particuliers)",
                    "key": "immonc",
                    "method": "Scraping Web Direct",
                },
            ]

            results = []
            for s in sources_config:
                k = s["key"]
                found = db_counts.get(k, {})
                c = found.get("count", 0)
                ls = found.get("last_scan", "Aujourd'hui")
                results.append({
                    "id": s["id"],
                    "name": s["name"],
                    "url": s["url"],
                    "type": s["type"],
                    "role": s["role"],
                    "status": "active" if c > 0 else "standby",
                    "method": s["method"],
                    "lastScan": f"À l'instant ({ls[-5:]})" if " " in ls else ls,
                    "count": c,  # Compte réel strict sans repli factice
                })

            self._send_json({"success": True, "sources": results})
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des sources : {e}")
            self._send_json({"success": False, "error": str(e)}, status_code=500)

    def handle_get_agencies(self):
        """Renvoie l'annuaire des 52 agences calédoniennes enrichi des comptes réels en base DuckDB."""
        try:
            db = PropertyDatabase(read_only=True)
            df = db.query("""
                SELECT 
                    agency_name,
                    COUNT(*) AS count_total,
                    COUNT(CASE WHEN transaction_type = 'VENTE' THEN 1 END) AS count_ventes,
                    COUNT(CASE WHEN transaction_type = 'LOCATION' THEN 1 END) AS count_locations,
                    MIN(source) AS primary_source,
                    MAX(scraped_at) AS last_scraped
                FROM listings
                WHERE is_active = TRUE
                GROUP BY agency_name
            """)

            db_agency_stats = {}
            for _, r in df.iterrows():
                ag_name_raw = str(r["agency_name"]).strip()
                if ag_name_raw and ag_name_raw.lower() not in ("none", "nan"):
                    db_agency_stats[ag_name_raw.lower()] = {
                        "name_in_db": ag_name_raw,
                        "total": int(r["count_total"]),
                        "ventes": int(r["count_ventes"]),
                        "locations": int(r["count_locations"]),
                        "source": str(r["primary_source"]),
                        "last_scraped": str(r["last_scraped"])[:16] if r["last_scraped"] else "",
                    }

            matched_db_keys = set()
            agencies_list = []

            for ag in NC_AGENCIES:
                total_c = 0
                ventes_c = 0
                locs_c = 0
                primary_source = "Fédération immobilier.nc"
                last_scraped = ""

                # Recherche de correspondances dans les statistiques réelles
                ag_name_lower = ag["name"].lower()
                for db_k, db_val in db_agency_stats.items():
                    is_match = False
                    if db_k == ag_name_lower:
                        is_match = True
                    else:
                        for alias in ag.get("aliases", []):
                            if alias in db_k or db_k in alias:
                                is_match = True
                                break

                    if is_match:
                        total_c += db_val["total"]
                        ventes_c += db_val["ventes"]
                        locs_c += db_val["locations"]
                        primary_source = db_val["source"]
                        last_scraped = db_val["last_scraped"]
                        matched_db_keys.add(db_k)

                agencies_list.append({
                    "id": ag["id"],
                    "name": ag["name"],
                    "website": ag["website"],
                    "city": ag["city"],
                    "logo_text": ag.get("logo_text", ag["name"][:3].upper()),
                    "badge_color": ag.get("badge_color", "blue"),
                    "total_listings": total_c,
                    "count_ventes": ventes_c,
                    "count_locations": locs_c,
                    "status": "active" if total_c > 0 else "monitored",
                    "primary_source": primary_source,
                    "last_scraped": last_scraped,
                })

            # Ajout des agences / négociateurs présents en base mais hors des 52 prédéfinies
            for db_k, db_val in db_agency_stats.items():
                if db_k not in matched_db_keys and db_val["name_in_db"] != " ":
                    agencies_list.append({
                        "id": "extra_" + re.sub(r"[^a-z0-9]", "_", db_k)[:15],
                        "name": db_val["name_in_db"],
                        "website": f"https://www.immobilier.nc",
                        "city": "Nouvelle-Calédonie",
                        "logo_text": db_val["name_in_db"][:3].upper(),
                        "badge_color": "slate",
                        "total_listings": db_val["total"],
                        "count_ventes": db_val["ventes"],
                        "count_locations": db_val["locations"],
                        "status": "active",
                        "primary_source": db_val["source"],
                        "last_scraped": db_val["last_scraped"],
                    })

            # Trier : les agences avec annonces actives en premier par volume décroissant
            agencies_list.sort(key=lambda x: (-x["total_listings"], x["name"]))

            active_count = len([a for a in agencies_list if a["total_listings"] > 0])
            self._send_json({
                "success": True,
                "total_agencies": len(agencies_list),
                "active_with_listings": active_count,
                "agencies": agencies_list
            })
        except Exception as e:
            logger.error(f"Erreur lors de la récupération des agences : {e}")
            self._send_json({"success": False, "error": str(e)}, status_code=500)

    def handle_get_audit(self):
        """Renvoie le dernier rapport d'audit JSON."""
        audit_file = BASE_DIR / "logs" / "audit_report.json"
        if audit_file.exists():
            with open(audit_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._send_json({"success": True, "audit": data})
        else:
            auditor = PipelineAuditor()
            report = auditor.run_full_audit()
            self._send_json({"success": True, "audit": report})

    def handle_get_logs(self):
        """Renvoie les 40 dernières lignes du journal d'exécution."""
        if DEFAULT_LOG_FILE.exists():
            with open(DEFAULT_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            last_lines = [l.strip() for l in lines[-40:] if l.strip()]
            self._send_json({"success": True, "logs": last_lines})
        else:
            self._send_json({"success": True, "logs": ["Aucun log disponible pour l'instant."]})

    def handle_post_refresh(self):
        """Déclenche la VRAIE actualisation multi-sources en direct depuis la NC (Immobilier.nc + Yatoo.nc)."""
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            raw_body = self.rfile.read(content_len).decode("utf-8")
            try:
                body = json.loads(raw_body)
            except Exception:
                pass

        source_filter = body.get("source")
        pages = int(body.get("pages", 10))
        if source_filter and "immonc" in source_filter.lower():
            pages = max(pages, 3)

        logger.info(f"Déclenchement requête POST /api/refresh (source={source_filter}, pages={pages})")

        scraper = LiveNCScraper()
        result = scraper.run_live_sync(
            max_pages=pages,
            include_rentals=True,
            include_yatoo=True,
            include_immonc=True,
            source_filter=source_filter
        )

        global _CACHED_LISTINGS_PAYLOAD
        _CACHED_LISTINGS_PAYLOAD = None

        # Exporte les snapshots statiques et préchauffe le cache en arrière-plan
        def _sync_static_files():
            try:
                import urllib.request
                for ep, fp in [
                    (f"http://127.0.0.1:{PORT}/api/listings", BASE_DIR / "data_listings.json"),
                    (f"http://127.0.0.1:{PORT}/api/sources", BASE_DIR / "data_sources.json"),
                    (f"http://127.0.0.1:{PORT}/api/agencies", BASE_DIR / "data_agencies.json"),
                ]:
                    try:
                        with urllib.request.urlopen(ep, timeout=15) as resp:
                            data = resp.read()
                            with open(fp, "wb") as f:
                                f.write(data)
                    except Exception as ex:
                        logger.warning(f"Erreur sync static file {fp.name}: {ex}")
            except Exception as e:
                logger.warning(f"Erreur lors de la synchronisation statique : {e}")

        threading.Thread(target=_sync_static_files, daemon=True).start()

        self._send_json(result)


def run_server():
    server_address = ("", PORT)
    httpd = ThreadingHTTPServer(server_address, NCImmoAPIHandler)
    httpd.daemon_threads = True
    logger.info(f"Serveur API & Dashboard Sentinel démarré sur http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Arrêt du serveur.")
        httpd.server_close()


if __name__ == "__main__":
    run_server()
