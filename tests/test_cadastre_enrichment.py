"""
Tests unitaires pour le module d'enrichissement cadastral et foncier officiel NC.
Vérifie la conversion des contenances légales (ha, a, ca), le calcul des écarts de surface,
la gestion des copropriétés, et la recherche spatiale (Cadastre & REFIL).
"""

import pytest
from src.analysis.cadastre_enrichment import (
    parse_cadastre_area,
    calculate_surface_discrepancy,
    CadastreSpatialIndex,
    get_cadastre_index,
    enrich_listings,
)


class TestCadastreAreaParser:
    def test_standard_hac_format(self):
        # 0ha 13a 94ca = 0*10000 + 13*100 + 94 = 1394 m²
        assert parse_cadastre_area("0ha 13a 94ca") == 1394.0
        # 0ha 5a 15ca = 515 m²
        assert parse_cadastre_area("0ha 5a 15ca") == 515.0
        # 1ha 0a 0ca = 10000 m²
        assert parse_cadastre_area("1ha 0a 0ca") == 10000.0
        # 3ha 19a 70ca = 31970 m²
        assert parse_cadastre_area("3ha 19a 70ca") == 31970.0

    def test_partial_hac_format(self):
        assert parse_cadastre_area("15a 30ca") == 1530.0
        assert parse_cadastre_area("50ca") == 50.0
        assert parse_cadastre_area("2ha") == 20000.0

    def test_direct_numeric_format(self):
        assert parse_cadastre_area("1200 m²") == 1200.0
        assert parse_cadastre_area("850 m2") == 850.0
        assert parse_cadastre_area("1 500,50 m²") == 1500.50

    def test_invalid_or_empty_inputs(self):
        assert parse_cadastre_area(None) is None
        assert parse_cadastre_area("") is None
        assert parse_cadastre_area("inconnu") is None
        assert parse_cadastre_area("0ha 0a 0ca") is None


class TestSurfaceDiscrepancyCalculator:
    def test_conforme_tolerance_5_percent(self):
        res = calculate_surface_discrepancy(505, 500)
        assert res["surfaceEcartStatus"] == "CONFORME"
        assert res["isConforme"] is True
        assert res["surfaceEcartM2"] == 5.0
        assert res["surfaceEcartPct"] == 1.0

    def test_ecart_mineur_tolerance_15_percent(self):
        res = calculate_surface_discrepancy(550, 500)
        assert res["surfaceEcartStatus"] == "ECART_MINEUR"
        assert res["isConforme"] is True
        assert res["surfaceEcartM2"] == 50.0
        assert res["surfaceEcartPct"] == 10.0

    def test_ecart_majeur_over_15_percent(self):
        res = calculate_surface_discrepancy(750, 500)
        assert res["surfaceEcartStatus"] == "ECART_MAJEUR"
        assert res["isConforme"] is False
        assert res["surfaceEcartM2"] == 250.0
        assert res["surfaceEcartPct"] == 50.0

    def test_negative_discrepancy(self):
        # Surface déclarée inférieure à la contenance officielle
        res = calculate_surface_discrepancy(350, 500)
        assert res["surfaceEcartStatus"] == "ECART_MAJEUR"
        assert res["isConforme"] is False
        assert res["surfaceEcartM2"] == -150.0
        assert res["surfaceEcartPct"] == -30.0

    def test_missing_or_zero_values(self):
        res = calculate_surface_discrepancy(None, 500)
        assert res["surfaceEcartStatus"] == "NON_EVALUABLE"
        assert res["isConforme"] is None

        res2 = calculate_surface_discrepancy(500, 0)
        assert res2["surfaceEcartStatus"] == "NON_EVALUABLE"
        assert res2["isConforme"] is None


class TestCadastreSpatialEnrichment:
    @pytest.fixture(scope="class")
    def spatial_index(self):
        idx = get_cadastre_index()
        assert idx.is_loaded is True
        return idx

    def test_nearest_parcel_anse_vata(self, spatial_index):
        # Coordonnées proches d'une parcelle cadastrale active à Nouméa Anse Vata
        lat, lon = -22.30258, 166.44418
        parcel, dist = spatial_index.find_nearest_parcel(lat, lon, max_dist_m=120.0)
        assert parcel is not None
        assert "nic" in parcel
        assert "section" in parcel
        assert parcel["commune"] == "NOUMEA"
        assert dist is not None and dist <= 120.0

    def test_nearest_building_refil(self, spatial_index):
        # Immeuble REFIL dans le secteur Anse Vata / Val Plaisance
        lat, lon = -22.30258, 166.44418
        bldg, dist = spatial_index.find_nearest_building(lat, lon, max_dist_m=100.0)
        if bldg:
            assert "nom" in bldg
            assert "adresse" in bldg
            assert dist <= 100.0

    def test_enrich_apartment_copropriete(self, spatial_index):
        listing = {
            "id": "apt_test_01",
            "title": "Appartement F3 vue mer Anse Vata",
            "propertyType": "APPARTEMENT",
            "lat": -22.30258,
            "lon": 166.44418,
            "surface": 85,
        }
        enriched = spatial_index.enrich_listing(listing)

        assert enriched.get("cadastreNic") is not None
        assert enriched.get("cadastreCommune") == "NOUMEA"
        # En copropriété, le statut d'écart doit être COPROPRIETE et non une fausse alerte
        assert enriched.get("surfaceEcartStatus") == "COPROPRIETE"
        assert "Assiette foncière copropriété" in enriched.get("surfaceEcartLabel", "")
        assert enriched.get("isConforme") is True

    def test_enrich_villa_discrepancy(self, spatial_index):
        listing = {
            "id": "villa_test_02",
            "title": "Belle villa F5 avec piscine",
            "propertyType": "MAISON",
            "lat": -22.25700,
            "lon": 166.46300,
            "surfaceTerrain": 650,
        }
        enriched = spatial_index.enrich_listing(listing)

        assert enriched.get("cadastreNic") is not None
        assert enriched.get("surfaceEcartStatus") in ("CONFORME", "ECART_MINEUR", "ECART_MAJEUR", "NON_EVALUABLE")

    def test_enrich_without_coordinates(self, spatial_index):
        listing = {
            "id": "no_coords",
            "title": "Bien sans géolocalisation",
            "lat": None,
            "lon": None,
        }
        enriched = spatial_index.enrich_listing(listing)
        assert "cadastreNic" not in enriched

    def test_batch_enrichment(self):
        listings = [
            {"id": "1", "lat": -22.30258, "lon": 166.44418, "title": "Appt 1", "propertyType": "APPARTEMENT"},
            {"id": "2", "lat": None, "lon": None, "title": "Sans coord"},
        ]
        results = enrich_listings(listings)
        assert len(results) == 2
        assert results[0].get("cadastreNic") is not None
        assert "cadastreNic" not in results[1]

    def test_enrich_with_low_precision_score_does_not_assign_parcel(self, spatial_index):
        listing = {
            "id": "apt_approx_quartier",
            "title": "Studio au Quartier Latin",
            "propertyType": "APPARTEMENT",
            "commune": "Nouméa",
            "quartier": "Quartier Latin",
            "lat": -22.2764,
            "lon": 166.4443,
            "precisionScore": 40,
            "precisionLevel": "QUARTIER_DEFAULT",
        }
        enriched = spatial_index.enrich_listing(listing)
        assert enriched.get("isCadastreCertifie") is False
        assert enriched.get("cadastreNic") is None
        assert enriched.get("cadastreLot") is None
        assert enriched.get("refilNom") is None
        assert enriched.get("cadastreStatus") == "INDICATIF_QUARTIER"

