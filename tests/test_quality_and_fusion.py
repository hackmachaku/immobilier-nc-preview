"""Tests unitaires couvrant les Lots A, B, C, D (Ingestion, Zéro-Perte, Fusion Multi-Sources, Quality Gate)."""

import pytest
from src.domain.agencies_directory import resolve_agency_name
from src.ingestion.live_scraper import _dedup_immonc_photos, _clean_html_and_cp1252, _normalize_phone_nc_simple
from src.processing.cleaner import (
    ListingCleaner,
    normalize_etage,
    parse_terrain_surface_m2,
    is_out_of_scope_listing,
)
from src.processing.description_structurer import structure_description, classify_bullet_or_sentence
from src.analysis.deduplicator import detect_and_annotate_duplicates
from src.quality.quality_gate import evaluate_listing_quality, run_quality_gate
from src.models.listing import RawListing, PropertyType, TransactionType


def test_agency_resolution_no_bien_immo_false_positive():
    assert resolve_agency_name("Bien", None, "") == "Bien"
    assert resolve_agency_name("-", None, "") == "Particulier NC"
    assert resolve_agency_name("Francis LE BOLE", "francislebole@gmail.com", "Agences s'abstenir") == "Francis LE BOLE"
    assert resolve_agency_name("William Valadon", None, "Colocation particulière") == "William Valadon"
    assert resolve_agency_name("Bien-Immo", None, "") == "Bien-Immo"
    assert resolve_agency_name(None, "contact@bien-immo.nc", "") == "Bien-Immo"


def test_immonc_photo_dedup_eliminates_triplication():
    urls = [
        "https://immonc.com/photos/photos_big/abc123.jpg",
        "https://www.immonc.com/photos/photos_big/abc123.jpg",
        "https://www.immonc.com/photos/photos_vignette/abc123.jpg",
        "https://www.immonc.com/photos/photos_medium/def456.jpg",
    ]
    deduped = _dedup_immonc_photos(urls)
    assert len(deduped) == 2
    assert all(u.startswith("https://www.immonc.com/photos/photos_big/") for u in deduped)


def test_terrain_surface_parser_fixes_ha_ares_and_decimals():
    assert parse_terrain_surface_m2("superficie d'environ 2ha 20 ares") == 22_000.0
    assert parse_terrain_surface_m2("terrain de 5 hectares et 60 ares") == 56_000.0
    assert parse_terrain_surface_m2("terrain divisible de 2ha29 en zone UR") == 22_900.0
    assert parse_terrain_surface_m2("parcelle de 4.99 ares") == 499.0
    assert parse_terrain_surface_m2("SUPERFICIE TOTALE DE 17a 64ca") == 1_764.0
    assert parse_terrain_surface_m2("Lot 203 - Surface 10A96") == 1_096.0
    assert parse_terrain_surface_m2("Propriété 17.5 Hectares") == 175_000.0


def test_furnished_no_immeuble_or_staging_false_positive():
    cleaner = ListingCleaner()
    assert cleaner.parse_furnished(None, "Immeuble de rapport", "Bel immeuble de 4 appartements au centre-ville", PropertyType.IMMEUBLE) is None
    assert cleaner.parse_furnished(None, "Appartement F3", "Découvrez le potentiel grâce à cette projection meublée par IA !", PropertyType.APPARTEMENT) is None
    assert cleaner.parse_furnished(None, "Appartement F2", "Loué entièrement meublé et équipé", PropertyType.APPARTEMENT) is True
    assert cleaner.parse_furnished(None, "Appartement F2", "Loué non meublé", PropertyType.APPARTEMENT) is False


def test_normalize_etage_canonical():
    assert normalize_etage("rdc") == "Rez-de-chaussée"
    assert normalize_etage("Rez-de-chaussée") == "Rez-de-chaussée"
    assert normalize_etage("1") == "1er étage"
    assert normalize_etage("premier étage") == "1er étage"
    assert normalize_etage("RDJ") == "Rez-de-jardin"
    assert normalize_etage("dernier étage") == "Dernier étage"


def test_out_of_scope_filtering():
    assert is_out_of_scope_listing("RECHERCHE PETIT LOCAL COMMERCIAL", "Budget max 30 000 F/mois") is True
    assert is_out_of_scope_listing("Vendre table de séjour", "Belle table en bois massif") is True
    assert is_out_of_scope_listing("Appartement à Montréal", "Quartier Plateau, Québec, Canada") is True
    assert is_out_of_scope_listing("Villa F4 à Nouméa", "Belle villa calme avec jardin") is False


def test_structurer_zero_loss_fallback_and_entree_classification():
    # Ne jamais retourner sections=[] sur une description monoligne avec 'Contact' en fin
    res = structure_description("Superbe villa F4 à Nouméa avec grande varangue de 25 m², piscine et vue dégagée sur les collines. Contactez-nous au 78.12.34.")
    assert len(res["sections"]) >= 1
    assert classify_bullet_or_sentence("Porte d'entrée blindée") != "conditions_modalites"
    assert classify_bullet_or_sentence("Ponton privatif de 8 m en galva") == "exterieurs"


def test_deduplicator_v2_anti_fp_crm_and_cross_source_fusion():
    # 1. Deux lots distincts avec mandats CRM différents sur immonc -> NE DOIVENT PAS fusionner
    a1 = {
        "id": "immonc_1", "source": "immonc", "sourceUrl": "https://www.immonc.com/1",
        "title": "Appartement F3", "description": "Bel appartement F3 moderne à Dumbéa-sur-Mer avec varangue et 2 parkings.",
        "transactionType": "LOCATION", "commune": "Dumbéa", "quartier": "Dumbéa-sur-Mer",
        "category": "appartement", "rooms": 3, "currentPrice": 120000, "surfaceHabitable": 70.0,
        "agencyName": "Agence A", "images": ["https://www.immonc.com/photos/photos_big/0_1700000000_LA2065_1.jpg"],
        "isDefaultImage": False, "precisionScore": 40,
    }
    a2 = {
        "id": "immonc_2", "source": "immonc", "sourceUrl": "https://www.immonc.com/2",
        "title": "Appartement F3", "description": "Bel appartement F3 moderne à Dumbéa-sur-Mer avec varangue et 2 parkings.",
        "transactionType": "LOCATION", "commune": "Dumbéa", "quartier": "Dumbéa-sur-Mer",
        "category": "appartement", "rooms": 3, "currentPrice": 125000, "surfaceHabitable": 70.0,
        "agencyName": "Agence A", "images": ["https://www.immonc.com/photos/photos_big/0_1700000000_LA3398_1.jpg"],
        "isDefaultImage": False, "precisionScore": 40,
    }
    res_fp = detect_and_annotate_duplicates([a1, a2])
    assert res_fp[0]["duplicateCount"] == 1 and res_fp[1]["duplicateCount"] == 1

    # 2. Vrai doublon inter-portails avec fusion non-destructive (immonc a complementaryDetails + GPS REFIL, bienmeloger a surface + charges + tel)
    b1 = {
        "id": "bienmeloger.nc_100", "source": "bienmeloger.nc", "sourceUrl": "https://www.immobilier.nc/details/100",
        "title": "Villa F4 Anse Vata", "description": "Magnifique villa F4 rénovée à l'Anse Vata avec grande varangue couverte, piscine au sel, cuisine équipée et 2 places couvertes.",
        "transactionType": "VENTE", "commune": "Nouméa", "quartier": "Anse Vata",
        "category": "maison_villa", "rooms": 4, "currentPrice": 58_000_000, "surfaceHabitable": 140.0,
        "surfaceTerrain": 600.0, "chargesMensuelles": 15000, "agencyName": "Chatelin Immobilier",
        "agencyPhone": "+687 28.98.88", "images": ["https://cdn/1.jpg", "https://cdn/2.jpg"], "isDefaultImage": False,
        "precisionScore": 40, "lat": -22.302, "lon": 166.444,
    }
    b2 = {
        "id": "immonc_200", "source": "immonc", "sourceUrl": "https://www.immonc.com/annonce/200",
        "title": "Villa F4 Anse Vata", "description": "Magnifique villa F4 rénovée à l'Anse Vata avec grande varangue couverte, piscine au sel, cuisine équipée et 2 places couvertes.",
        "transactionType": "VENTE", "commune": "Nouméa", "quartier": "Anse Vata",
        "category": "maison_villa", "rooms": 4, "currentPrice": 61_480_000, "surfaceHabitable": 0.0,
        "bathrooms": 2, "complementaryDetails": {"Cuisine": ["Cuisine équipée"]},
        "agencyName": "Chatelin Immobilier", "agencyPhone": "Non renseigné",
        "images": ["https://www.immonc.com/photos/photos_big/x1.jpg"], "isDefaultImage": False,
        "precisionScore": 85, "precisionLevel": "IMMEUBLE_REFIL", "lat": -22.3015, "lon": 166.4442,
        "latPrecise": -22.3015, "lonPrecise": 166.4442,
    }
    res_fuse = detect_and_annotate_duplicates([b1, b2])
    canon = [x for x in res_fuse if not x["isDuplicateSecondary"]][0]
    assert canon["duplicateCount"] == 2
    assert canon["priceSpreadXpf"] == 3_480_000
    assert canon["priceSpreadNote"] == "Écart compatible HT / TTC (TGC 6 %)"
    assert canon["surfaceHabitable"] == 140.0
    assert canon["bathrooms"] == 2
    assert canon["precisionScore"] == 85
    assert len(canon["enrichedFromPortals"]) == 1


def test_quality_gate_quarantines_p0_and_scores_clean_listing():
    good = {
        "id": "bienmeloger.nc_1", "source": "bienmeloger.nc", "title": "Appartement F3 Anse Vata",
        "description": "Très bel appartement F3 lumineux de 85 m² avec varangue de 20 m², climatisé, 2 parkings couverts.",
        "category": "appartement", "transactionType": "VENTE", "currentPrice": 32_000_000,
        "surfaceHabitable": 85.0, "rooms": 3, "bedrooms": 2, "quartier": "Anse Vata",
        "lat": -22.302, "lon": 166.444, "precisionScore": 85, "isDefaultImage": False,
        "agencyPhone": "+687 28.98.88", "structuredDescription": [{"key": "cadre_vie", "items": ["Bel F3"]}],
    }
    bad = {
        "id": "yatoo.nc_bad", "source": "yatoo.nc", "title": "Vendre table de séjour",
        "description": "Table en bois 6 personnes.", "category": "maison_villa",
        "transactionType": "LOCATION", "currentPrice": 25_000, "lat": -22.27, "lon": 166.44,
    }
    pub, quar, rep = run_quality_gate([good, bad])
    assert len(pub) == 1 and pub[0]["qualityGrade"] == "A"
    assert len(quar) == 1 and "HORS_PERIMETRE_RECHERCHE_OU_OBJET" in quar[0]["quarantineReasons"]
    assert rep["summary"]["publishedCount"] == 1
    assert rep["summary"]["quarantinedCount"] == 1


def test_auto_quality_control_runs_on_every_import(tmp_path):
    """Vérifie que IngestionPipeline.process_and_store exécute systématiquement le contrôle qualité post-import."""
    from src.storage.database import PropertyDatabase
    from src.ingestion.collector import IngestionPipeline
    from src.quality.post_import_controller import run_post_import_control

    db = PropertyDatabase(db_path=tmp_path / "test_auto_qc.duckdb")
    pipe = IngestionPipeline(db=db)

    raw_batch = [
        RawListing(
            source="immonc",
            source_id="9001",
            url="https://www.immonc.com/annonce/vente/appartement-f3/noumea-anse-vata-9001",
            title="Appartement F3 Anse Vata",
            description="Bel appartement F3 de 82 m² au premier étage avec deux salles d'eau, varangue 18 m² et vue mer. Contact : 78.12.34.",
            raw_price="34 000 000 F CFP",
            raw_surface="82",
            raw_rooms="F3",
            raw_location="Nouméa - Anse Vata",
            transaction_type_declared="Vente",
            property_type_declared="Appartement",
            agency_name="Chatelin Immobilier",
            image_url="https://immonc.com/photos/photos_big/photo1.jpg",
            images_json='["https://immonc.com/photos/photos_big/photo1.jpg", "https://www.immonc.com/photos/photos_vignette/photo1.jpg"]',
        ),
        RawListing(
            source="yatoo.nc",
            source_id="9002",
            url="https://yatoo.nc/annonces/ventes-immobilieres/9002",
            title="Vendre table de séjour",
            description="Table en bois massif 6 personnes.",
            raw_price="45 000 F CFP",
            raw_location="Nouméa",
        ),
    ]

    res = pipe.process_and_store(raw_batch, batch_tag="unit_auto_qc")
    # L'objet hors-périmètre est rejeté dès l'import
    assert res["stored_cleaned"] == 1
    assert res["rejected"] == 1
    # Le contrôle qualité automatique s'est exécuté systématiquement
    qc = res["quality_control"]
    assert qc is not None
    assert qc["gate_status"] in ("PASS", "WARN")
    assert qc["sanitization"]["sanitized"] == 1
    assert qc["ai_cache_updated"] == 1

    # Vérifier la déduplication photos, SDB=2, étage canonique et cache v4.1 en base
    row = db.query("SELECT bathrooms, etage, images_json FROM listings WHERE id = 'immonc_9001'").iloc[0]
    assert int(row["bathrooms"]) == 2
    assert row["etage"] == "1er étage"
    import json
    assert len(json.loads(row["images_json"])) == 1
    cache_cnt = int(db.query("SELECT COUNT(*) AS c FROM listing_ai_enrichment WHERE listing_id = 'immonc_9001'").iloc[0]["c"])
    assert cache_cnt == 1

    # Vérifier l'archivage du rapport horodaté sur run_post_import_control
    log_dir = tmp_path / "logs"
    qc2 = run_post_import_control(db=db, batch_tag="unit_full", run_geo=False, rebuild_ai_cache=True, run_canary=False, save_report=True, log_dir=log_dir)
    assert (log_dir / "quality_gate_last.json").exists()
    assert (log_dir / "quality_gate_history.jsonl").exists()
    assert qc2["summary"]["publishedCount"] == 1

