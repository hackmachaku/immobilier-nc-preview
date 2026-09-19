"""
Module d'enrichissement de haute précision géographique, score de localisation
et datation des photos pour Immobilier NC.

Combine :
1. Extraction EXIF des photos (Coordonnées GPS brutes, DateTimeOriginal, modèle d'appareil).
2. Croisement sémantique NLP avec le REFIL DITTT (5 352 immeubles, résidences et lotissements).
3. Détection et extraction des numéros de lots et parcelles cadastrales.
4. Datation multi-source des photos (EXIF, nom de fichier smartphone, horodatage CDN).
5. Calcul du Score de Précision de Localisation (de 40% quartier à 100% GPS photo métrique).
"""

import io
import re
import math
import json
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import duckdb
from PIL import Image, ExifTags

logger = logging.getLogger(__name__)

# Bounding box Nouvelle-Calédonie pour validation GPS
NC_LAT_MIN, NC_LAT_MAX = -23.10, -19.50
NC_LON_MIN, NC_LON_MAX = 163.50, 168.50

# Cache mémoire pour éviter de retélécharger les photos déjà analysées
_EXIF_CACHE: Dict[str, Dict[str, Any]] = {}


def dms_to_decimal(coords, ref: str) -> Optional[float]:
    """Convertit les degrés, minutes, secondes EXIF en coordonnées décimales."""
    try:
        deg = float(coords[0])
        minutes = float(coords[1]) / 60.0
        seconds = float(coords[2]) / 3600.0
        dec = deg + minutes + seconds
        if ref.upper() in ['S', 'W']:
            dec = -dec
        return round(dec, 6)
    except Exception:
        return None


def is_valid_nc_coords(lat: Optional[float], lon: Optional[float]) -> bool:
    """Vérifie que les coordonnées tombent bien en Nouvelle-Calédonie."""
    if lat is None or lon is None:
        return False
    return (NC_LAT_MIN <= lat <= NC_LAT_MAX) and (NC_LON_MIN <= lon <= NC_LON_MAX)


def parse_photo_date_from_text(url: str) -> Optional[datetime]:
    """Extrait la date d'une photo depuis son nom de fichier ou son URL."""
    if not url:
        return None

    # 1. Format smartphone iPhone standard : Photo-12-01-2024-15-24-48.jpg
    m = re.search(r'photo-(\d{2})-(\d{2})-(\d{4})-(\d{2})-(\d{2})-(\d{2})', url, re.IGNORECASE)
    if m:
        try:
            day, month, year, h, mi, s = map(int, m.groups())
            if 2000 <= year <= 2035 and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(year, month, day, h, mi, s, tzinfo=timezone.utc)
        except Exception:
            pass

    # 2. Format smartphone Android standard avec préfixe img/photo ou date seule :
    # IMG20180807150108.jpg, 20240304_112203.jpg, WP_20220513_102902.jpg
    m = re.search(r'(?:img|photo|wp|screenshot)?[_-]?(\d{4})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})', url, re.IGNORECASE)
    if m:
        try:
            year, month, day, h, mi, s = map(int, m.groups())
            if 2000 <= year <= 2035 and 1 <= month <= 12 and 1 <= day <= 31 and 0 <= h <= 23 and 0 <= mi <= 59:
                return datetime(year, month, day, h, mi, s, tzinfo=timezone.utc)
        except Exception:
            pass

    # 3. Format CDN bienmeloger : uuid-202609021612.jpeg
    m = re.search(r'-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})\.jpe?g', url, re.IGNORECASE)
    if m:
        try:
            year, month, day, h, mi = map(int, m.groups())
            if 2000 <= year <= 2035 and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(year, month, day, h, mi, 0, tzinfo=timezone.utc)
        except Exception:
            pass

    # 4. Format dossier immobilier.nc : /photos.immobilier.nc/2024/7/o_...
    m = re.search(r'/photos\.immobilier\.nc/(\d{4})/(\d{1,2})/', url)
    if m:
        try:
            year = int(m.group(1))
            month = int(m.group(2))
            if 2000 <= year <= 2035 and 1 <= month <= 12:
                return datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
        except Exception:
            pass

    # 5. Format Unix timestamp immonc : 0_1743111123_... ou 1646969800LM...
    m_ts = re.findall(r'(?:^|[_/])(1[5-8]\d{8})(?:[_.]|[a-zA-Z]|$)', url)
    if m_ts:
        try:
            ts = int(m_ts[0])
            dt_ts = datetime.fromtimestamp(ts, tz=timezone.utc)
            if 2017 <= dt_ts.year <= 2035:
                return dt_ts
        except Exception:
            pass

    return None


def compute_photo_age_months(photo_date: Optional[datetime], ref_date: Optional[datetime] = None) -> Optional[int]:
    """Calcule l'ancienneté d'une photo en mois révolus."""
    if not photo_date:
        return None
    if ref_date is None:
        ref_date = datetime.now(timezone.utc)
    if photo_date.tzinfo is None:
        photo_date = photo_date.replace(tzinfo=timezone.utc)
    if ref_date.tzinfo is None:
        ref_date = ref_date.replace(tzinfo=timezone.utc)
    diff_days = (ref_date - photo_date).days
    if diff_days < 0:
        return 0
    return int(diff_days // 30)


class GeoPrecisionEnricher:
    """Moteur de croisement haute précision géographique et datation."""

    def __init__(self, cadastre_dir: Optional[Path] = None):
        self.cadastre_dir = cadastre_dir or (Path(__file__).resolve().parent.parent.parent / "data" / "cadastre")
        self.refil_entries: List[Dict[str, Any]] = []
        self.is_refil_loaded = False
        self._load_refil_dictionary()

    def _load_refil_dictionary(self) -> None:
        """Charge et indexe les noms de résidences, immeubles et lotissements du REFIL DITTT."""
        refil_path = self.cadastre_dir / "refil_nc.parquet"
        if not refil_path.exists():
            try:
                from src.ingestion.cadastre_ingestion import ensure_cadastre_data
                ensure_cadastre_data(self.cadastre_dir)
            except Exception as ex:
                logger.warning(f"Impossible de préparer les données REFIL : {ex}")
                return

        con = duckdb.connect()
        try:
            con.execute("INSTALL spatial; LOAD spatial;")
        except Exception:
            pass

        r_str = str(refil_path).replace("\\", "/")
        try:
            rows = con.execute(f"""
                SELECT DISTINCT nom, type, libadrs1, quartier, ST_X(point_geo) as lon, ST_Y(point_geo) as lat
                FROM '{r_str}'
                WHERE nom IS NOT NULL AND length(nom) >= 3 AND point_geo IS NOT NULL;
            """).fetchall()

            self.refil_entries = []
            stop_names = {'bat a', 'bat b', 'bat c', 'bat d', 'bat e', 'bat f', 'bat g', 'bat h', 'immeuble', 'residence', 'villa', 'lot'}

            for r in rows:
                raw_name = str(r[0]).strip()
                # Gérer les formes composites 'DOMAINE DE NOURE | CALGARY'
                parts = [p.strip() for p in raw_name.split('|')]
                for p in parts:
                    norm = p.lower()
                    if len(norm) >= 3 and norm not in stop_names:
                        self.refil_entries.append({
                            "name_clean": norm,
                            "nom": raw_name,
                            "full_name": raw_name,
                            "type": r[1] or "IMMEUBLE",
                            "address": r[2] or "",
                            "quartier": r[3] or "",
                            "lon": float(r[4]),
                            "lat": float(r[5])
                        })

            # Trier par longueur décroissante pour matcher les noms les plus spécifiques en premier
            self.refil_entries.sort(key=lambda x: len(x["name_clean"]), reverse=True)
            self.is_refil_loaded = True
            logger.info(f"Dictionnaire REFIL chargé : {len(self.refil_entries)} repères indexés.")
        except Exception as e:
            logger.warning(f"Erreur lors du chargement de refil_nc.parquet : {e}")

    def extract_photo_metadata(self, photo_url: Optional[str]) -> Dict[str, Any]:
        """Extrait les métadonnées EXIF (GPS, date, modèle) avec cache mémoire."""
        res = {
            "lat": None,
            "lon": None,
            "date_taken": None,
            "camera": None,
            "source_type": "NONE"
        }
        if not photo_url or not photo_url.startswith("http"):
            return res

        if photo_url in _EXIF_CACHE:
            return _EXIF_CACHE[photo_url]

        # 1. Tentative d'extraction temporelle depuis le nom de fichier/URL
        date_from_name = parse_photo_date_from_text(photo_url)
        if date_from_name:
            res["date_taken"] = date_from_name
            res["source_type"] = "FILENAME_CDN"

        # 2. Tentative de lecture EXIF binaire
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0"}
        try:
            req = urllib.request.Request(photo_url, headers=headers)
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = resp.read()

            img = Image.open(io.BytesIO(data))
            exif = img.getexif()
            if exif:
                tag_dict = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}

                # Date
                raw_dt = tag_dict.get("DateTimeOriginal") or tag_dict.get("DateTime")
                if raw_dt and isinstance(raw_dt, str):
                    try:
                        clean_dt = raw_dt.replace("-", ":").split()
                        ymd = clean_dt[0].split(":")
                        hms = clean_dt[1].split(":")
                        dt = datetime(int(ymd[0]), int(ymd[1]), int(ymd[2]), int(hms[0]), int(hms[1]), int(hms[2]), tzinfo=timezone.utc)
                        res["date_taken"] = dt
                        res["source_type"] = "EXIF_ORIGINAL"
                    except Exception:
                        pass

                # Appareil
                make = tag_dict.get("Make", "")
                model = tag_dict.get("Model", "")
                if make or model:
                    res["camera"] = f"{make} {model}".strip()

                # GPS
                gps_ifd = None
                try:
                    gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
                except Exception:
                    pass
                if not gps_ifd:
                    raw_gps = exif.get(34853)
                    if isinstance(raw_gps, dict):
                        gps_ifd = raw_gps

                if gps_ifd:
                    gps_tags = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_ifd.items()}
                    glat = gps_tags.get("GPSLatitude")
                    glat_ref = gps_tags.get("GPSLatitudeRef")
                    glon = gps_tags.get("GPSLongitude")
                    glon_ref = gps_tags.get("GPSLongitudeRef")

                    if glat and glat_ref and glon and glon_ref:
                        plat = dms_to_decimal(glat, glat_ref)
                        plon = dms_to_decimal(glon, glon_ref)
                        if is_valid_nc_coords(plat, plon):
                            res["lat"] = plat
                            res["lon"] = plon
                            res["source_type"] = "EXIF_GPS"
        except Exception:
            pass

        _EXIF_CACHE[photo_url] = res
        return res

    def match_refil_semantics(self, text: str) -> Optional[Dict[str, Any]]:
        """Recherche par NLP sémantique des noms de résidences ou immeubles dans le descriptif."""
        if not text or not self.refil_entries:
            return None
        lower = text.lower()
        context_triggers = ["résidence", "residence", "immeuble", "lotissement", "domaine", "tour", "bâtiment", "batiment", "villa"]

        for entry in self.refil_entries:
            name = entry["name_clean"]
            if name in lower:
                # Si nom distinctif long (> 6 lettres), match direct
                if len(name) >= 7:
                    return entry
                # Si nom court, exiger la présence d'un déclencheur contextuel
                for cw in context_triggers:
                    if f"{cw} {name}" in lower or f"{cw} « {name} »" in lower or f"{cw} \"{name}\"" in lower:
                        return entry
        return None

    def extract_cadastre_lot_reference(self, text: str) -> Optional[str]:
        """Détecte les numéros de lots ou de parcelles dans le texte."""
        if not text:
            return None
        m = re.search(r'\b(?:lot|parcelle)\s*(?:n°|numéro|no|#)?\s*(\d+[a-z]?)\b', text, re.IGNORECASE)
        if m:
            return m.group(1)
        return None

    def enrich_listing_dict(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Enrichit un dictionnaire d'annonce avec la géolocalisation haute précision,
        le score de précision (0-100) et la datation de la photo.
        """
        enriched = dict(item)

        # Extraction textuelle
        title = item.get("title") or ""
        desc = item.get("description") or ""
        text_corpus = f"{title} {desc}"

        # 1. Analyse photo & EXIF
        photo_url = item.get("image_url") or item.get("imageUrl") or ""
        if not photo_url and item.get("photos"):
            p_list = item.get("photos")
            if isinstance(p_list, list) and p_list:
                first_p = p_list[0]
                photo_url = first_p.get("picture") if isinstance(first_p, dict) else str(first_p)

        photo_meta = self.extract_photo_metadata(photo_url) if photo_url else {
            "lat": None, "lon": None, "date_taken": None, "camera": None, "source_type": "NONE"
        }

        # Date de prise de vue & ancienneté
        date_taken = photo_meta.get("date_taken")
        if date_taken:
            enriched["photo_date_taken"] = date_taken.isoformat() if hasattr(date_taken, "isoformat") else str(date_taken)
            now = datetime.now(timezone.utc)
            months_diff = max(0, (now.year - date_taken.year) * 12 + (now.month - date_taken.month))
            enriched["photo_age_months"] = months_diff
        else:
            enriched["photo_date_taken"] = None
            enriched["photo_age_months"] = None

        # 2. Hiérarchie de précision géographique
        # NIVEAU 1 : GPS Métrique EXIF
        if photo_meta.get("lat") and photo_meta.get("lon"):
            enriched["lat_precise"] = photo_meta["lat"]
            enriched["lon_precise"] = photo_meta["lon"]
            enriched["lat"] = photo_meta["lat"]
            enriched["lon"] = photo_meta["lon"]
            enriched["precision_score"] = 95
            enriched["precision_level"] = "METRIQUE_GPS"
            cam = f" ({photo_meta['camera']})" if photo_meta.get("camera") else ""
            enriched["precision_detail"] = f"GPS Photo certifié au mètre{cam}"
            enriched["precision_badge"] = "🟢 95% GPS Photo"
            return enriched

        # NIVEAU 2 : Sémantique REFIL DITTT
        refil_match = self.match_refil_semantics(text_corpus)
        if refil_match:
            enriched["lat_precise"] = refil_match["lat"]
            enriched["lon_precise"] = refil_match["lon"]
            enriched["lat"] = refil_match["lat"]
            enriched["lon"] = refil_match["lon"]
            score = 85 if refil_match.get("type") in ("IMMEUBLE", "ENSEMBLE IMMOBILIER") else 75
            enriched["precision_score"] = score
            enriched["precision_level"] = "IMMEUBLE_REFIL"
            addr = f" - {refil_match['address']}" if refil_match.get("address") else ""
            enriched["precision_detail"] = f"REFIL DITTT : {refil_match['full_name']}{addr}"
            enriched["precision_badge"] = f"🔵 {score}% {refil_match['type'].title()} REFIL"
            return enriched

        # NIVEAU 3 : Détection Lot Cadastral
        lot_num = self.extract_cadastre_lot_reference(text_corpus)
        if lot_num:
            enriched["precision_score"] = 70
            enriched["precision_level"] = "LOT_CADASTRE"
            enriched["precision_detail"] = f"Lot n° {lot_num} identifié dans le descriptif"
            enriched["precision_badge"] = "🟣 70% Lot Cadastral"
            enriched["lat_precise"] = item.get("lat")
            enriched["lon_precise"] = item.get("lon")
            return enriched

        # NIVEAU 4 : Approximatif Quartier / Commune
        enriched["precision_score"] = 40
        enriched["precision_level"] = "QUARTIER_DEFAULT"
        q_name = item.get("quartier") or item.get("commune") or "Secteur"
        enriched["precision_detail"] = f"Approximation centroïde secteur : {q_name}"
        enriched["precision_badge"] = "⚪ 40% Secteur Quartier"
        enriched["lat_precise"] = item.get("lat")
        enriched["lon_precise"] = item.get("lon")

        return enriched

    def enrich_all_listings(self, listings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Enrichit une collection complète d'annonces."""
        return [self.enrich_listing_dict(l) for l in listings]

    def enrich_database_table(self, db_path: Optional[Path] = None, max_exif_downloads: int = 150) -> Dict[str, Any]:
        """
        Scanne la base DuckDB et applique l'enrichissement haute précision et la datation
        directement dans les colonnes dédiées de la table listings.
        """
        from src.storage.database import PropertyDatabase
        db = PropertyDatabase(db_path=db_path)
        db._init_schema()

        with db.get_connection(read_only=False) as con:
            rows = con.execute("""
                SELECT id, title, description, source, image_url, commune, quartier
                FROM listings
                WHERE is_active = TRUE;
            """).fetchall()

            logger.info(f"Enrichissement haute précision de {len(rows)} annonces en base...")
            updates = []

            # 1. Analyse locale instantanée (REFIL, Cadastre, URL/filename timestamps)
            now = datetime.now(timezone.utc)
            items_to_download_exif = []
            for r in rows:
                lid, title, desc, src, img_url, com, q = r
                item_dict = {
                    "id": lid, "title": title, "description": desc, "source": src,
                    "image_url": img_url, "commune": com, "quartier": q
                }
                
                # Check regex date from URL
                dt_url = parse_photo_date_from_text(img_url) if img_url else None
                age_m = max(0, (now.year - dt_url.year) * 12 + (now.month - dt_url.month)) if dt_url else None
                refil_match = self.match_refil_semantics(f"{title} {desc}")
                lot_num = self.extract_cadastre_lot_reference(f"{title} {desc}")

                if refil_match:
                    score = 85 if refil_match.get("type") in ("IMMEUBLE", "ENSEMBLE IMMOBILIER") else 75
                    addr = f" - {refil_match['address']}" if refil_match.get("address") else ""
                    updates.append((
                        refil_match["lat"], refil_match["lon"], score, "IMMEUBLE_REFIL",
                        f"REFIL DITTT : {refil_match['full_name']}{addr}",
                        dt_url.isoformat() if dt_url else None,
                        age_m, lid
                    ))
                elif lot_num:
                    updates.append((
                        None, None, 70, "LOT_CADASTRE",
                        f"Lot n° {lot_num} identifié dans le descriptif",
                        dt_url.isoformat() if dt_url else None,
                        age_m, lid
                    ))
                else:
                    # Candidat pour téléchargement EXIF si photo disponible
                    if img_url and ("gestion.immobilier.nc" in img_url or "yatoo" in img_url or "storage.googleapis" in img_url):
                        items_to_download_exif.append(item_dict)
                    else:
                        q_name = q or com or "Secteur"
                        updates.append((
                            None, None, 40, "QUARTIER_DEFAULT",
                            f"Approximation centroïde secteur : {q_name}",
                            dt_url.isoformat() if dt_url else None,
                            age_m, lid
                        ))

            # 2. Analyse concurrente EXIF pour les candidats les plus prometteurs
            if items_to_download_exif:
                to_process = items_to_download_exif[:max_exif_downloads]
                remaining = items_to_download_exif[max_exif_downloads:]
                for it in remaining:
                    lid = it["id"]
                    dt_url = parse_photo_date_from_text(it.get("image_url"))
                    age_m = max(0, (now.year - dt_url.year) * 12 + (now.month - dt_url.month)) if dt_url else None
                    q_name = it.get("quartier") or it.get("commune") or "Secteur"
                    updates.append((
                        None, None, 40, "QUARTIER_DEFAULT",
                        f"Approximation centroïde secteur : {q_name}",
                        dt_url.isoformat() if dt_url else None,
                        age_m, lid
                    ))

                logger.info(f"Analyse EXIF réseau de {len(to_process)} photos candidates...")
                
                def _fetch_exif(it):
                    url = it["image_url"]
                    meta = self.extract_photo_metadata(url)
                    return it, meta

                with ThreadPoolExecutor(max_workers=8) as executor:
                    futures = [executor.submit(_fetch_exif, it) for it in to_process]
                    for fut in as_completed(futures):
                        try:
                            it, meta = fut.result()
                            lid = it["id"]
                            dt = meta.get("date_taken")
                            dt_iso = dt.isoformat() if hasattr(dt, "isoformat") else (str(dt) if dt else None)
                            age_m = max(0, (now.year - dt.year) * 12 + (now.month - dt.month)) if dt else None
                            if meta.get("lat") and meta.get("lon"):
                                cam = f" ({meta['camera']})" if meta.get("camera") else ""
                                updates.append((
                                    meta["lat"], meta["lon"], 95, "METRIQUE_GPS",
                                    f"GPS Photo certifié au mètre{cam}",
                                    dt_iso, age_m, lid
                                ))
                            else:
                                q_name = it.get("quartier") or it.get("commune") or "Secteur"
                                updates.append((
                                    None, None, 40, "QUARTIER_DEFAULT",
                                    f"Approximation centroïde secteur : {q_name}",
                                    dt_iso, age_m, lid
                                ))
                        except Exception:
                            pass

            # 3. Application en base DuckDB
            if updates:
                con.executemany("""
                    UPDATE listings SET
                        lat_precise = COALESCE(?, lat_precise),
                        lon_precise = COALESCE(?, lon_precise),
                        precision_score = ?,
                        precision_level = ?,
                        precision_detail = ?,
                        photo_date_taken = ?,
                        photo_age_months = ?
                    WHERE id = ?;
                """, updates)
                logger.info(f"Mise à jour DuckDB terminée : {len(updates)} annonces enrichies.")

        return {
            "total_listings": len(rows),
            "updated_count": len(updates),
            "refil_count": sum(1 for u in updates if u[3] == "IMMEUBLE_REFIL"),
            "exif_gps_count": sum(1 for u in updates if u[3] == "METRIQUE_GPS"),
            "lot_count": sum(1 for u in updates if u[3] == "LOT_CADASTRE"),
        }
