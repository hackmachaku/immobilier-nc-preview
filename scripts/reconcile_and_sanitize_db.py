"""
Script de réconciliation live complète + assainissement profond DuckDB + rebuild cache v4 + Canari Live.
Exécute les 5 étapes de mise en conformité Production :
1. Réconciliation live multi-portails (purge 404/deleted, hydratation ImmoNC, photos/mobiles Yatoo, API immobilier.nc)
2. Assainissement local profond (CP1252, HTML, photos dédupliquées, surfaces terrain/hab, meublé, étage, SDB, hors-périmètre)
3. Ré-enrichissement géolocalisation haute précision (REFIL + LOT_CADASTRE gardé + centroïdes recalibrés)
4. Reconstruction complète du cache sémantique v4 (`listing_ai_enrichment`)
5. Contrôle Canari Live (20 fiches / 4 portails -> data/canary_latest.json)
"""

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.storage.database import PropertyDatabase, _DB_LOCK
from src.ingestion.live_scraper import LiveNCScraper, _dedup_immonc_photos, _clean_html_and_cp1252
from src.processing.cleaner import (
    ListingCleaner,
    normalize_etage,
    parse_terrain_surface_m2,
    is_out_of_scope_listing,
)
from src.processing.description_structurer import structure_description
from src.analysis.geo_precision_enricher import GeoPrecisionEnricher
from src.models.listing import PropertyType, TransactionType
from src.quality.canary_verifier import run_canary_verification


def sanitize_duckdb_active_listings(db: PropertyDatabase) -> dict:
    """Passe d'assainissement déterministe sur toutes les annonces encore actives en base."""
    cleaner = ListingCleaner()
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
    to_deactivate = []
    updates = []

    for r in rows:
        lid = str(r["id"])
        title = _clean_html_and_cp1252(r.get("title") or "")
        desc = _clean_html_and_cp1252(r.get("description") or "")
        url = str(r.get("url") or "")

        if is_out_of_scope_listing(title, desc, url):
            to_deactivate.append(lid)
            continue

        # 1. Déduplication photos ImmoNC + suppression placeholders Unsplash si enregistrés en base
        img_url = str(r.get("image_url") or "").strip()
        if "unsplash.com" in img_url:
            img_url = ""
        imgs = []
        raw_ij = r.get("images_json")
        if raw_ij and str(raw_ij).strip() not in ("None", "nan"):
            try:
                parsed = json.loads(str(raw_ij))
                if isinstance(parsed, list):
                    imgs = _dedup_immonc_photos([u for u in parsed if isinstance(u, str) and "unsplash.com" not in u])
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
        # Reclasser parkings/garages hors de MAISON_VILLA
        if pt == PropertyType.MAISON_VILLA and re.match(r"^(?:place\s+de\s+)?(?:parking|garage|box)\b", title.lower()):
            pt = PropertyType.LOCAL_COMMERCIAL

        try:
            tx = TransactionType(str(r.get("transaction_type") or "VENTE").upper())
        except Exception:
            tx = TransactionType.VENTE

        price = int(r.get("price_xpf") or 0)
        # Correction défensive des ventes ÷ 1000 coincées en LOCATION
        desc_low = f"{title} {desc}".lower()
        if tx == TransactionType.LOCATION and 10_000 <= price <= 300_000:
            if "/vente/" in url.lower() or (
                re.search(r"\b(?:prix\s*(?:de\s+vente|fai)?\s*:?\s*\d{1,3}[\s.,]?\d{0,3}\s*(?:mf|millions?|000\s*000)|vendu\s+lou[ée]|actuellement\s+lou[ée].*bail\s+en\s+cours)\b", desc_low)
                and not re.search(r"\b(?:loyer\s*:?\s*" + str(price) + r"|à\s+louer)\b", desc_low)
            ):
                price = price * 1_000
                tx = TransactionType.VENTE

        # 3. Surfaces (correction ha+ares, décimaux, complementaryDetails, terrain vs hab)
        surf_h = float(r.get("surface_habitable_m2") or 0.0) if r.get("surface_habitable_m2") is not None else None
        surf_t = float(r.get("surface_terrain_m2") or 0.0) if r.get("surface_terrain_m2") is not None else None
        surf_var = float(r.get("surface_terrasse_m2") or 0.0) if r.get("surface_terrasse_m2") is not None else None

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
                    if pt != PropertyType.TERRAIN and (not surf_h or surf_h <= 0):
                        m_sh = re.search(r"surface\s*(?:habitable)?\s*(\d+(?:[.,]\d+)?)\s*(?:m2|m²)?", it_s.lower())
                        if m_sh:
                            v_sh = float(m_sh.group(1).replace(",", "."))
                            if 9.0 <= v_sh <= 25_000.0:
                                surf_h = v_sh

        if parsed_t and (not surf_t or surf_t <= 0 or parsed_t >= surf_t * 2.5 or (surf_t >= parsed_t * 2.5 and parsed_t >= 100)):
            surf_t = parsed_t

        # Si surface habitable est un placeholder (< 9 m²) ou égale au terrain (>= 400 m²) sur une villa -> reset
        if surf_h is not None and surf_h < 9.0:
            surf_h = None
        if pt == PropertyType.TERRAIN:
            if (not surf_t or surf_t <= 0) and surf_h and surf_h >= 50.0:
                surf_t = surf_h
            surf_h = None
        elif surf_h and surf_t and surf_t >= 400.0 and abs(surf_h - surf_t) <= 2.0 and pt == PropertyType.MAISON_VILLA:
            surf_h = None

        # 4. Meublé, Piscine, Vue Mer, Étage, SDB, Charges
        is_furn = cleaner.parse_furnished(None, title, desc, pt, tx)
        if is_furn is None and isinstance(comp_dict, dict):
            for k_cd, v_cd in comp_dict.items():
                if "meuble" in str(k_cd).lower() and isinstance(v_cd, list) and len(v_cd) >= 2:
                    is_furn = True
                    break

        from src.reference.geo import normalize_text
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

        # Bathrooms (SDB / SDE)
        bathrooms = int(r["bathrooms"]) if r.get("bathrooms") is not None and not (isinstance(r["bathrooms"], float) and r["bathrooms"] != r["bathrooms"]) and int(r["bathrooms"]) > 0 else None
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

        # Charges (garde-fou charges < loyer + tolérance XFP)
        charges = int(r["charges_mensuelles_xpf"]) if r.get("charges_mensuelles_xpf") is not None and not (isinstance(r["charges_mensuelles_xpf"], float) and r["charges_mensuelles_xpf"] != r["charges_mensuelles_xpf"]) and int(r["charges_mensuelles_xpf"]) > 0 else None
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

        # Cohérence rooms >= bedrooms
        rooms = int(r["rooms"]) if r.get("rooms") is not None and not (isinstance(r["rooms"], float) and r["rooms"] != r["rooms"]) and int(r["rooms"]) > 0 else None
        bedrooms = int(r["bedrooms"]) if r.get("bedrooms") is not None and not (isinstance(r["bedrooms"], float) and r["bedrooms"] != r["bedrooms"]) and int(r["bedrooms"]) >= 0 else None
        if rooms is not None and bedrooms is not None and bedrooms > rooms:
            rooms = bedrooms + 1

        # Prix/m2 recalculé
        pm2 = round(price / surf_h, 2) if (surf_h and surf_h > 0 and price > 0) else None
        pm2_eur = round(pm2 / 119.33174, 2) if pm2 else None

        # Re-vérification anti-faux-positif "Bien-Immo" sur les fiches qui n'auraient pas été rafraîchies
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
        import pandas as pd
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


def rebuild_ai_v4_cache(db: PropertyDatabase) -> int:
    """Reconstruit intégralement le cache listing_ai_enrichment (Tier-1 Déterministe v4) pour 100 % des actifs."""
    from datetime import datetime, timezone
    import pandas as pd

    df = db.query("SELECT id, description, property_type FROM listings WHERE is_active = TRUE")
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
        con.execute("DELETE FROM listing_ai_enrichment")
        con.register("ai_rebuild_df", cache_df)
        con.execute("INSERT INTO listing_ai_enrichment BY NAME SELECT * FROM ai_rebuild_df")

    return len(records)


def main():
    t0 = time.time()
    db = PropertyDatabase()
    scraper = LiveNCScraper(db=db)

    print("=== ETAPE 1/5 : Réconciliation live sur les 4 portails (purge 404 + hydratation + photos) ===")
    rec_stats = scraper.reconcile_active_listings(max_workers=16, hydrate_immonc_stubs=True)
    print(json.dumps({k: v for k, v in rec_stats.items() if k != "etl"}, ensure_ascii=False, indent=2))

    print("\n=== ETAPE 2/5 : Assainissement déterministe DuckDB (surfaces, meublé, étage, SDB, agences, photos) ===")
    san_stats = sanitize_duckdb_active_listings(db)
    print(json.dumps(san_stats, ensure_ascii=False, indent=2))

    print("\n=== ETAPE 3/5 : Ré-enrichissement géolocalisation haute précision (REFIL + LOT_CADASTRE + centroïdes) ===")
    geo_enr = GeoPrecisionEnricher()
    geo_stats = geo_enr.enrich_database_table(max_exif_downloads=0)
    print(json.dumps(geo_stats, ensure_ascii=False, indent=2))

    print("\n=== ETAPE 4/5 : Reconstruction du cache sémantique v4.1 (`listing_ai_enrichment`) ===")
    cached_n = rebuild_ai_v4_cache(db)
    print(f"Cache sémantique v4.1 reconstruit : {cached_n} fiches.")

    print("\n=== ETAPE 5/5 : Contrôle Canari Live (20 fiches / 4 portails) ===")
    from server import NCImmoAPIHandler
    payload_bytes, _ = NCImmoAPIHandler.build_listings_payload_bytes()
    pub_items = (json.loads(payload_bytes.decode("utf-8")) or {}).get("data") or []
    canary = run_canary_verification(pub_items, per_portal=5, seed=2026)
    print(json.dumps({k: v for k, v in canary.items() if k != "samples"}, ensure_ascii=False, indent=2))

    print(f"\n=== TERMINÉ EN {time.time() - t0:.1f}s | Actives publiées : {len(pub_items)} ===")


if __name__ == "__main__":
    main()
