"""
Script d'audit, de correction géographique et de ré-enrichissement haute précision
pour l'ensemble des 2 683 annonces de la base DuckDB Immobilier NC.
"""

import re
import sys
from pathlib import Path
from datetime import datetime, timezone

# Ensure root directory is on path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import duckdb
from src.config.settings import DB_PATH
from src.models.listing import Commune
from src.reference.geo import GeoReferential
from src.analysis.geo_precision_enricher import GeoPrecisionEnricher, parse_photo_date_from_text

def run_migration_and_audit():
    print("=" * 70)
    print("MIGRATION & AUDIT GÉOGRAPHIQUE GLOBAL DES ANNONCES (DUCKDB)")
    print("=" * 70)
    
    geo_ref = GeoReferential()
    enricher = GeoPrecisionEnricher()
    
    con = duckdb.connect(str(DB_PATH), read_only=False)
    
    rows = con.execute("""
        SELECT id, title, description, commune, quartier, source, image_url,
               lat_precise, lon_precise, precision_score, precision_level, precision_detail
        FROM listings
        WHERE is_active = TRUE;
    """).fetchall()
    
    total = len(rows)
    print(f"Total d'annonces actives en base : {total}")
    
    now = datetime.now(timezone.utc)
    
    updated_quartiers_count = 0
    refil_matches_count = 0
    refil_prevented_false_positives = 0
    pk_listings_fixed = 0
    
    updates = []
    
    for r in rows:
        lid, title, desc, com_orig, q_orig, src, img_url, old_lat_p, old_lon_p, old_score, old_lvl, old_detail = r
        title_str = str(title or "").strip()
        desc_str = str(desc or "").strip()
        clean_desc = re.sub(r'(?:contact|tél|tel|email|mail|agent|notre agence|retrouvez-nous)\s*:?.*', '', desc_str, flags=re.IGNORECASE)
        
        # 1. Détection hiérarchique du quartier et de la commune
        # Priorité 1 : Titre
        c_found, q_found, _ = geo_ref.find_location(title_str)
        
        # Priorité 2 : Si non trouvé dans le titre, conserver l'ancien quartier s'il est valide
        if not q_found and q_orig and str(q_orig).strip().lower() not in ("none", "nan", "secteur calédonien", "secteur"):
            q_found = str(q_orig).strip()
            if com_orig and str(com_orig).strip().upper() in Commune.__members__:
                c_found = Commune[str(com_orig).strip().upper()]
                
        # Priorité 3 : Descriptif nettoyé
        if not q_found and clean_desc:
            c_d, q_d, _ = geo_ref.find_location(clean_desc)
            if q_d:
                q_found = q_d
                if c_found == Commune.AUTRE:
                    c_found = c_d
                    
        # Commune finale
        com_final = c_found.value if c_found != Commune.AUTRE else (str(com_orig or "NOUMEA").upper())
        if com_final not in Commune.__members__:
            com_final = "NOUMEA"
            
        quartier_final = q_found
        
        if (q_orig != quartier_final) and quartier_final:
            updated_quartiers_count += 1
            if "PK" in quartier_final.upper() or "P.K" in title_str.upper():
                pk_listings_fixed += 1
                
        # 2. Datation de la photo (URL / timestamp)
        dt_url = parse_photo_date_from_text(img_url) if img_url else None
        age_m = max(0, (now.year - dt_url.year) * 12 + (now.month - dt_url.month)) if dt_url else None
        
        # 3. Évaluation REFIL et Cadastre avec garde-fous
        refil_match = enricher.match_refil_semantics(
            f"{title_str} {clean_desc}",
            commune=com_final,
            quartier=quartier_final
        )
        lot_num = enricher.extract_cadastre_lot_reference(f"{title_str} {clean_desc}")
        
        if refil_match:
            refil_matches_count += 1
            score = 85 if refil_match.get("type") in ("IMMEUBLE", "ENSEMBLE IMMOBILIER") else 75
            addr = f" - {refil_match['address']}" if refil_match.get("address") else ""
            lvl = "IMMEUBLE_REFIL"
            detail = f"REFIL DITTT : {refil_match['full_name']}{addr}"
            lat_p = refil_match["lat"]
            lon_p = refil_match["lon"]
        elif lot_num:
            score = 70
            lvl = "LOT_CADASTRE"
            detail = f"Lot n° {lot_num} identifié dans le descriptif"
            lat_p = None
            lon_p = None
        else:
            score = 40
            lvl = "QUARTIER_DEFAULT"
            q_label = quartier_final or com_final or "Secteur"
            detail = f"Approximation centroïde secteur : {q_label}"
            lat_p = None
            lon_p = None
            
        updates.append((
            com_final,
            quartier_final,
            lat_p,
            lon_p,
            score,
            lvl,
            detail,
            dt_url.isoformat() if dt_url else None,
            age_m,
            lid
        ))
        
    print(f"Mise à jour en base de {len(updates)} annonces...")
    
    con.executemany("""
        UPDATE listings
        SET commune = ?,
            quartier = ?,
            lat_precise = ?,
            lon_precise = ?,
            precision_score = ?,
            precision_level = ?,
            precision_detail = ?,
            photo_date_taken = ?,
            photo_age_months = ?
        WHERE id = ?;
    """, updates)
    
    con.commit()
    
    print("\n" + "=" * 70)
    print("RÉSULTATS DE L'AUDIT & DE LA MIGRATION")
    print("=" * 70)
    print(f"Total annonces traitées : {total}")
    print(f"Quartiers mis à jour / résolus : {updated_quartiers_count}")
    print(f"Annonces PK (PK4/PK6/PK7) corrigées : {pk_listings_fixed}")
    print(f"Repères REFIL certifiés (avec validation de quartier) : {refil_matches_count}")
    
    # Vérification ciblée sur l'annonce Charlotte VIARD
    viard = con.execute("""
        SELECT id, title, commune, quartier, lat_precise, lon_precise, precision_score, precision_level, precision_detail
        FROM listings
        WHERE id = 'immobilier.nc_515124';
    """).fetchone()
    
    print("\n--- VÉRIFICATION ANNONCE CIBLE (CHARLOTTE VIARD) ---")
    print(f"ID               : {viard[0]}")
    print(f"Titre            : {viard[1]}")
    print(f"Commune          : {viard[2]}")
    print(f"Quartier assigné : {viard[3]}")
    print(f"Score précision  : {viard[6]}% ({viard[7]})")
    print(f"Détail précision : {viard[8]}")
    
    # Vérification de l'absence totale de biens PK6/PK7 avec REFIL à Faubourg / Ducos
    check_anomalies = con.execute("""
        SELECT count(*)
        FROM listings
        WHERE (title ILIKE '%P.k. 6%' OR title ILIKE '%PK6%' OR title ILIKE '%P.k. 7%' OR title ILIKE '%PK7%')
          AND precision_detail ILIKE '%BELLE VUE%';
    """).fetchone()[0]
    
    print(f"\nAnomalies 'BELLE VUE' sur PK résiduelles : {check_anomalies} (Attendu: 0)")
    
    con.close()
    print("\nMigration et audit terminés avec succès !")

if __name__ == "__main__":
    run_migration_and_audit()
