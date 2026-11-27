import json
import pytest
from src.ingestion.live_scraper import LiveNCScraper
from src.processing.cleaner import ListingCleaner
from src.reference.geo import GeoReferential
from src.models.listing import TransactionType, PropertyType, Commune


from src.storage.database import PropertyDatabase


@pytest.fixture
def scraper(tmp_path):
    return LiveNCScraper(db=PropertyDatabase(db_path=tmp_path / "test_immo.duckdb"))


@pytest.fixture
def cleaner():
    return ListingCleaner(GeoReferential())


def test_parse_immonc_item_vente_maison(scraper, cleaner):
    item = {
        "id": "5332137",
        "url": "https://www.immonc.com/annonce/vente/maison-f4/noumea-normandie-5332137",
        "title": "Maison F4",
        "location_raw": "Normandie",
        "price_xpf": 28000000,
        "description": "Au pont des français, normandie agréable maison f4 en bois avec dépendances terrain arboré avec piscine de 10ares26",
        "agency_name": "Acti Immobilier",
        "agency_email": "christelle@acti-immo.nc",
        "photos": [
            "https://immonc.com/photos/photos_medium/5332137_1789306845_1784239264VM81818original.jpg",
            "https://immonc.com/photos/photos_medium/5332137_1789306849_1784239289VM81826original.jpg"
        ],
        "deal_type": "vente",
    }

    raw = scraper.parse_immonc_item_to_raw_listing(item, deal_type="vente")
    assert raw is not None
    assert raw.source == "immonc"
    assert raw.source_id == "5332137"
    assert raw.transaction_type_declared == "Vente"
    assert raw.property_type_declared == "Maison"
    assert raw.raw_price == "28000000 F CFP"
    assert raw.agency_name == "Acti Immobilier"
    assert "piscine" in raw.facilities
    assert raw.image_url.startswith("https://immonc.com/photos/")
    assert len(json.loads(raw.images_json)) == 2

    cleaned = cleaner.clean(raw)
    assert cleaned.id == "immonc_5332137"
    assert cleaned.price_xpf == 28000000
    assert cleaned.transaction_type == TransactionType.VENTE
    assert cleaned.property_type == PropertyType.MAISON_VILLA
    assert cleaned.commune == Commune.NOUMEA
    assert cleaned.surface_terrain_m2 == 1026.0
    assert cleaned.has_pool is True
    assert cleaned.agency_name == "Acti Immobilier"


def test_parse_immonc_item_location_appartement_meuble(scraper, cleaner):
    item = {
        "id": "5332275",
        "url": "https://www.immonc.com/annonce/location/appartement-f2/noumea-val-plaisance-5332275",
        "title": "Appartement F2 meublé",
        "location_raw": "Val Plaisance",
        "price_xpf": 95000,
        "description": "Bel appartement F2 entièrement meublé et équipé à Val Plaisance avec clim et terrasse couverte de 15m2",
        "agency_name": "Max Immo",
        "agency_email": "locations@maximmo.nc",
        "photos": ["https://immonc.com/photos/photos_medium/0_1786656639_IMG7585.jpeg"],
        "deal_type": "location",
    }

    raw = scraper.parse_immonc_item_to_raw_listing(item, deal_type="location")
    assert raw is not None
    assert raw.source == "immonc"
    assert raw.source_id == "5332275"
    assert raw.transaction_type_declared == "Location"
    assert raw.property_type_declared == "Appartement"
    assert raw.raw_price == "95000 F CFP/mois"
    assert "meuble" in raw.facilities
    assert "climatisation" in raw.facilities

    cleaned = cleaner.clean(raw)
    assert cleaned.id == "immonc_5332275"
    assert cleaned.price_xpf == 95000
    assert cleaned.transaction_type == TransactionType.LOCATION
    assert cleaned.property_type == PropertyType.APPARTEMENT
    assert cleaned.commune == Commune.NOUMEA
    assert cleaned.quartier == "Val Plaisance"
    assert cleaned.is_furnished is True
    assert cleaned.has_air_conditioning is True


def test_parse_immonc_dock_commercial(scraper, cleaner):
    item = {
        "id": "5331999",
        "url": "https://www.immonc.com/annonce/vente/dock/noumea-ducos-5331999",
        "title": "Dock 350 m2 à Ducos",
        "location_raw": "Ducos",
        "price_xpf": 55000000,
        "description": "Grand dock de 350m2 d'activité avec mezzanine et bureau à Ducos",
        "agency_name": "Agence Générale",
        "agency_email": "contact@ag.nc",
        "photos": [],
        "deal_type": "vente",
    }

    raw = scraper.parse_immonc_item_to_raw_listing(item, deal_type="vente")
    assert raw is not None
    assert raw.property_type_declared == "Dock"

    cleaned = cleaner.clean(raw)
    assert cleaned.property_type == PropertyType.DOCK
    assert cleaned.commune == Commune.NOUMEA
    assert cleaned.quartier == "Ducos"
    assert cleaned.surface_habitable_m2 == 350.0
