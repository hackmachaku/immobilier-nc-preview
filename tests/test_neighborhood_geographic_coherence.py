"""
Tests de cohérence géographique, détection NLP des quartiers et garde-fous REFIL/Cadastre.
Vérifie en particulier la résolution de l'annonce Charlotte VIARD au PK6 (et non à Ducos)
et l'absence totale de dérives inter-quartiers.
"""

import json
import re
from pathlib import Path
import pytest

from src.models.listing import Commune
from src.reference.geo import GeoReferential
from src.analysis.cadastre_enrichment import CadastreSpatialIndex
from src.analysis.geo_precision_enricher import GeoPrecisionEnricher


@pytest.fixture(scope="module")
def geo_ref():
    return GeoReferential()


@pytest.fixture(scope="module")
def cadastre_idx():
    idx = CadastreSpatialIndex()
    idx.load()
    return idx


@pytest.fixture(scope="module")
def geo_enricher():
    return GeoPrecisionEnricher()


def test_georeferential_title_nlp_extraction(geo_ref):
    """Vérifie la détection NLP précise des quartiers dans les titres."""
    test_cases = [
        ("Maison F5 à Nouméa (P.k. 6)", Commune.NOUMEA, "PK6"),
        ("Appartement F4 à Nouméa (P.k. 7)", Commune.NOUMEA, "PK7"),
        ("Maison F4 à Nouméa (P.k. 4)", Commune.NOUMEA, "PK4"),
        ("Location Appartement F2 à Portes de fer", Commune.NOUMEA, "Portes de Fer"),
        ("Appartement meublé STUDIO à Nouméa (Orphelinat)", Commune.NOUMEA, "Orphelinat"),
        ("Appartement F3 à Nouméa (Quartier latin)", Commune.NOUMEA, "Quartier Latin"),
        ("Maison F4 à Nouméa (P.k. 7) contactez notre agence à l'Anse Vata", Commune.NOUMEA, "PK7"),
        ("Appartement F3 à Nouméa (Vallée des colons)", Commune.NOUMEA, "Vallée des Colons"),
        ("Villa F5 à Dumbéa (Koutio)", Commune.DUMBEA, "Koutio"),
        ("Maison F4 à Mont-dore (Yahoué)", Commune.MONT_DORE, "Yahoué"),
    ]

    for title, exp_com, exp_q in test_cases:
        com, q, _ = geo_ref.find_location(title)
        assert com == exp_com, f"Erreur commune pour '{title}': obtenu {com}, attendu {exp_com}"
        assert q == exp_q, f"Erreur quartier pour '{title}': obtenu '{q}', attendu '{exp_q}'"


def test_charlotte_viard_pk6_coherence(cadastre_idx):
    """
    Test spécifique de l'annonce Charlotte VIARD :
    'Maison F5 à Nouméa (P.k. 6)' doit être géolocalisée au PK6 et JAMAIS à Ducos.
    """
    item_id = "immobilier.nc_515124"
    title = "Maison F5 à Nouméa (P.k. 6)"
    desc = "Charlotte VIARD vous propose cette magnifique villa..."
    
    res = cadastre_idx.resolve_listing_geography(
        item_id=item_id,
        commune="NOUMEA",
        quartier=None,  # Simule le cas non résolu en base
        title=title,
        description=desc
    )

    lat = res["lat"]
    lon = res["lon"]

    # Bornes du secteur PK6 (Route du Sud / Jacques Iékawé / Col de Tina)
    assert -22.248 <= lat <= -22.225, f"La latitude {lat} n'est pas dans la zone PK6"
    assert 166.458 <= lon <= 166.485, f"La longitude {lon} n'est pas dans la zone PK6"

    # Vérification d'exclusion stricte de la zone industrielle de Ducos
    is_in_ducos = (-22.260 <= lat <= -22.235) and (166.420 <= lon <= 166.445)
    assert not is_in_ducos, f"L'annonce PK6 est erronément localisée à Ducos ({lat}, {lon})"
    assert res["match_type"] == "QUARTIER_PARCEL"


def test_refil_cross_neighborhood_guardrail(geo_enricher):
    """
    Vérifie qu'un descriptif avec 'belle vue' pour un bien au PK7
    ne matche PAS l'immeuble REFIL 'BELLE VUE' situé au Faubourg Blanchot.
    """
    text = "Maison F4 à Nouméa (P.k. 7) avec piscine, vue dégagée et bel espace extérieur"
    match = geo_enricher.match_refil_semantics(text, commune="NOUMEA", quartier="PK7")
    
    # Doit être None car 'BELLE VUE' n'a pas de déclencheur 'résidence' et n'est pas au PK7
    assert match is None, f"Faux positif REFIL détecté : {match}"


def test_cadastre_pools_contain_new_neighborhoods(cadastre_idx):
    """Vérifie que les tables de parcelles contiennent bien les nouveaux quartiers de Nouméa."""
    noumea_pools = cadastre_idx.commune_quartier_parcels.get("NOUMEA", {})
    required_quartiers = ["pk6", "pk7", "pk4", "portes de fer", "orphelinat", "quartier latin"]
    
    for q in required_quartiers:
        assert q in noumea_pools, f"Quartier '{q}' absent des pools cadastre de Nouméa"
        assert len(noumea_pools[q]) >= 50, f"Trop peu de parcelles dans '{q}': {len(noumea_pools[q])}"


def test_no_pk_listings_in_ducos():
    """Vérifie dans data_listings.json qu'aucune annonce PK n'apparaît à Ducos."""
    data_path = Path(__file__).resolve().parent.parent / "data_listings.json"
    assert data_path.exists(), "data_listings.json n'existe pas"
    
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)["data"]

    pk_listings = [
        x for x in data
        if re.search(r"\b(?:p\.k\.|pk)\s*[467]\b", x.get("title", ""), re.IGNORECASE)
    ]
    assert len(pk_listings) > 0, "Aucune annonce PK trouvée dans le snapshot"

    ducos_bounds = (-22.260, -22.235, 166.420, 166.445)
    misplaced = [
        x for x in pk_listings
        if ducos_bounds[0] <= x["lat"] <= ducos_bounds[1] and ducos_bounds[2] <= x["lon"] <= ducos_bounds[3]
    ]

    assert len(misplaced) == 0, f"{len(misplaced)} annonces PK sont à Ducos : {[m['id'] for m in misplaced]}"
