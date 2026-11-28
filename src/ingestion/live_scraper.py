import os
import sys
import json
import re
import html as html_lib
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor
import requests
import urllib3

urllib3.disable_warnings()

from src.models.listing import RawListing, CleanedListing
from src.ingestion.collector import IngestionPipeline
from src.storage.database import PropertyDatabase
from src.utils.logger import get_logger
from src.utils.log_analyzer import PipelineAuditor
from src.domain.agencies_directory import resolve_agency_name
from src.ingestion.detail_parser import parse_immonc_detail_html

logger = get_logger("live_scraper")

_CP1252_TRANS = str.maketrans({
    "\x80": "€", "\x85": "...", "\x91": "'", "\x92": "'",
    "\x93": '"', "\x94": '"', "\x95": "•", "\x96": "–", "\x97": "—",
})


def _clean_html_and_cp1252(text: Optional[str]) -> str:
    """Nettoie les balises HTML (<br />, <strong>, <u>) et octets Windows-1252 (\x80-\x9f) dès l'ingestion."""
    if not text:
        return ""
    s = str(text).translate(_CP1252_TRANS)
    s = re.sub(r"[\x80-\x9f]", " ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*(?:p|div|li|h[1-6]|tr)\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</?[a-zA-Z][^>]*>", "", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _normalize_phone_nc_simple(raw_phone: Any) -> Optional[str]:
    """Formate un numéro de téléphone NC (+687 XX.XX.XX) si 6 chiffres valides sont détectés."""
    if not raw_phone:
        return None
    digits = re.sub(r"\D", "", str(raw_phone))
    if digits.startswith("00687"):
        digits = digits[5:]
    elif digits.startswith("687") and len(digits) == 9:
        digits = digits[3:]
    elif digits.startswith("0") and len(digits) == 7:
        digits = digits[1:]
    if len(digits) == 6 and digits[0] in "23456789":
        return f"+687 {digits[:2]}.{digits[2:4]}.{digits[4:]}"
    return None


def _canonicalize_immonc_photo(url: str) -> str:
    """Normalise une URL photo ImmoNC vers https://www.immonc.com/photos/photos_big/<filename>."""
    if not url:
        return ""
    u = str(url).strip()
    u = re.sub(r"/photos_(?:medium|small|vignette|thumb)/", "/photos_big/", u)
    u = re.sub(r"^https?://(?:www\.)?immonc\.com/", "https://www.immonc.com/", u, flags=re.IGNORECASE)
    return u


def _dedup_immonc_photos(urls: List[str]) -> List[str]:
    """Déduplique les URLs photos ImmoNC par nom de fichier (élimine les doublons www/no-www/vignette)."""
    out: List[str] = []
    seen_keys = set()
    for raw_u in urls:
        if not raw_u or not str(raw_u).strip().startswith("http"):
            continue
        canon = _canonicalize_immonc_photo(str(raw_u))
        fname = canon.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()
        key = fname if ("immonc.com" in canon.lower() and fname) else canon.lower()
        if key not in seen_keys:
            seen_keys.add(key)
            out.append(canon)
    return out


def parse_datetime_safe(val: Any) -> Optional[datetime]:
    """Convertit une chaîne de date (ISO ou format SQL) en objet datetime UTC cohérent."""
    if not val:
        return None
    val_str = str(val).strip()
    try:
        dt = datetime.fromisoformat(val_str.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(val_str, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


class LiveNCScraper:
    """
    Scraper en direct multi-sources connecté :
    1. À l'API publique de Nouvelle-Calédonie (immobilier.nc / bienmeloger.nc)
       couvrant la fédération des 52 agences professionnelles de NC (3 300+ ventes et 3 000+ locations).
    2. À l'API GraphQL Cloud Function de Yatoo.nc (petites annonces ventes & locations).
    3. Au portail calédonien Immo.nc (immonc.com - 2 100+ annonces d'agences et de particuliers).
    """

    API_POSTS_URL = "https://api.immobilier.nc/api/posts"
    YATOO_GRAPHQL_URL = "https://australia-southeast1-yatoo-nc.cloudfunctions.net/api"
    IMMONC_BASE_URL = "https://www.immonc.com"

    HEADERS_IMMO_NC = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/json, application/vnd_api.immobilier.nc.v0.1+json;",
        "Referer": "https://www.immobilier.nc/",
    }

    HEADERS_YATOO = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Origin": "https://yatoo.nc",
        "Referer": "https://yatoo.nc/",
    }

    HEADERS_IMMONC = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.immonc.com/",
    }

    def __init__(self, db: Optional[PropertyDatabase] = None):
        self.pipeline = IngestionPipeline(db=db)
        self.db = self.pipeline.db
        self.auditor = PipelineAuditor(db=self.db)

    def fetch_live_raw_items(self, deal_type: str = "vente", page: int = 1) -> List[Dict[str, Any]]:
        """Interroge l'API live immobilier.nc et renvoie la liste brute des données JSON."""
        params = {
            "by_deal_type": deal_type,
            "page": page,
        }
        logger.info(f"Connexion au flux en direct NC : {self.API_POSTS_URL} (page={page}, by_deal_type={deal_type})")
        try:
            resp = requests.get(self.API_POSTS_URL, params=params, headers=self.HEADERS_IMMO_NC, timeout=14, verify=False)
            resp.raise_for_status()
            payload = resp.json()
            items = payload.get("data", [])
            total = payload.get("meta", {}).get("total", len(items))
            logger.info(f"Flux NC page {page} ({deal_type}) : {len(items)} annonces reçues (Total disponible en NC : {total}).")
            return items
        except Exception as e:
            logger.error(f"Erreur de connexion à l'API en direct NC (page {page}, {deal_type}) : {e}")
            return []

    def parse_item_to_raw_listing(self, item: Dict[str, Any], default_source: str = "immobilier.nc") -> Optional[RawListing]:
        """Convertit un objet JSON issu de l'API NC en RawListing standardisé."""
        try:
            raw_id = item.get("id")
            if raw_id is None:
                return None
            item_id = str(raw_id).strip()
            if not item_id or item_id.lower() == "none":
                return None

            # 1. Localisation
            locality = item.get("locality") or {}
            commune_name = (locality.get("name") or "").strip()
            quartier_dict = locality.get("children") or {}
            quartier_name = ""
            if isinstance(quartier_dict, dict):
                quartier_name = (quartier_dict.get("name") or "").strip()

            location_str = f"{commune_name} - {quartier_name}".strip(" -")

            # 2. Détection du type de transaction (Vente vs Location)
            # Priorité stricte à deal_type_api quand il est fourni par l'API (évite que "loyer/bail" dans un bien de rendement en vente bascule en Location)
            deal_type_api = str(item.get("deal_type") or "").strip().lower()
            desc = _clean_html_and_cp1252(item.get("description") or "")
            blob = f"{desc} {item.get('title') or ''}".lower()
            blob_no_invest = re.sub(
                r"\b(id[ée]al\s*(?:pour\s*)?investiss\w*|rentabilit[ée]\s*locative|rapport\s*locatif|actuellement\s*lou[ée]e?s?|vendu[es]?\s*lou[ée]e?s?|lou[ée]e?\s+\d|loyer\s*(?:actuel|mensuel\s*per[çc]u|annuel)|bail\s*en\s*cours|revenus?\s*locatifs?)\b",
                " ",
                blob,
            )
            has_loc_words = bool(re.search(r"\b(location|louer|loyer|f/mois|charges comprises|bail)\b", blob_no_invest))
            has_vente_words = bool(re.search(r"\b(vente|vendre|achat|acheter|prix fai|millions?|\bmf\b)\b", blob))

            if deal_type_api == "location":
                is_location = True
                transaction_type_declared = "Location"
            elif deal_type_api == "vente":
                is_location = False
                transaction_type_declared = "Vente"
            elif has_loc_words and not has_vente_words:
                is_location = True
                transaction_type_declared = "Location"
            else:
                is_location = False
                transaction_type_declared = "Vente"

            # 3. Gestion et normalisation du Prix en F CFP
            raw_p = item.get("price")
            price_str = None
            if raw_p is not None:
                try:
                    p_val = float(raw_p)
                    if is_location:
                        # Pour une location : loyer mensuel
                        if p_val < 1000:
                            price_int = int(p_val * 1_000)
                        else:
                            price_int = int(p_val)
                        price_str = f"{price_int} F CFP/mois"
                    else:
                        # Pour une vente (l'API renvoie parfois des milliers, ex: 39500 -> 39 500 000 F ou 39.9 -> 39 900 000 F)
                        if p_val < 1000:
                            price_int = int(round(p_val * 1_000_000))
                        elif p_val >= 1_000_000:
                            price_int = int(round(p_val))
                        elif deal_type_api == "vente" or has_vente_words:
                            price_int = int(round(p_val * 1_000))
                        elif has_loc_words:
                            is_location = True
                            transaction_type_declared = "Location"
                            price_int = int(round(p_val))
                            price_str = f"{price_int} F CFP/mois"
                        else:
                            price_int = int(round(p_val * 1_000))
                        if not price_str:
                            price_str = f"{price_int} F CFP"
                except (ValueError, TypeError):
                    price_str = str(raw_p)

            # 4. Surface habitable (gère les surfaces composées ex: "81 + 48 " -> 129 m²)
            h_size = item.get("home_size")
            surface_str = None
            if h_size is not None and str(h_size).strip():
                hs_clean = str(h_size).strip()
                if "+" in hs_clean:
                    parts_h = re.findall(r"\d+(?:[.,]\d+)?", hs_clean)
                    if parts_h:
                        total_h = sum(float(x.replace(",", ".")) for x in parts_h)
                        if total_h > 0:
                            surface_str = f"{total_h:g} m²"
                else:
                    surface_str = f"{hs_clean} m²"

            # 4b. Champs natifs structurés de l'API (terrain, charges, SDB, parkings, dépôt)
            api_comp_details: Dict[str, List[str]] = {}
            full_area = item.get("full_area_size")
            full_unit = str(item.get("full_area_unit") or "m2").strip().lower()
            if full_area is not None and str(full_area).strip() not in ("", "0", "0.0"):
                fa_str = str(full_area).strip()
                if "ha" in full_unit or "hect" in full_unit:
                    api_comp_details.setdefault("Terrain", []).append(f"Sur terrain {fa_str} ha")
                elif "are" in full_unit:
                    api_comp_details.setdefault("Terrain", []).append(f"Sur terrain {fa_str} ares")
                else:
                    api_comp_details.setdefault("Terrain", []).append(f"Sur terrain {fa_str} m2")

            nb_bath = item.get("nb_of_bathroom")
            if nb_bath is not None:
                try:
                    nb_b = int(nb_bath)
                    if 1 <= nb_b <= 15:
                        api_comp_details.setdefault("Salle de bain", []).append(f"Salle d'eau {nb_b}")
                except (ValueError, TypeError):
                    pass

            nb_park = item.get("nb_of_parking")
            if nb_park is not None:
                try:
                    nb_p = int(nb_park)
                    if 1 <= nb_p <= 20:
                        api_comp_details.setdefault("Garage", []).append(f"Parking {nb_p}")
                except (ValueError, TypeError):
                    pass

            raw_charges_api: Optional[str] = None
            renting_fees = item.get("renting_fees")
            if renting_fees is not None and str(renting_fees).strip() not in ("", "0", "0.0"):
                raw_charges_api = str(renting_fees).strip()
                api_comp_details.setdefault("Informations financières", []).append(f"Charges mensuelles {raw_charges_api}")

            deposit = item.get("deposit")
            if deposit is not None and str(deposit).strip() not in ("", "0", "0.0"):
                api_comp_details.setdefault("Informations financières", []).append(f"Dépôt de garantie {str(deposit).strip()}")

            # 5. Détection du type de bien et titre
            prop_cat = str(item.get("property_category") or "").upper()
            prop_type = str(item.get("property_type") or "").capitalize()
            if not prop_type:
                prop_type = "Bien immobilier"

            title = f"{prop_type} {prop_cat} à {commune_name.capitalize()}".strip()
            if quartier_name:
                title += f" ({quartier_name.capitalize()})"

            # 6. Agence ou propriétaire avec résolution dans le référentiel des 52 agences NC + téléphone owner.phone
            owner = item.get("owner") or {}
            raw_owner_name = (owner.get("full_name") or "").strip()
            raw_mail = (owner.get("mail") or "").strip()
            owner_phone = _normalize_phone_nc_simple(owner.get("phone"))

            link_blob = ""
            for lk in (item.get("links") or []):
                if isinstance(lk, dict):
                    link_blob += " " + str(lk.get("path") or "")

            agency_name = resolve_agency_name(
                raw_name=raw_owner_name,
                raw_mail=raw_mail,
                text_blob=f"{desc} {link_blob}"
            )

            # 7. Détection de la source précise et photos authentiques
            source = default_source
            photos = item.get("photos") or []
            photo_urls = []
            if photos and isinstance(photos, list):
                for p in photos:
                    if isinstance(p, dict):
                        u = str(p.get("picture") or p.get("thumbnail") or "").strip()
                        if u and u.startswith("http") and u not in photo_urls:
                            photo_urls.append(u)
                            if "bienmeloger.nc" in u:
                                source = "bienmeloger.nc"

            photo_url = photo_urls[0] if photo_urls else ""
            images_json = json.dumps(photo_urls) if photo_urls else None

            url = f"https://www.immobilier.nc/details/{item_id}"
            facilities = item.get("facilities") if isinstance(item.get("facilities"), list) else None
            published_dt = parse_datetime_safe(item.get("published_at") or item.get("created_at"))
            created_dt = parse_datetime_safe(item.get("created_at"))

            return RawListing(
                source=source,
                source_id=item_id,
                url=url,
                title=title,
                description=desc,
                raw_price=price_str,
                raw_surface=surface_str,
                raw_rooms=prop_cat,
                raw_location=location_str,
                transaction_type_declared=transaction_type_declared,
                property_type_declared=prop_type,
                agency_name=agency_name,
                image_url=photo_url or None,
                images_json=images_json,
                facilities=facilities,
                raw_charges=raw_charges_api,
                complementary_details=api_comp_details if api_comp_details else None,
                direct_phone=owner_phone,
                published_at=published_dt,
                created_at_declared=created_dt,
                extracted_at=datetime.now(timezone.utc),
            )
        except Exception as err:
            logger.warning(f"Impossible de convertir l'annonce brute {item.get('id')} : {err}")
            return None

    def fetch_yatoo_raw_items(self, category: str = "ventes-immobilieres", limit: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """Interroge l'API GraphQL native de Yatoo.nc pour extraire les petites annonces immobilières (avec mobile & media)."""
        query = """
        query getSearchPosts($queryVariables: searchParams) {
          searchPosts(searchParams: $queryVariables) {
            data {
              id
              additionalType
              title
              media
              photos
              price
              sid
              category
              town
              description
              mobile
              createdAt
              updatedAt
              userName
            }
            total
          }
        }
        """
        payload = {
            "operationName": "getSearchPosts",
            "variables": {
                "queryVariables": {
                    "category": category,
                    "limit": limit,
                    "page": page
                }
            },
            "query": query
        }
        try:
            logger.info(f"Interrogation API GraphQL Yatoo.nc ({category}, page={page}, limit={limit})...")
            resp = requests.post(self.YATOO_GRAPHQL_URL, json=payload, headers=self.HEADERS_YATOO, timeout=12, verify=False)
            resp.raise_for_status()
            data = resp.json()
            posts = data.get("data", {}).get("searchPosts", {}).get("data", [])
            total = data.get("data", {}).get("searchPosts", {}).get("total", len(posts))
            logger.info(f"Yatoo.nc ({category}) : {len(posts)} annonces reçues (Total : {total}).")
            return posts
        except Exception as e:
            logger.error(f"Erreur lors de la requête GraphQL Yatoo.nc ({category}) : {e}")
            return []

    def parse_yatoo_item_to_raw_listing(self, item: Dict[str, Any], category: str = "ventes-immobilieres") -> Optional[RawListing]:
        """Convertit une petite annonce Yatoo.nc en RawListing standardisé."""
        try:
            raw_id = item.get("id")
            if raw_id is None:
                return None
            item_id = str(raw_id).strip()
            if not item_id or item_id.lower() == "none":
                return None

            title = _clean_html_and_cp1252(item.get("title") or "Annonce Immobilière Yatoo")
            desc = _clean_html_and_cp1252(item.get("description") or "")
            town = str(item.get("town") or "").strip()
            raw_price = item.get("price")
            is_loc = "locat" in category.lower() or "louer" in title.lower() or "loyer" in desc.lower()

            price_str = None
            if raw_price is not None:
                try:
                    p_val = float(raw_price)
                    price_int = int(p_val)
                    price_str = f"{price_int} F CFP/mois" if is_loc else f"{price_int} F CFP"
                except (ValueError, TypeError):
                    price_str = str(raw_price)

            # Photos Yatoo (photos + media fallback : gère dict, liste de dicts, URLs brutes et thumbnails)
            photos = item.get("photos") or []
            media_items = item.get("media") or []
            if isinstance(media_items, str):
                try:
                    media_items = json.loads(media_items)
                except Exception:
                    media_items = [media_items] if media_items.startswith("http") else []
            photo_urls = []
            for p in (photos if isinstance(photos, list) else []) + (media_items if isinstance(media_items, list) else []):
                if isinstance(p, dict):
                    u = str(p.get("contentUrl") or p.get("url") or p.get("src") or p.get("secure_url") or "").strip()
                    if not u:
                        thumbs = p.get("thumbnail") or []
                        if isinstance(thumbs, dict):
                            u = str(thumbs.get("contentUrl") or thumbs.get("url") or "").strip()
                        elif thumbs and isinstance(thumbs, list) and isinstance(thumbs[0], dict):
                            u = str(thumbs[0].get("contentUrl") or thumbs[0].get("url") or "").strip()
                    if u and u.startswith("http") and "unsplash.com" not in u and u not in photo_urls:
                        photo_urls.append(u)
                elif isinstance(p, str) and p.strip().startswith("http") and "unsplash.com" not in p and p.strip() not in photo_urls:
                    photo_urls.append(p.strip())

            photo_url = photo_urls[0] if photo_urls else ""
            images_json = json.dumps(photo_urls) if photo_urls else None
            mobile_phone = _normalize_phone_nc_simple(item.get("mobile"))

            # Inférence du type de bien
            lower_text = f"{title} {desc}".lower()
            if "dock" in lower_text or "entrepot" in lower_text or "entrepôt" in lower_text:
                prop_type = "Dock"
            elif any(k in lower_text for k in ("local commercial", "bureau", "bureaux", "commerce", "boutique")):
                prop_type = "Local commercial"
            elif "terrain" in lower_text:
                prop_type = "Terrain"
            elif "appartement" in lower_text or "f1" in lower_text or "f2" in lower_text or "f3" in lower_text or "studio" in lower_text:
                prop_type = "Appartement"
            else:
                prop_type = "Maison"

            sid = item.get("sid") or item_id
            url = f"https://yatoo.nc/annonces/{category}/{sid}"

            agency_name = resolve_agency_name(
                raw_name=item.get("userName"),
                raw_mail=None,
                text_blob=f"{title} {desc}"
            )

            pub_dt = parse_datetime_safe(item.get("createdAt"))

            return RawListing(
                source="yatoo.nc",
                source_id=item_id,
                url=url,
                title=title,
                description=desc,
                raw_price=price_str,
                raw_surface=None,
                raw_rooms=None,
                raw_location=town,
                transaction_type_declared="Location" if is_loc else "Vente",
                property_type_declared=prop_type,
                agency_name=agency_name,
                image_url=photo_url or None,
                images_json=images_json,
                direct_phone=mobile_phone,
                published_at=pub_dt,
                extracted_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning(f"Erreur conversion Yatoo item {item.get('id')} : {e}")
            return None

    def fetch_immonc_geodata(self, deal_type: str = "vente") -> List[Dict[str, Any]]:
        """Télécharge le jeu complet d'annonces géolocalisées depuis la carte interactive Immo.nc."""
        deal_slug = "vente" if deal_type.lower() == "vente" else "location"
        url = f"{self.IMMONC_BASE_URL}/geolocalisation-{deal_slug}"
        logger.info(f"Téléchargement du flux cartographique Immo.nc ({deal_slug}) : {url}")
        try:
            resp = requests.get(url, headers=self.HEADERS_IMMONC, timeout=25, verify=False)
            resp.raise_for_status()
            try:
                html = resp.content.decode("utf-8")
            except Exception:
                html = resp.content.decode("windows-1252", errors="ignore")
            matches = re.findall(r'data:\s*(\{.*?"id_annonce".*?\})\s*\}', html)
            items = []
            for m in matches:
                try:
                    items.append(json.loads(m))
                except Exception:
                    pass
            logger.info(f"Flux cartographique Immo.nc ({deal_slug}) : {len(items)} annonces directes extraites.")
            return items
        except Exception as e:
            logger.error(f"Erreur lors de la récupération cartographique Immo.nc ({deal_slug}) : {e}")
            return []

    def parse_immonc_geo_item_to_raw_listing(self, item: Dict[str, Any], deal_type: str = "vente") -> Optional[RawListing]:
        """Convertit un élément cartographique Immo.nc en RawListing standardisé."""
        try:
            raw_id = item.get("id_annonce")
            if raw_id is None:
                return None
            item_id = str(raw_id).strip()
            if not item_id or item_id.lower() == "none":
                return None

            lib_bien = str(item.get("lib_bien") or "").strip()
            lib_sous = str(item.get("lib_sous_types") or "").strip()
            if lib_sous.lower() == "none":
                lib_sous = ""
            title = f"{lib_bien} {lib_sous}".strip() or "Bien immobilier"
            suburb_str = str(item.get("suburb") or "").strip()
            loc_city = str(item.get("location") or "").strip()
            loc_str = f"{suburb_str} {loc_city}".strip()
            price_str = item.get("prix_annonce")
            is_loc = deal_type.lower() == "location" or "location" in deal_type.lower()

            photo_fn = item.get("property_photo")
            photo_url = _canonicalize_immonc_photo(f"https://www.immonc.com/photos/photos_big/{photo_fn}") if photo_fn else None

            url = item.get("property_url") or f"https://www.immonc.com/annonce/{deal_type}/{item_id}"

            lower_ident = f"{title} {url}".lower()
            if any(k in lower_ident for k in ["commercial", "bureau", "local", "commerce"]):
                prop_type = "Local commercial"
            elif "dock" in lower_ident or "entrepot" in lower_ident:
                prop_type = "Dock"
            elif "terrain" in lower_ident:
                prop_type = "Terrain"
            elif "immeuble" in lower_ident:
                prop_type = "Immeuble"
            elif any(k in lower_ident for k in ["villa", "maison"]):
                prop_type = "Maison"
            elif any(k in lower_ident for k in ["appartement", "f1", "f2", "f3", "f4", "studio"]):
                prop_type = "Appartement"
            else:
                prop_type = "Autre"

            agency = resolve_agency_name(
                raw_name=item.get("partenaires"),
                raw_mail=item.get("email"),
                text_blob=f"{title} {loc_str}"
            )

            partenaire_txt = str(item.get("partenaires") or agency or "Agence").strip()
            tel_txt = str(item.get("tel") or "").strip()
            email_txt = str(item.get("email") or "").strip()
            contact_bits = [b for b in (f"Tél: {tel_txt}" if tel_txt else "", f"Email: {email_txt}" if email_txt else "") if b]
            contact_suffix = f" ({', '.join(contact_bits)})" if contact_bits else ""
            desc = f"Annonce {title} à {loc_str}. Contact: {partenaire_txt}{contact_suffix}."

            return RawListing(
                source="immonc",
                source_id=item_id,
                url=url,
                title=title,
                description=desc,
                raw_price=price_str,
                raw_surface=None,
                raw_rooms=item.get("lib_sous_types"),
                raw_location=loc_str,
                transaction_type_declared="Location" if is_loc else "Vente",
                property_type_declared=prop_type,
                agency_name=agency,
                image_url=photo_url,
                images_json=json.dumps([photo_url]) if photo_url else None,
                extracted_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            return None

    def fetch_immonc_raw_items(self, deal_type: str = "vente", page: int = 1) -> List[Dict[str, Any]]:
        """Scrape une page d'annonces depuis le portail Immo.nc (immonc.com)."""
        deal_slug = "vente" if deal_type.lower() == "vente" else "location"
        url = f"{self.IMMONC_BASE_URL}/{deal_slug}?page={page}"
        logger.info(f"Connexion au flux Immo.nc : {url} (page={page}, deal_type={deal_slug})")
        try:
            resp = requests.get(url, headers=self.HEADERS_IMMONC, timeout=14, verify=False)
            resp.raise_for_status()
            try:
                raw_html = resp.content.decode("utf-8")
            except Exception:
                raw_html = resp.content.decode("windows-1252", errors="ignore")

            card_chunks = re.split(r'(?=<div[^>]*class=["\'][^"\']*search-result-card[^"\']*["\'][^>]*id=["\']gcli\d+["\'])', raw_html)
            items = []
            for chunk in card_chunks:
                id_m = re.search(r'id=["\']gcli(\d+)["\']', chunk)
                if not id_m:
                    continue
                item_id = id_m.group(1)

                link_m = re.search(r'href=["\'](https://www.immonc.com/annonce/[^"\']+)["\']', chunk)
                url_link = link_m.group(1) if link_m else f"https://www.immonc.com/annonce/{deal_slug}/{item_id}"

                title_m = re.search(r'<div[^>]*class=["\']ventitle["\'][^>]*>.*?<h2>(.*?)</h2>', chunk, re.DOTALL)
                title = html_lib.unescape(title_m.group(1).strip()) if title_m else "Bien immobilier"

                loc_m = re.search(r'<div[^>]*class=["\']vendrlocation["\'][^>]*>.*?<h3>(.*?)</h3>', chunk, re.DOTALL)
                location_raw = html_lib.unescape(loc_m.group(1).strip()) if loc_m else ""

                price_m = re.search(r'<h3[^>]*class=["\']venderprice["\'][^>]*>(.*?)</h3>', chunk, re.DOTALL)
                price_raw = price_m.group(1).strip() if price_m else ""
                clean_num = re.sub(r'[^\d]', '', price_raw)
                price_val = int(clean_num) if clean_num else 0

                desc_m = re.search(r'<div[^>]*class=["\']pera["\'][^>]*>.*?<p>(.*?)</p>', chunk, re.DOTALL)
                desc = html_lib.unescape(desc_m.group(1).strip()) if desc_m else ""

                agency_m = re.search(rf'id=["\']ad_username{item_id}["\'][^>]*value=["\'](.*?)["\']', chunk)
                agency_name = html_lib.unescape(agency_m.group(1).strip()) if agency_m else ""
                if not agency_name:
                    ag_user_m = re.search(r'title=["\']Voir les annonces de (.*?)["\']', chunk)
                    agency_name = html_lib.unescape(ag_user_m.group(1).strip()) if ag_user_m else "Particulier / ImmoNC"

                email_m = re.search(rf'id=["\']ag_email{item_id}["\'][^>]*value=["\'](.*?)["\']', chunk)
                agency_email = email_m.group(1).strip() if email_m else ""

                photos = re.findall(r'<img[^>]*src=["\'](https://immonc.com/photos/(?:photos_medium|photos_big)/[^"\']+)["\']', chunk)
                seen_photos = []
                for p in photos:
                    big_p = p.replace("/photos_medium/", "/photos_big/")
                    if big_p not in seen_photos:
                        seen_photos.append(big_p)

                items.append({
                    "id": item_id,
                    "url": url_link,
                    "title": title,
                    "location_raw": location_raw,
                    "price_xpf": price_val,
                    "description": desc,
                    "agency_name": agency_name,
                    "agency_email": agency_email,
                    "photos": seen_photos,
                    "deal_type": deal_slug,
                })

            logger.info(f"Immo.nc page {page} ({deal_slug}) : {len(items)} annonces extraites.")
            return items
        except Exception as e:
            logger.error(f"Erreur de connexion à Immo.nc (page {page}, {deal_type}) : {e}")
            return []

    def fetch_immonc_detail(self, url: str) -> Dict[str, Any]:
        """Télécharge la page de détail complète d'une annonce Immo.nc et extrait 100% des données."""
        if not url:
            return {}
        try:
            resp = requests.get(url, headers=self.HEADERS_IMMONC, timeout=15, verify=False)
            resp.raise_for_status()
            try:
                html = resp.content.decode("utf-8")
            except Exception:
                html = resp.content.decode("windows-1252", errors="ignore")
            return parse_immonc_detail_html(html, url=url)
        except Exception as e:
            logger.warning(f"Erreur de récupération fiche détaillée Immo.nc ({url}) : {e}")
            return {}

    def hydrate_raw_listing(self, rl: RawListing) -> RawListing:
        """Hydrate un RawListing existant avec les données haute fidélité de sa page détaillée."""
        if not rl or rl.source != "immonc" or not rl.url:
            return rl

        detail = self.fetch_immonc_detail(rl.url)
        if not detail:
            return rl

        # 1. Description complète non tronquée
        if detail.get("full_description"):
            rl.description = detail["full_description"]

        # 2. Surfaces et charges réelles
        if detail.get("surface_habitable_m2") is not None:
            rl.raw_surface = str(detail["surface_habitable_m2"])
        if detail.get("surface_terrasse_m2") is not None:
            rl.raw_terrace = str(detail["surface_terrasse_m2"])
        if detail.get("charges_mensuelles_xpf") is not None:
            rl.raw_charges = str(detail["charges_mensuelles_xpf"])
        if detail.get("etage") is not None:
            rl.raw_floor = str(detail["etage"])

        # 3. Détails complémentaires & Contact direct négociateur
        if detail.get("complementary_details"):
            rl.complementary_details = detail["complementary_details"]
        if detail.get("direct_negotiator"):
            rl.direct_negotiator = detail["direct_negotiator"]
        if detail.get("direct_phone"):
            rl.direct_phone = detail["direct_phone"]

        # 4. Équipements
        facs = rl.facilities or []
        if detail.get("has_sea_view") and "vue_mer" not in facs:
            facs.append("vue_mer")
        if detail.get("has_air_conditioning") and "climatisation" not in facs:
            facs.append("climatisation")
        if detail.get("has_pool") and "piscine" not in facs:
            facs.append("piscine")
        if detail.get("has_elevator") and "ascenseur" not in facs:
            facs.append("ascenseur")
        rl.facilities = facs if facs else None

        # 5. Photos HD additionnelles (normalisées & dédupliquées par nom de fichier — zéro x3)
        if detail.get("photos"):
            existing_photos = []
            if rl.images_json:
                try:
                    existing_photos = json.loads(rl.images_json)
                except Exception:
                    existing_photos = []
            merged_photos = _dedup_immonc_photos(list(detail["photos"]) + list(existing_photos))
            if merged_photos:
                rl.images_json = json.dumps(merged_photos)
                rl.image_url = merged_photos[0]

        return rl

    def parse_immonc_item_to_raw_listing(
        self,
        item: Dict[str, Any],
        deal_type: str = "vente",
        detail_data: Optional[Dict[str, Any]] = None
    ) -> Optional[RawListing]:
        """Convertit une annonce Immo.nc en RawListing standardisé avec enrichissement haute fidélité."""
        try:
            item_id = str(item.get("id"))
            if not item_id:
                return None

            # Si le dictionnaire item contient déjà detail_data
            if detail_data is None and isinstance(item.get("detail_data"), dict):
                detail_data = item.get("detail_data")

            title = str(item.get("title") or "Bien immobilier").strip()
            # Si detail_data est fourni, utiliser la description complète non tronquée
            if detail_data and detail_data.get("full_description"):
                desc = str(detail_data["full_description"]).strip()
            else:
                desc = str(item.get("description") or "").strip()

            url = str(item.get("url") or f"https://www.immonc.com/annonce/{deal_type}/{item_id}").strip()
            loc_raw = str(item.get("location_raw") or "").strip()
            price_xpf = item.get("price_xpf", 0)

            is_loc = deal_type.lower() == "location" or "location" in url.lower() or "loyer" in title.lower()
            price_str = f"{price_xpf} F CFP/mois" if is_loc else f"{price_xpf} F CFP"

            photos = _dedup_immonc_photos(
                list((detail_data.get("photos") if detail_data else None) or []) + list(item.get("photos") or [])
            )

            photo_url = photos[0] if photos else None
            images_json = json.dumps(photos) if photos else None

            lower_ident = f"{title} {url}".lower()
            if any(k in lower_ident for k in ["commercial", "bureau", "local", "commerce"]):
                prop_type = "Local commercial"
            elif "dock" in lower_ident or "entrepot" in lower_ident:
                prop_type = "Dock"
            elif "terrain" in lower_ident:
                prop_type = "Terrain"
            elif "immeuble" in lower_ident:
                prop_type = "Immeuble"
            elif any(k in lower_ident for k in ["villa", "maison"]):
                prop_type = "Maison"
            elif any(k in lower_ident for k in ["appartement", "f1", "f2", "f3", "f4", "studio"]):
                prop_type = "Appartement"
            else:
                lower_desc = desc.lower()
                if any(k in lower_desc for k in ["villa", "maison"]):
                    prop_type = "Maison"
                elif any(k in lower_desc for k in ["appartement", "studio"]):
                    prop_type = "Appartement"
                elif "terrain" in lower_desc:
                    prop_type = "Terrain"
                elif "dock" in lower_desc:
                    prop_type = "Dock"
                else:
                    prop_type = "Autre"

            facs = []
            lower_text = f"{title} {desc} {url}".lower()
            if "piscine" in lower_text or (detail_data and detail_data.get("has_pool")):
                facs.append("piscine")
            if "clim" in lower_text or (detail_data and detail_data.get("has_air_conditioning")):
                facs.append("climatisation")
            if "vue mer" in lower_text or "vue lagon" in lower_text or (detail_data and detail_data.get("has_sea_view")):
                facs.append("vue_mer")
            if re.search(r"\bmeubl[ée]e?s?\b", lower_text) and not re.search(r"\b(?:non[\s-]meubl[ée]|projection\s+meubl[ée]|home\s+staging)", lower_text):
                facs.append("meuble")
            if detail_data and detail_data.get("has_elevator"):
                facs.append("ascenseur")

            agency = resolve_agency_name(
                raw_name=item.get("agency_name"),
                raw_mail=item.get("agency_email"),
                text_blob=f"{title} {desc}"
            )

            # Champs haute fidélité issus du détail
            raw_surface = None
            raw_terrace = None
            raw_charges = None
            raw_floor = None
            comp_details = None
            direct_neg = None
            direct_ph = None

            if detail_data:
                if detail_data.get("surface_habitable_m2") is not None:
                    raw_surface = str(detail_data["surface_habitable_m2"])
                if detail_data.get("surface_terrasse_m2") is not None:
                    raw_terrace = str(detail_data["surface_terrasse_m2"])
                if detail_data.get("charges_mensuelles_xpf") is not None:
                    raw_charges = str(detail_data["charges_mensuelles_xpf"])
                if detail_data.get("etage") is not None:
                    raw_floor = str(detail_data["etage"])
                comp_details = detail_data.get("complementary_details")
                direct_neg = detail_data.get("direct_negotiator")
                direct_ph = detail_data.get("direct_phone")

            return RawListing(
                source="immonc",
                source_id=item_id,
                url=url,
                title=title,
                description=desc,
                raw_price=price_str,
                raw_surface=raw_surface,
                raw_rooms=str(detail_data.get("rooms")) if detail_data and detail_data.get("rooms") else None,
                raw_location=loc_raw,
                transaction_type_declared="Location" if is_loc else "Vente",
                property_type_declared=prop_type,
                agency_name=agency,
                image_url=photo_url,
                images_json=images_json,
                facilities=facs if facs else None,
                raw_charges=raw_charges,
                raw_terrace=raw_terrace,
                raw_floor=raw_floor,
                complementary_details=comp_details,
                direct_negotiator=direct_neg,
                direct_phone=direct_ph,
                extracted_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.warning(f"Erreur conversion Immo.nc item {item.get('id')} : {e}")
            return None

    def fetch_immonc_mass_listings(
        self,
        max_pages: int = 10,
        include_rentals: bool = True,
        hydrate_details: bool = True,
        hydrate_all: bool = False,
    ) -> List[RawListing]:
        """
        Moissonnage massif et hybride d'Immo.nc :
        1. Capture instantanée des flux cartographiques (1 450+ annonces avec coordonnées et contacts).
        2. Moissonnage multithreadé des pages de résultats.
        3. Hydratation haute fidélité multithreadée des fiches détaillées (charges, terrasses, étage, ascenseur, descriptif intégral).
        """
        logger.info("=== DEMARRAGE DU MOISSONNAGE MASSIF IMMO.NC ===")
        listings_map: Dict[str, RawListing] = {}

        # 1. Capture du flux cartographique géolocalisé
        try:
            for it in self.fetch_immonc_geodata("vente"):
                rl = self.parse_immonc_geo_item_to_raw_listing(it, deal_type="vente")
                if rl:
                    listings_map[rl.source_id] = rl
        except Exception as e:
            logger.warning(f"Erreur flux carto vente : {e}")

        if include_rentals:
            try:
                for it in self.fetch_immonc_geodata("location"):
                    rl = self.parse_immonc_geo_item_to_raw_listing(it, deal_type="location")
                    if rl:
                        listings_map[rl.source_id] = rl
            except Exception as e:
                logger.warning(f"Erreur flux carto location : {e}")

        logger.info(f"Immo.nc : {len(listings_map)} annonces de base capturées via le flux cartographique.")

        # 2. Moissonnage multithreadé des pages de résultats pour enrichissement
        pages_to_crawl = max(1, min(max_pages, 25))
        tasks = []
        with ThreadPoolExecutor(max_workers=4) as ex:
            for p in range(1, pages_to_crawl + 1):
                tasks.append(ex.submit(self.fetch_immonc_raw_items, "vente", p))
                if include_rentals:
                    tasks.append(ex.submit(self.fetch_immonc_raw_items, "location", p))

            page_listings: List[RawListing] = []
            for t in tasks:
                try:
                    for it in t.result():
                        deal = it.get("deal_type", "vente")
                        rl = self.parse_immonc_item_to_raw_listing(it, deal_type=deal)
                        if rl:
                            page_listings.append(rl)
                except Exception as e:
                    logger.warning(f"Erreur traitement page Immo.nc : {e}")

        # 3. Hydratation multithreadée des fiches détaillées pour zéro perte de données
        page_ids = {p.source_id for p in page_listings}
        if hydrate_all:
            extra_geo_items = [rl for sid, rl in listings_map.items() if sid not in page_ids]
        else:
            extra_geo_items = [
                rl for sid, rl in listings_map.items()
                if ("coloc" in (rl.title or "").lower() or "coloc" in (rl.url or "").lower())
                and sid not in page_ids
            ]
        items_to_hydrate = page_listings + extra_geo_items
        if hydrate_details and items_to_hydrate:
            logger.info(f"Hydratation haute fidélité de {len(items_to_hydrate)} annonces Immo.nc (hydrate_all={hydrate_all})...")
            with ThreadPoolExecutor(max_workers=10) as ex_hydr:
                hydrated_results = list(ex_hydr.map(self.hydrate_raw_listing, items_to_hydrate))
        else:
            hydrated_results = items_to_hydrate

        # Fusion des annonces hydratées dans le dictionnaire
        for rl in hydrated_results:
            if rl.source_id in listings_map:
                existing = listings_map[rl.source_id]
                if rl.description and len(rl.description) > len(existing.description or ""):
                    existing.description = rl.description
                if rl.raw_surface:
                    existing.raw_surface = rl.raw_surface
                if rl.raw_terrace:
                    existing.raw_terrace = rl.raw_terrace
                if rl.raw_charges:
                    existing.raw_charges = rl.raw_charges
                if rl.raw_floor:
                    existing.raw_floor = rl.raw_floor
                if rl.complementary_details:
                    existing.complementary_details = rl.complementary_details
                if rl.direct_negotiator:
                    existing.direct_negotiator = rl.direct_negotiator
                if rl.direct_phone:
                    existing.direct_phone = rl.direct_phone
                if rl.images_json:
                    try:
                        ex_imgs = json.loads(existing.images_json) if existing.images_json else []
                        new_imgs = json.loads(rl.images_json)
                        merged_imgs = _dedup_immonc_photos(list(new_imgs) + list(ex_imgs))
                        if merged_imgs:
                            existing.images_json = json.dumps(merged_imgs)
                            existing.image_url = merged_imgs[0]
                    except Exception:
                        pass
                if rl.facilities:
                    existing.facilities = rl.facilities
            else:
                listings_map[rl.source_id] = rl

        all_listings = list(listings_map.values())
        logger.info(f"=== MOISSONNAGE IMMO.NC TERMINE : {len(all_listings)} annonces complètes prêtes pour ingestion ===")
        return all_listings

    def fetch_yatoo_post_by_id(self, item_id: str) -> Optional[Dict[str, Any]]:
        """Récupère une annonce Yatoo.nc individuelle par id ou sid (pour réconciliation live)."""
        query = """
        query getPost($id: String!) {
          post(id: $id) {
            id
            sid
            status
            deleted
            title
            description
            price
            town
            category
            media
            photos
            mobile
            userName
            createdAt
            updatedAt
          }
        }
        """
        try:
            resp = requests.post(
                self.YATOO_GRAPHQL_URL,
                json={"operationName": "getPost", "variables": {"id": item_id}, "query": query},
                headers=self.HEADERS_YATOO,
                timeout=10,
                verify=False,
            )
            if resp.status_code != 200:
                return None
            payload = resp.json()
            return (payload.get("data") or {}).get("post")
        except Exception:
            return None

    def reconcile_active_listings(self, max_workers: int = 12, hydrate_immonc_stubs: bool = True) -> Dict[str, Any]:
        """
        Réconcilie en direct TOUTES les annonces actives en base contre les 4 portails sources :
        - Passe en is_active=FALSE les annonces supprimées / expirées (HTTP 404 / deleted=true).
        - Rafraîchit les annonces modifiées (prix, description propre sans HTML, owner.phone, champs API).
        - Hydrate 100 % des coquilles ImmoNC et déduplique les photos x3.
        - Récupère les vraies photos media et mobiles GraphQL sur Yatoo.nc.
        """
        df = self.db.query("""
            SELECT id, source, source_id, url, title, description, price_xpf,
                   transaction_type, agency_name, images_json, complementary_details_json
            FROM listings
            WHERE is_active = TRUE
        """)
        rows = df.to_dict(orient="records")
        logger.info(f"=== RECONCILIATION LIVE DEMARREE SUR {len(rows)} ANNONCES ACTIVES ===")

        expired_ids: List[str] = []
        refreshed_raw: List[RawListing] = []
        stats = {"checked": len(rows), "expired": 0, "refreshed": 0, "errors": 0, "by_source": {}}

        def _check_row(row: Dict[str, Any]):
            lid = str(row["id"])
            src = str(row["source"]).lower()
            sid = str(row["source_id"])
            url = str(row.get("url") or "")
            try:
                if src in ("immobilier.nc", "bienmeloger.nc"):
                    r = requests.get(
                        f"{self.API_POSTS_URL}/{sid}",
                        headers=self.HEADERS_IMMO_NC,
                        timeout=12,
                        verify=False,
                    )
                    if r.status_code in (404, 410):
                        return ("expired", lid, src, None)
                    if r.status_code == 200:
                        data = (r.json() or {}).get("data")
                        if not data:
                            return ("expired", lid, src, None)
                        rl = self.parse_item_to_raw_listing(data, default_source=src)
                        if rl:
                            # Préserver la source exacte d'origine (évite changement d'ID immobilier.nc <-> bienmeloger.nc)
                            rl.source = src
                            return ("refresh", lid, src, rl)
                    return ("ok", lid, src, None)

                elif src == "yatoo.nc":
                    post = self.fetch_yatoo_post_by_id(sid)
                    if not post:
                        # Fallback par sid court extrait de l'URL
                        short_sid = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
                        if short_sid and short_sid != sid:
                            post = self.fetch_yatoo_post_by_id(short_sid)
                    if not post or post.get("deleted") is True or str(post.get("status") or "").lower() in ("deleted", "expired", "archived"):
                        return ("expired", lid, src, None)
                    cat = str(post.get("category") or ("locations" if "locations" in url else "ventes-immobilieres"))
                    rl = self.parse_yatoo_item_to_raw_listing(post, category=cat)
                    if rl:
                        rl.source_id = sid
                        return ("refresh", lid, src, rl)
                    return ("ok", lid, src, None)

                elif src == "immonc":
                    if not url:
                        return ("ok", lid, src, None)
                    r = requests.get(url, headers=self.HEADERS_IMMONC, timeout=14, verify=False)
                    if r.status_code in (404, 410):
                        return ("expired", lid, src, None)
                    if r.status_code == 200:
                        try:
                            raw_html = r.content.decode("utf-8")
                        except Exception:
                            raw_html = r.content.decode("windows-1252", errors="ignore")
                        low_html = raw_html.lower()
                        if len(raw_html) < 4500 or "annonce introuvable" in low_html or "n'existe plus" in low_html:
                            return ("expired", lid, src, None)

                        detail = parse_immonc_detail_html(raw_html, url=url)
                        # Extraire agence réelle & localisation depuis le HTML détail si présent
                        from bs4 import BeautifulSoup
                        soup = BeautifulSoup(raw_html, "html.parser")
                        h1 = soup.find("h1")
                        live_title = _clean_html_and_cp1252(h1.get_text(" ", strip=True)) if h1 else str(row.get("title") or "")
                        if not live_title or len(live_title) < 3:
                            live_title = str(row.get("title") or "Bien immobilier")

                        ag_name_raw = ""
                        ag_input = soup.find("input", id=re.compile(r"^ad_username"))
                        if ag_input and ag_input.get("value"):
                            ag_name_raw = ag_input.get("value").strip()
                        if not ag_name_raw:
                            ag_link = soup.find("a", title=re.compile(r"Voir les annonces de", re.I))
                            if ag_link and ag_link.get("title"):
                                ag_name_raw = re.sub(r"^Voir les annonces de\s+", "", ag_link["title"], flags=re.I).strip()
                        if not ag_name_raw:
                            part_box = soup.find("div", class_=re.compile(r"agency|agent|partenaire|vendeur", re.I))
                            if part_box:
                                h_ag = part_box.find(["h3", "h4", "strong", "a"])
                                if h_ag:
                                    ag_name_raw = h_ag.get_text(" ", strip=True)

                        ag_email_raw = ""
                        em_input = soup.find("input", id=re.compile(r"^ag_email"))
                        if em_input and em_input.get("value"):
                            ag_email_raw = em_input.get("value").strip()

                        deal_slug = "location" if "/location/" in url.lower() else "vente"
                        is_loc = deal_slug == "location"
                        price_xpf = int(row.get("price_xpf") or 0)
                        # Vérifier le prix live sur la page HTML
                        price_el = soup.find(class_=re.compile(r"venderprice|listing-price|prix", re.I))
                        if price_el:
                            p_digits = re.sub(r"[^\d]", "", price_el.get_text(" ", strip=True))
                            if p_digits and int(p_digits) >= 10_000:
                                price_xpf = int(p_digits)

                        # Localisation dans le slug URL ou breadcrumb
                        loc_hint = ""
                        m_slug = re.search(r"/annonce/[^/]+/[^/]+/([^/]+?)-\d+$", url)
                        if m_slug:
                            loc_hint = m_slug.group(1).replace("-", " ")

                        existing_imgs = []
                        try:
                            if row.get("images_json"):
                                existing_imgs = json.loads(str(row["images_json"]))
                        except Exception:
                            existing_imgs = []

                        item_dict = {
                            "id": sid,
                            "url": url,
                            "title": live_title,
                            "location_raw": loc_hint,
                            "price_xpf": price_xpf,
                            "description": detail.get("full_description") or str(row.get("description") or ""),
                            "agency_name": ag_name_raw or ("" if str(row.get("agency_name") or "") == "Bien-Immo" else str(row.get("agency_name") or "")),
                            "agency_email": ag_email_raw,
                            "photos": _dedup_immonc_photos(list(detail.get("photos") or []) + list(existing_imgs)),
                            "deal_type": deal_slug,
                        }
                        rl = self.parse_immonc_item_to_raw_listing(item_dict, deal_type=deal_slug, detail_data=detail)
                        if rl:
                            return ("refresh", lid, src, rl)
                    return ("ok", lid, src, None)
            except Exception as exc:
                return ("error", lid, src, str(exc))
            return ("ok", lid, src, None)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(_check_row, rows))

        for status, lid, src, payload in results:
            bs = stats["by_source"].setdefault(src, {"checked": 0, "expired": 0, "refreshed": 0, "errors": 0})
            bs["checked"] += 1
            if status == "expired":
                expired_ids.append(lid)
                stats["expired"] += 1
                bs["expired"] += 1
            elif status == "refresh" and payload is not None:
                refreshed_raw.append(payload)
                stats["refreshed"] += 1
                bs["refreshed"] += 1
            elif status == "error":
                stats["errors"] += 1
                bs["errors"] += 1

        if expired_ids:
            self.db.mark_listings_inactive(expired_ids)
        if refreshed_raw:
            etl_res = self.pipeline.process_and_store(refreshed_raw, batch_tag="live_reconciliation")
            stats["etl"] = etl_res

        logger.info(f"=== RECONCILIATION TERMINEE : {stats['expired']} expirées, {stats['refreshed']} rafraîchies, {stats['errors']} erreurs ===")
        return stats

    def run_live_sync(
        self,
        max_pages: int = 10,
        include_rentals: bool = True,
        include_yatoo: bool = True,
        include_immonc: bool = True,
        source_filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Exécute la synchronisation en direct multi-sources complète :
        1. Télécharge les ventes et locations depuis api.immobilier.nc (Fédération des 52 agences NC)
        2. Télécharge les petites annonces en direct depuis Yatoo.nc (GraphQL)
        3. Moissonnage massif d'Immo.nc (immonc.com - 1 450+ annonces réelles)
        4. Convertit et valide via RawListing
        5. Traite via IngestionPipeline (Cleaner + Enricher)
        6. Stocke/met à jour dans DuckDB
        7. Effectue l'audit de qualité et de santé du système
        """
        start_time = datetime.now(timezone.utc)
        logger.info(f"=== DEMARRAGE DU SCAN MULTI-SOURCES REEL (Pages: 1 à {max_pages}) ===")

        all_raw_listings: List[RawListing] = []

        # 1. SCAN DE LA PASSERELLE CENTRALE NC (IMMOBILIER.NC / BIENMELOGER.NC - 52 AGENCES)
        if not source_filter or "immobilier" in source_filter.lower() or "bienmeloger" in source_filter.lower():
            # A. VENTES
            for page in range(1, max_pages + 1):
                items = self.fetch_live_raw_items(deal_type="vente", page=page)
                for it in items:
                    rl = self.parse_item_to_raw_listing(it)
                    if rl:
                        all_raw_listings.append(rl)

            # B. LOCATIONS
            if include_rentals:
                rental_pages = max(2, min(max_pages, 10))
                for page in range(1, rental_pages + 1):
                    items = self.fetch_live_raw_items(deal_type="location", page=page)
                    for it in items:
                        rl = self.parse_item_to_raw_listing(it)
                        if rl:
                            all_raw_listings.append(rl)

        # 2. SCAN EN DIRECT DE YATOO.NC (GRAPHQL)
        if include_yatoo and (not source_filter or "yatoo" in source_filter.lower()):
            for y_cat in ["ventes-immobilieres", "locations"]:
                y_items = self.fetch_yatoo_raw_items(category=y_cat, limit=50, page=1)
                for it in y_items:
                    rl = self.parse_yatoo_item_to_raw_listing(it, category=y_cat)
                    if rl:
                        all_raw_listings.append(rl)

        # 3. SCAN MASSIF D'IMMO.NC (IMMONC.COM)
        if include_immonc and (not source_filter or "immonc" in source_filter.lower()):
            immonc_listings = self.fetch_immonc_mass_listings(max_pages=max_pages, include_rentals=include_rentals)
            all_raw_listings.extend(immonc_listings)

        if source_filter:
            norm_filter = source_filter.lower().strip().replace("_", ".")
            all_raw_listings = [
                rl for rl in all_raw_listings
                if norm_filter in rl.source.lower().replace("_", ".")
                or rl.source.lower().replace("_", ".") in norm_filter
            ]

        logger.info(f"Total annonces brutes collectées prêtes pour ETL : {len(all_raw_listings)}")

        # 3. Exécution du pipeline d'ingestion vers DuckDB
        etl_result = self.pipeline.process_and_store(all_raw_listings, batch_tag="live_multi_sync")

        # 4. Exécution de l'audit de santé
        audit_summary = self.auditor.run_full_audit()
        duration_sec = (datetime.now(timezone.utc) - start_time).total_seconds()

        # 5. Compte total actuel en base
        total_in_db = self.db.query("SELECT COUNT(*) AS c FROM listings WHERE is_active=TRUE").iloc[0]["c"]

        # Répartition par source
        source_breakdown = self.db.query("""
            SELECT source, COUNT(*) AS count
            FROM listings
            WHERE is_active=TRUE
            GROUP BY source
        """).to_dict(orient="records")

        logger.info(
            f"=== SCAN REEL TERMINE EN {duration_sec:.2f}s : "
            f"{etl_result['stored_cleaned']} traitées/insérées, "
            f"Total actif DuckDB : {total_in_db} annonces. Statut audit : {audit_summary.get('status')} ==="
        )

        return {
            "success": True,
            "duration_seconds": round(duration_sec, 2),
            "fetched_count": len(all_raw_listings),
            "stored_cleaned": etl_result["stored_cleaned"],
            "rejected_count": etl_result["rejected"],
            "total_in_db": int(total_in_db),
            "sources_summary": source_breakdown,
            "audit_status": audit_summary.get("status", "HEALTHY"),
            "audit_summary": audit_summary,
        }


if __name__ == "__main__":
    scraper = LiveNCScraper()
    res = scraper.run_live_sync(max_pages=2, include_rentals=True, include_yatoo=True)
    print(json.dumps(res, indent=2))
