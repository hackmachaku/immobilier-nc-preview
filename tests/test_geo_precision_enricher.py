"""
Tests unitaires pour le moteur de géolocalisation haute précision et datation des photos (GeoPrecisionEnricher).
Vérifie l'extraction des dates depuis les noms de fichiers et URLs, la conversion DMS -> Décimal,
la détection sémantique REFIL, l'extraction de lots cadastraux et la hiérarchie des scores de précision.
"""

from datetime import datetime, timezone
import pytest
from src.analysis.geo_precision_enricher import (
    GeoPrecisionEnricher,
    parse_photo_date_from_text,
    dms_to_decimal,
    is_valid_nc_coords,
    compute_photo_age_months,
)


class TestPhotoDateParsing:
    def test_iphone_photo_format(self):
        url = "https://immonc.com/photos/Photo-12-01-2024-15-24-48.jpg"
        dt = parse_photo_date_from_text(url)
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 12
        assert dt.hour == 15
        assert dt.minute == 24

    def test_android_img_format(self):
        url = "https://immonc.com/photos/IMG20230514120000.jpg"
        dt = parse_photo_date_from_text(url)
        assert dt is not None
        assert dt.year == 2023
        assert dt.month == 5
        assert dt.day == 14

    def test_android_wp_format(self):
        url = "https://immonc.com/photos/WP_20220513_102902.jpg"
        dt = parse_photo_date_from_text(url)
        assert dt is not None
        assert dt.year == 2022
        assert dt.month == 5
        assert dt.day == 13

    def test_bienmeloger_cdn_timestamp(self):
        url = "https://www.bienmeloger.nc/media/004/87681938-5717-4e2a-af6f-c75775aedb6c-202609021612.jpeg"
        dt = parse_photo_date_from_text(url)
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 9
        assert dt.day == 2
        assert dt.hour == 16
        assert dt.minute == 12

    def test_immobilier_nc_folder_format(self):
        url = "https://gestion.immobilier.nc/photos.immobilier.nc/2025/6/o_1iunbao94sg7mmv1s8bqbrbkn1j.jpg"
        dt = parse_photo_date_from_text(url)
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 6
        assert dt.day == 1

    def test_immonc_unix_timestamp(self):
        url = "https://immonc.com/photos/photos_big/0_1743111123_P20250326151036.jpg"
        dt = parse_photo_date_from_text(url)
        assert dt is not None
        assert 2024 <= dt.year <= 2026

    def test_invalid_urls(self):
        assert parse_photo_date_from_text(None) is None
        assert parse_photo_date_from_text("") is None
        assert parse_photo_date_from_text("https://images.unsplash.com/photo-1580587771525.jpg") is None


class TestGeoCoordinatesAndEXIF:
    def test_dms_to_decimal_south_east(self):
        # 22° 16' 47.47" S -> -22.279853
        # 166° 27' 3.40" E -> 166.450944
        lat = dms_to_decimal((22, 16, 47.47), 'S')
        lon = dms_to_decimal((166, 27, 3.40), 'E')
        assert pytest.approx(lat, abs=1e-4) == -22.27985
        assert pytest.approx(lon, abs=1e-4) == 166.45094
        assert is_valid_nc_coords(lat, lon) is True

    def test_invalid_coords_outside_nc(self):
        # Paris coordinates
        assert is_valid_nc_coords(48.8566, 2.3522) is False
        # Null coordinates
        assert is_valid_nc_coords(None, None) is False
        assert is_valid_nc_coords(0.0, 0.0) is False


class TestPhotoAgeCalculation:
    def test_photo_age_recent(self):
        ref = datetime(2026, 9, 15, tzinfo=timezone.utc)
        photo = datetime(2026, 8, 1, tzinfo=timezone.utc)
        age = compute_photo_age_months(photo, ref)
        assert age == 1

    def test_photo_age_three_years(self):
        ref = datetime(2026, 9, 15, tzinfo=timezone.utc)
        photo = datetime(2023, 3, 1, tzinfo=timezone.utc)
        age = compute_photo_age_months(photo, ref)
        assert age >= 42  # ~3.5 years = 42 months


class TestRefilAndCadastreMatching:
    @pytest.fixture(scope="class")
    def enricher(self):
        return GeoPrecisionEnricher()

    def test_refil_dictionary_loaded(self, enricher):
        assert enricher.is_refil_loaded is True
        assert len(enricher.refil_entries) >= 5000

    def test_match_known_residence_bao(self, enricher):
        match = enricher.match_refil_semantics("Appartement dans la résidence BAO à Panda Dumbéa")
        assert match is not None
        assert "BAO" in match["nom"].upper()
        assert match["lat"] is not None
        assert match["lon"] is not None
        assert is_valid_nc_coords(match["lat"], match["lon"]) is True

    def test_match_known_residence_isle_de_france(self, enricher):
        match = enricher.match_refil_semantics("Superbe appartement dans l'immeuble Isle de France Anse Vata")
        assert match is not None
        assert "ISLE DE FRANCE" in match["nom"].upper()
        assert match["lat"] is not None
        assert match["lon"] is not None

    def test_extract_cadastre_lot_reference(self, enricher):
        lot = enricher.extract_cadastre_lot_reference("Très beau terrain plat viabilisé Lot n° 142 dans le lotissement")
        assert lot == "142"

        lot2 = enricher.extract_cadastre_lot_reference("Villa sur parcelle 35 section Magenta")
        assert lot2 == "35"

    def test_enrich_listing_dict_hierarchy(self, enricher):
        # Listing 1: With REFIL match
        item_refil = {
            "id": "test_1",
            "title": "F3 Résidence BAO",
            "description": "Appartement de standing",
            "image_url": "https://www.bienmeloger.nc/media/004/test-202609021612.jpeg",
            "commune": "Dumbéa",
            "quartier": "Panda"
        }
        enriched = enricher.enrich_listing_dict(item_refil)
        assert enriched["precision_score"] in (75, 85)
        assert enriched["precision_level"] == "IMMEUBLE_REFIL"
        assert enriched["lat_precise"] is not None
        assert enriched["photo_date_taken"] is not None
        assert "REFIL" in enriched["precision_badge"]

        # Listing 2: Fallback quartier
        item_default = {
            "id": "test_2",
            "title": "Maison vue colline",
            "description": "Sans nom particulier",
            "commune": "Nouméa",
            "quartier": "Anse Vata",
            "lat": -22.302,
            "lon": 166.444
        }
        enriched2 = enricher.enrich_listing_dict(item_default)
        assert enriched2["precision_score"] == 40
        assert enriched2["precision_level"] == "QUARTIER_DEFAULT"
        assert "Secteur" in enriched2["precision_badge"]
