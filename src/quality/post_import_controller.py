"""
Contrôleur Qualité Automatique Post-Import — Sentinel ImmoNC.
Exécuté SYSTÉMATIQUEMENT à chaque import (IngestionPipeline, LiveNCScraper.run_live_sync,
POST /api/refresh, main.py, reconcile_and_sanitize_db.py) :
1. Assainissement déterministe DuckDB (hors-périmètre, CP1252, HTML, photos dédupliquées, surfaces, meublé, étage, SDB, charges, agences).
2. Enrichissement géolocalisation haute précision (REFIL + LOT_CADASTRE avec garde-fous).
3. Mise à jour automatique du cache sémantique v4.1 (`listing_ai_enrichment`).
4. Vérification Canari Live contre les sites sources (20 fiches / 4 portails -> data/canary_latest.json).
5. Évaluation Quality Gate complète (score A/B/C/D, quarantaine P0, statut PASS/WARN/BLOCK)
   et archivage horodaté dans logs/quality_gate_last.json + logs/quality_gate_history.jsonl.
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from src.models.listing import PropertyType, TransactionType
from src.processing.cleaner import (
    ListingCleaner,
    is_out_of_scope_listing,
    normalize_etage,
    parse_terrain_surface_m2,
)
from src.processing.description_structurer import structure_description
from src.quality.canary_verifier import load_latest_canary_report, run_canary_verification
from src.quality.quality_gate import evaluate_listing_quality, run_quality_gate
from src.reference.geo import normalize_text
from src.storage.database import PropertyDatabase, _DB_LOCK
from src.utils.logger import get_logger

logger = get_logger("post_import_controller")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOGS_DIR = BASE_DIR / "logs"

_CP1252_TRANS = str.maketrans({
    "\x80": "€", "\x85": "...", "\x91": "'", "\x92": "'",
    "\x93": '"', "\x94": '"', "\x95": "•", "\x96": "–", "\x97": "—",
})


def _clean_text_cp1252_html(text: Optional[str]) -> str:
    if not text:
        return ""
    import html as html_lib
    s = str(text).translate(_CP1252_TRANS)
    s = re.sub(r"[\x80-\x9f]", " ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</\s*(?:p|div|li|h[1-6]|tr)\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</?[a-zA-Z][^>]*>", "", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _dedup_photos_local(urls: List[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw_u in urls:
        if not raw_u or not isinstance(raw_u, str):
            continue
        u = raw_u.strip()
        if not u.startswith("http") or "unsplash.com" in u:
            continue
        u = re.sub(r"/photos_(?:medium|small|vignette|thumb)/", "/photos_big/", u)
        u = re.sub(r"^https?://(?:www\.)?immonc\.com/", "https://www.immonc.com/", u, flags=re.IGNORECASE)
        fname = u.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()
        key = fname if ("immonc.com" in u.lower() and fname) else u.lower()
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out


def sanitize_duckdb_active_listings(
    db: PropertyDatabase,
    listing_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Passe d'assainissement déterministe sur les annonces actives en base
    (tout le parc si listing_ids=None, ou incrémental sur listing_ids après un import ciblé).
    """
    cleaner = ListingCleaner()
    if listing_ids:
        clean_ids = [str(i).strip() for i in listing_ids if i and str(i).strip()]
        if not clean_ids:
            return {"sanitized": 0, "deactivated_out_of_scope": 0}
        placeholders = ",".join(["?"] * len(clean_ids))
        df = db.query(
            f"""
            SELECT id, source, url, title, description, transaction_type, property_type,
                   price_xpf, charges_mensuelles_xpf, surface_habitable_m2, surface_terrain_m2,
                   surface_terrasse_m2, rooms, bedrooms, bathrooms, parkings, etage,
                   has_sea_view, has_pool, is_furnished, agency_name, image_url, images_json,
                   complementary_details_json, direct_phone
            FROM listings
            WHERE is_active = TRUE AND id IN ({placeholders})
            """,
            clean_ids,
        )
    else:
        df = db.query("""
            SELECT id, source, url, title, description, transaction_type, property_type,
                   price_xpf, charges_mensuelles_xpf, surface_habitable_m2, surface_terrain_m2,
                   surface_terrasse_m2, rooms, bedrooms, bathrooms, parkings, etage,
                   has_sea_view, has_pool, is_furnished, agency_name, image_url, images_json,
                   complementary_details_json, direct_phone
            FROM listings
            WHERE is_active = TRUE
        """)

    rows = df.to_dict(orient="records")
    to_deactivate: List[str] = []
    updates = []

    for r in rows:
        lid = str(r["id"])
        title = _clean_text_cp1252_html(r.get("title") or "")
        desc = _clean_text_cp1252_html(r.get("description") or "")
        url = str(r.get("url") or "")

        if is_out_of_scope_listing(title, desc, url):
            to_deactivate.append(lid)
            continue

        # 1. Déduplication photos ImmoNC + suppression placeholders Unsplash
        img_url = str(r.get("image_url") or "").strip()
        if "unsplash.com" in img_url:
            img_url = ""
        imgs: List[str] = []
        raw_ij = r.get("images_json")
        if raw_ij and str(raw_ij).strip() not in ("None", "nan"):
            try:
                parsed = json.loads(str(raw_ij))
                if isinstance(parsed, list):
                    imgs = _dedup_photos_local(parsed)
            except Exception:
                imgs = []
        if imgs:
            img_url = imgs[0]
            images_json = json.dumps(imgs)
        else:
            images_json = None
            img_url = img_url or None

        # 2. Type bien & Transaction
        try:
            pt = PropertyType(str(r.get("property_type") or "AUTRE").upper())
        except Exception:
            pt = PropertyType.AUTRE
        if pt == PropertyType.MAISON_VILLA and re.match(r"^(?:place\s+de\s+)?(?:parking|garage|box)\b", title.lower()):
            pt = PropertyType.LOCAL_COMMERCIAL

        try:
            tx = TransactionType(str(r.get("transaction_type") or "VENTE").upper())
        except Exception:
            tx = TransactionType.VENTE

        price = int(r.get("price_xpf") or 0)
        desc_low = f"{title} {desc}".lower()
        if tx == TransactionType.LOCATION and 10_000 <= price <= 300_000:
            if "/vente/" in url.lower() or (
                re.search(r"\b(?:prix\s*(?:de\s+vente|fai)?\s*:?\s*\d{1,3}[\s.,]?\d{0,3}\s*(?:mf|millions?|000\s*000)|vendu\s+lou[ée]|actuellement\s+lou[ée].*bail\s+en\s+cours)\b", desc_low)
                and not re.search(r"\b(?:loyer\s*:?\s*" + str(price) + r"|à\s+louer)\b", desc_low)
            ):
                price = price * 1_000
                tx = TransactionType.VENTE

        # 3. Surfaces
        def _opt_float(val):
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return None
            try:
                f = float(val)
                return f if f > 0 else None
            except Exception:
                return None

        surf_h = _opt_float(r.get("surface_habitable_m2"))
        surf_t = _opt_float(r.get("surface_terrain_m2"))
        surf_var = _opt_float(r.get("surface_terrasse_m2"))

        parsed_t = parse_terrain_surface_m2(f"{title} {desc}")
        comp_dict = None
        if r.get("complementary_details_json") and str(r["complementary_details_json"]).strip() not in ("None", "nan"):
            try:
                comp_dict = json.loads(str(r["complementary_details_json"]))
            except Exception:
                comp_dict = None

        if isinstance(comp_dict, dict):
            for cat, items in comp_dict.items():
                if not isinstance(items, list):
                    continue
                for it in items:
                    it_s = str(it)
                    if "sur terrain" in it_s.lower() or "terrain" in str(cat).lower():
                        pt_cd = parse_terrain_surface_m2(it_s)
                        if not pt_cd:
                            m_bt = re.search(r"sur\s+terrain\s+(\d+(?:[.,]\d+)?)\s*$", it_s.lower())
                            if m_bt:
                                bt = float(m_bt.group(1).replace(",", "."))
                                pt_cd = round(bt * 100.0, 1) if 0.5 <= bt < 50.0 else (bt if bt >= 50.0 else None)
                        if pt_cd and (not parsed_t or pt_cd > parsed_t):
                            parsed_t = pt_cd
                    if pt != PropertyType.TERRAIN and not surf_h:
                        m_sh = re.search(r"surface\s*(?:habitable)?\s*(\d+(?:[.,]\d+)?)\s*(?:m2|m²)?", it_s.lower())
                        if m_sh:
                            v_sh = float(m_sh.group(1).replace(",", "."))
                            if 9.0 <= v_sh <= 25_000.0:
                                surf_h = v_sh

        if parsed_t and (not surf_t or parsed_t >= surf_t * 2.5 or (surf_t >= parsed_t * 2.5 and parsed_t >= 100)):
            surf_t = parsed_t

        if surf_h is not None and surf_h < 9.0:
            surf_h = None
        if pt == PropertyType.TERRAIN:
            if not surf_t and surf_h and surf_h >= 50.0:
                surf_t = surf_h
            surf_h = None
        elif surf_h and surf_t and surf_t >= 400.0 and abs(surf_h - surf_t) <= 2.0 and pt == PropertyType.MAISON_VILLA:
            surf_h = None

        # 4. Attributs qualitatifs & normalisation
        is_furn = cleaner.parse_furnished(None, title, desc, pt, tx)
        if is_furn is None and isinstance(comp_dict, dict):
            for k_cd, v_cd in comp_dict.items():
                if "meuble" in str(k_cd).lower() and isinstance(v_cd, list) and len(v_cd) >= 2:
                    is_furn = True
                    break

        blob_n = normalize_text(f"{title} {desc}")
        has_sea_view = bool(re.search(r"\b(vue\s*(?:sur\s+la\s+)?mer|vue\s*(?:sur\s+le\s+)?lagon|vue\s*(?:sur\s+la\s+)?baie|vue\s*panoramique\s*(?:sur\s+la\s+)?mer|apercu\s*mer)\b", blob_n))
        has_pool = bool(
            re.search(r"\b(piscine|bassin\s+de\s+nage|couloir\s+de\s+nage)\b", blob_n)
            and not re.search(r"\b(sans\s+piscine|pas\s+de\s+piscine|piscine\s*:\s*non|dalle\s+(?:de\s+)?piscine|projet\s+de\s+piscine|piscinable)\b", blob_n)
        )
        if isinstance(comp_dict, dict):
            for k_cd, v_cd in comp_dict.items():
                if not isinstance(v_cd, list):
                    continue
                for it in v_cd:
                    itn = normalize_text(str(it))
                    if any(w in itn for w in ("vue mer", "vue lagon", "vue baie")) and "non" not in itn:
                        has_sea_view = True
                    if "piscine" in itn and not re.search(r"\b(non|piscinable)\b", itn):
                        has_pool = True

        etage_norm = normalize_etage(r.get("etage"))
        if etage_norm == "Rez-de-chaussée" and re.search(r"\b(?:commerces?|locaux|boutiques?)\s+au\s+(?:rdc|rez[- ]de[- ]chauss[eé]e)\b", desc_low):
            etage_norm = None

        def _opt_int(val, allow_zero=False):
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return None
            try:
                iv = int(val)
                return iv if (iv >= 0 if allow_zero else iv > 0) else None
            except Exception:
                return None

        bathrooms = _opt_int(r.get("bathrooms"))
        if bathrooms is None and pt not in (PropertyType.TERRAIN, PropertyType.DOCK):
            sdb_words_map = {"une": 1, "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5}
            m_sdb = re.search(
                r"\b([1-9]|une|deux|trois|quatre|cinq)\s*(?:belles?\s+|grandes?\s+|petites?\s+)?(?:salles?\s*d\s*eau|salles?\s*de\s*bains?|sdb|sde)\b",
                blob_n,
            )
            if m_sdb:
                tok = m_sdb.group(1)
                bathrooms = sdb_words_map.get(tok, int(tok) if tok.isdigit() else 1)
            elif re.search(r"\b(?:salle\s*d\s*eau|salle\s*de\s*bains?)\b", blob_n):
                bathrooms = 1

        charges = _opt_int(r.get("charges_mensuelles_xpf"))
        if charges is None:
            m_chg = re.search(
                r"(?<!hors\s)(?<!sans\s)\bcharges?\s*(?:mensuelles?|de\s+copropri[eé]t[eé]|copro|en\s+sus)?\s*[:=\s]?\s*(\d{1,3}(?:[\s.]?\d{3})*|\d{3,6})(?:[.,]\d{1,2})?\s*(?:f\s*cfp|xpf|xfp|f\b|cfp|/mois)",
                desc,
                re.IGNORECASE,
            )
            if m_chg:
                cc = re.sub(r"[^\d]", "", m_chg.group(1))
                if cc and 500 <= int(cc) <= 150_000:
                    charges = int(cc)
        if charges is not None and tx == TransactionType.LOCATION and price > 0 and charges >= price:
            charges = None

        rooms = _opt_int(r.get("rooms"))
        bedrooms = _opt_int(r.get("bedrooms"), allow_zero=True)
        if rooms is not None and bedrooms is not None and bedrooms > rooms:
            rooms = bedrooms + 1

        pm2 = round(price / surf_h, 2) if (surf_h and surf_h > 0 and price > 0) else None
        pm2_eur = round(pm2 / 119.33174, 2) if pm2 else None

        ag_name = str(r.get("agency_name") or "").strip()
        if ag_name == "Bien-Immo" and not re.search(r"\bbien[\s-]*immo\b", f"{desc} {url}", re.I):
            ag_name = "Particulier NC"

        updates.append((
            title, desc, tx.value, pt.value, price, round(price / 119.33174, 2),
            charges, surf_h, surf_t, surf_var, pm2, pm2_eur,
            rooms, bedrooms, bathrooms, etage_norm,
            has_sea_view, has_pool, is_furn, ag_name, img_url, images_json, lid
        ))

    if to_deactivate:
        db.mark_listings_inactive(to_deactivate)

    if updates:
        upd_df = pd.DataFrame(updates, columns=[
            "title", "description", "transaction_type", "property_type", "price_xpf", "price_eur",
            "charges_mensuelles_xpf", "surface_habitable_m2", "surface_terrain_m2", "surface_terrasse_m2",
            "prix_m2_habitable_xpf", "prix_m2_habitable_eur",
            "rooms", "bedrooms", "bathrooms", "etage",
            "has_sea_view", "has_pool", "is_furnished", "agency_name", "image_url", "images_json", "id"
        ])
        with _DB_LOCK, db.get_connection(read_only=False) as con:
            con.register("san_df", upd_df)
            con.execute("""
                UPDATE listings AS l
                SET
                    title = s.title,
                    description = s.description,
                    transaction_type = s.transaction_type,
                    property_type = s.property_type,
                    price_xpf = s.price_xpf,
                    price_eur = s.price_eur,
                    charges_mensuelles_xpf = s.charges_mensuelles_xpf,
                    surface_habitable_m2 = s.surface_habitable_m2,
                    surface_terrain_m2 = s.surface_terrain_m2,
                    surface_terrasse_m2 = s.surface_terrasse_m2,
                    prix_m2_habitable_xpf = s.prix_m2_habitable_xpf,
                    prix_m2_habitable_eur = s.prix_m2_habitable_eur,
                    rooms = s.rooms,
                    bedrooms = s.bedrooms,
                    bathrooms = s.bathrooms,
                    etage = s.etage,
                    has_sea_view = s.has_sea_view,
                    has_pool = s.has_pool,
                    is_furnished = s.is_furnished,
                    agency_name = s.agency_name,
                    image_url = s.image_url,
                    images_json = s.images_json
                FROM san_df AS s
                WHERE l.id = s.id
            """)

    return {"sanitized": len(updates), "deactivated_out_of_scope": len(to_deactivate)}


def rebuild_ai_v4_cache(
    db: PropertyDatabase,
    listing_ids: Optional[List[str]] = None,
) -> int:
    """
    Met à jour le cache sémantique v4.1 (`listing_ai_enrichment`) :
    - Si listing_ids est fourni : upsert incrémental des fiches importées (< 1 s).
    - Sinon : reconstruction complète pour 100 % des annonces actives.
    """
    if listing_ids:
        clean_ids = [str(i).strip() for i in listing_ids if i and str(i).strip()]
        if not clean_ids:
            return 0
        placeholders = ",".join(["?"] * len(clean_ids))
        df = db.query(
            f"SELECT id, description, property_type FROM listings WHERE is_active = TRUE AND id IN ({placeholders})",
            clean_ids,
        )
    else:
        df = db.query("SELECT id, description, property_type FROM listings WHERE is_active = TRUE")

    if df.empty:
        return 0

    now_iso = datetime.now(timezone.utc)
    records = []
    for r in df.to_dict(orient="records"):
        lid = str(r["id"])
        desc = str(r.get("description") or "")
        pt = str(r.get("property_type") or "")
        s = structure_description(desc, property_type=pt)
        raw_resp = {
            "technical_specs": s.get("technical_specs", {}),
            "key_highlights": s.get("key_highlights", []),
            "agency_metadata": s.get("agency_metadata", {}),
        }
        records.append({
            "listing_id": lid,
            "model_version": "Tier-1 Déterministe v4.1",
            "enriched_at": now_iso,
            "extracted_contacts_json": json.dumps(s.get("direct_contacts", []), ensure_ascii=False),
            "structured_sections_json": json.dumps(s.get("sections", []), ensure_ascii=False),
            "raw_response_json": json.dumps(raw_resp, ensure_ascii=False),
        })

    cache_df = pd.DataFrame(records)
    with _DB_LOCK, db.get_connection(read_only=False) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS listing_ai_enrichment (
                listing_id VARCHAR PRIMARY KEY,
                model_version VARCHAR,
                enriched_at TIMESTAMP,
                extracted_contacts_json VARCHAR,
                structured_sections_json VARCHAR,
                raw_response_json VARCHAR
            )
        """)
        con.register("ai_rebuild_df", cache_df)
        if listing_ids:
            con.execute("""
                INSERT INTO listing_ai_enrichment BY NAME
                SELECT * FROM ai_rebuild_df
                ON CONFLICT (listing_id) DO UPDATE SET
                    model_version = EXCLUDED.model_version,
                    enriched_at = EXCLUDED.enriched_at,
                    extracted_contacts_json = EXCLUDED.extracted_contacts_json,
                    structured_sections_json = EXCLUDED.structured_sections_json,
                    raw_response_json = EXCLUDED.raw_response_json
            """)
        else:
            con.execute("DELETE FROM listing_ai_enrichment")
            con.execute("INSERT INTO listing_ai_enrichment BY NAME SELECT * FROM ai_rebuild_df")

    return len(records)


def _evaluate_db_quality_quick(
    db: PropertyDatabase,
    listing_ids: Optional[List[str]] = None,
    canary_report: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Construit une vue rapide des fiches actives en base et exécute run_quality_gate()."""
    if listing_ids:
        clean_ids = [str(i).strip() for i in listing_ids if i and str(i).strip()]
        if clean_ids:
            placeholders = ",".join(["?"] * len(clean_ids))
            df = db.query(
                f"SELECT * FROM listings WHERE is_active = TRUE AND id IN ({placeholders})",
                clean_ids,
            )
        else:
            df = pd.DataFrame()
    else:
        df = db.query("SELECT * FROM listings WHERE is_active = TRUE")

    if df.empty:
        _, _, rep = run_quality_gate([], canary_report=canary_report)
        return [], rep

    # Charger les sections structurées du cache v4.1 si la table existe
    sd_map: Dict[str, list] = {}
    try:
        ai_df = db.query("SELECT listing_id, structured_sections_json FROM listing_ai_enrichment")
        for r in ai_df.to_dict(orient="records"):
            raw_s = r.get("structured_sections_json")
            if raw_s and str(raw_s).strip():
                try:
                    sd_map[str(r["listing_id"])] = json.loads(str(raw_s))
                except Exception:
                    pass
    except Exception:
        pass

    cat_map = {
        "TERRAIN": "terrain",
        "DOCK": "dock_industriel",
        "LOCAL_COMMERCIAL": "local_commercial",
        "IMMEUBLE": "local_commercial",
        "APPARTEMENT": "appartement",
    }

    items: List[Dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        lid = str(row["id"])
        pt = str(row.get("property_type") or "").upper()
        cat = cat_map.get(pt, "maison_villa")
        img = str(row.get("image_url") or "").strip()
        is_def = (not img) or ("unsplash.com" in img)
        lat = row.get("lat_precise")
        lon = row.get("lon_precise")
        lat_val = float(lat) if (lat is not None and not pd.isna(lat)) else None
        lon_val = float(lon) if (lon is not None and not pd.isna(lon)) else None
        items.append({
            "id": lid,
            "source": str(row.get("source") or ""),
            "sourceUrl": str(row.get("url") or ""),
            "title": str(row.get("title") or ""),
            "description": str(row.get("description") or ""),
            "category": cat,
            "transactionType": str(row.get("transaction_type") or "VENTE").upper(),
            "currentPrice": int(row["price_xpf"]) if (row.get("price_xpf") is not None and not pd.isna(row["price_xpf"])) else 0,
            "surfaceHabitable": float(row["surface_habitable_m2"]) if (row.get("surface_habitable_m2") is not None and not pd.isna(row["surface_habitable_m2"])) else 0.0,
            "surfaceTerrain": float(row["surface_terrain_m2"]) if (row.get("surface_terrain_m2") is not None and not pd.isna(row["surface_terrain_m2"])) else 0.0,
            "chargesMensuelles": int(row["charges_mensuelles_xpf"]) if (row.get("charges_mensuelles_xpf") is not None and not pd.isna(row["charges_mensuelles_xpf"])) else None,
            "rooms": int(row["rooms"]) if (row.get("rooms") is not None and not pd.isna(row["rooms"])) else None,
            "bedrooms": int(row["bedrooms"]) if (row.get("bedrooms") is not None and not pd.isna(row["bedrooms"])) else None,
            "quartier": str(row.get("quartier") or ""),
            "lat": lat_val,
            "lon": lon_val,
            "precisionScore": int(row["precision_score"]) if (row.get("precision_score") is not None and not pd.isna(row["precision_score"])) else 40,
            "isDefaultImage": is_def,
            "agencyPhone": str(row.get("direct_phone") or "Non renseigné"),
            "directContacts": [{"phone": row["direct_phone"]}] if (row.get("direct_phone") and not pd.isna(row["direct_phone"])) else [],
            "structuredDescription": sd_map.get(lid, []),
        })

    pub, _quar, rep = run_quality_gate(items, canary_report=canary_report)
    return pub, rep


def run_post_import_control(
    db: Optional[PropertyDatabase] = None,
    listing_ids: Optional[List[str]] = None,
    batch_tag: str = "import",
    run_geo: bool = True,
    rebuild_ai_cache: bool = True,
    run_canary: bool = False,
    canary_per_portal: int = 5,
    save_report: bool = True,
    log_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Point unique du contrôle qualité automatique post-import.
    Garantit qu'à CHAQUE import :
    - Les annonces sont assainies (CP1252, HTML, photos dédupliquées, surfaces, meublé, étages, SDB, agences, hors-périmètre)
    - Le cache sémantique v4.1 est synchronisé
    - La géolocalisation haute précision REFIL / Cadastre est mise à jour (si run_geo=True)
    - Un contrôle Canari live contre les sites sources est exécuté (si run_canary=True)
    - Le Quality Gate évalue l'état (PASS / WARN / BLOCK) et archive le rapport.
    """
    t0 = time.time()
    database = db or PropertyDatabase()

    # 1. Assainissement déterministe
    san_stats = sanitize_duckdb_active_listings(database, listing_ids=listing_ids)

    # 2. Enrichissement géolocalisation REFIL / Cadastre (hors mode unitaire rapide)
    geo_stats = None
    if run_geo:
        try:
            from src.analysis.geo_precision_enricher import GeoPrecisionEnricher
            geo_stats = GeoPrecisionEnricher().enrich_database_table(
                db_path=database.db_path, max_exif_downloads=0
            )
        except Exception as exc:
            logger.warning(f"Post-import GeoPrecision ignoré : {exc}")

    # 3. Mise à jour du cache sémantique v4.1
    cached_n = 0
    if rebuild_ai_cache:
        try:
            cached_n = rebuild_ai_v4_cache(database, listing_ids=listing_ids)
        except Exception as exc:
            logger.warning(f"Post-import rebuild cache v4.1 ignoré : {exc}")

    # 4. Évaluation Quality Gate + Contrôle Canari live optionnel
    canary_report = load_latest_canary_report()
    pub_items, q_report = _evaluate_db_quality_quick(
        database, listing_ids=listing_ids, canary_report=canary_report
    )

    if run_canary and pub_items:
        try:
            canary_report = run_canary_verification(pub_items, per_portal=canary_per_portal)
            _, q_report = _evaluate_db_quality_quick(
                database, listing_ids=listing_ids, canary_report=canary_report
            )
        except Exception as exc:
            logger.warning(f"Post-import Canary Live ignoré : {exc}")

    duration_s = round(time.time() - t0, 2)
    result = {
        "batch_tag": batch_tag,
        "controlled_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": duration_s,
        "scope": f"{len(listing_ids)} annonces (incrémental)" if listing_ids else "base complète",
        "sanitization": san_stats,
        "geo_precision": geo_stats,
        "ai_cache_updated": cached_n,
        "gate_status": q_report.get("gateStatus", "PASS"),
        "block_reasons": q_report.get("blockReasons", []),
        "warn_reasons": q_report.get("warnReasons", []),
        "summary": q_report.get("summary", {}),
        "canary_freshness_pct": canary_report.get("freshnessPct") if isinstance(canary_report, dict) else None,
        "canary_price_match_pct": canary_report.get("priceExactMatchPct") if isinstance(canary_report, dict) else None,
    }

    if save_report:
        target_log_dir = log_dir or LOGS_DIR
        try:
            target_log_dir.mkdir(parents=True, exist_ok=True)
            (target_log_dir / "quality_gate_last.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            with open(target_log_dir / "quality_gate_history.jsonl", "a", encoding="utf-8") as hf:
                hf.write(json.dumps({
                    "controlled_at": result["controlled_at"],
                    "batch_tag": batch_tag,
                    "gate_status": result["gate_status"],
                    "avg_score": result["summary"].get("avgScore"),
                    "published": result["summary"].get("publishedCount"),
                    "quarantined": result["summary"].get("quarantinedCount"),
                    "canary_freshness_pct": result["canary_freshness_pct"],
                    "sanitized": san_stats.get("sanitized", 0),
                    "deactivated_out_of_scope": san_stats.get("deactivated_out_of_scope", 0),
                }, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning(f"Impossible d'archiver le rapport post-import : {exc}")

    logger.info(
        f"[AUTO-QUALITY-CONTROL] batch='{batch_tag}' ({result['scope']}) terminé en {duration_s}s | "
        f"Gate={result['gate_status']} | Score={result['summary'].get('avgScore')}/100 | "
        f"Assainies={san_stats.get('sanitized')} | Hors-périmètre={san_stats.get('deactivated_out_of_scope')} | "
        f"Quarantaine={result['summary'].get('quarantinedCount')}"
    )
    return result
