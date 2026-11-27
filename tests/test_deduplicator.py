"""
Tests unitaires pour le moteur de dédoublonnage multi-portails & détection d'écarts de prix.
"""

from src.analysis.deduplicator import detect_and_annotate_duplicates


def _make_listing(**overrides):
    base = {
        "id": "src_1",
        "source": "immonc",
        "sourceUrl": "https://www.immonc.com/annonce/1",
        "title": "Villa F4 à Nouméa (Ouémo)",
        "description": (
            "Magnifique villa F4 rénovée avec goût située au calme dans le quartier prisé de Ouémo. "
            "Elle offre un spacieux séjour lumineux ouvrant sur une grande varangue couverte en kohu, "
            "une cuisine équipée avec îlot central, trois chambres climatisées avec placards, "
            "deux salles d'eau modernes, piscine au sel et carport double sur terrain plat arboré."
        ),
        "transactionType": "VENTE",
        "commune": "Nouméa",
        "quartier": "Ouémo",
        "category": "maison_villa",
        "rooms": 4,
        "currentPrice": 45_000_000,
        "surfaceHabitable": 135.0,
        "surfaceTerrain": 800.0,
        "agencyName": "Agence A",
        "agencyPhone": "+687 28.11.11",
        "precisionScore": 85,
        "images": ["https://example.com/a1.jpg", "https://example.com/a2.jpg"],
        "isDefaultImage": False,
        "etage": None,
    }
    base.update(overrides)
    return base


def test_cross_portal_duplicate_and_price_spread():
    a = _make_listing(
        id="immonc_100",
        source="immonc",
        agencyName="Agence A",
        currentPrice=45_000_000,
        images=["https://example.com/1.jpg", "https://example.com/2.jpg", "https://example.com/3.jpg"],
    )
    b = _make_listing(
        id="bienmeloger.nc_200",
        source="bienmeloger.nc",
        agencyName="Agence B",
        currentPrice=46_500_000,
        images=["https://example.com/b1.jpg"],
    )
    res = detect_and_annotate_duplicates([a, b])
    assert len(res) == 2
    by_id = {x["id"]: x for x in res}

    assert by_id["immonc_100"]["isDuplicateSecondary"] is False
    assert by_id["bienmeloger.nc_200"]["isDuplicateSecondary"] is True
    assert by_id["immonc_100"]["duplicateCount"] == 2
    assert by_id["immonc_100"]["duplicateGroupId"] == by_id["bienmeloger.nc_200"]["duplicateGroupId"]
    assert by_id["immonc_100"]["minPriceAcrossPortals"] == 45_000_000
    assert by_id["immonc_100"]["maxPriceAcrossPortals"] == 46_500_000
    assert by_id["immonc_100"]["priceSpreadXpf"] == 1_500_000
    assert len(by_id["immonc_100"]["multiPortals"]) == 2
    assert by_id["immonc_100"]["multiPortals"][0]["price"] == 45_000_000


def test_different_lots_never_merged():
    a = _make_listing(
        id="imm_1",
        title="Terrain plat Lot 12 à Païta",
        description=(
            "Beau terrain plat viabilisé Lot n° 12 dans le nouveau lotissement calme de Païta "
            "avec vue dégagée montagne, eau et électricité en bordure, accès goudronné facile "
            "proche des écoles et des commerces du village de Païta."
        ),
        category="terrain",
        commune="Païta",
        quartier="Mont Mou",
        rooms=None,
        surfaceHabitable=0.0,
        surfaceTerrain=1000.0,
        currentPrice=12_000_000,
    )
    b = _make_listing(
        id="imm_2",
        title="Terrain plat Lot 15 à Païta",
        description=(
            "Beau terrain plat viabilisé Lot n° 15 dans le nouveau lotissement calme de Païta "
            "avec vue dégagée montagne, eau et électricité en bordure, accès goudronné facile "
            "proche des écoles et des commerces du village de Païta."
        ),
        category="terrain",
        commune="Païta",
        quartier="Mont Mou",
        rooms=None,
        surfaceHabitable=0.0,
        surfaceTerrain=1000.0,
        currentPrice=12_000_000,
    )
    res = detect_and_annotate_duplicates([a, b])
    assert all(x["duplicateCount"] == 1 for x in res)
    assert all(x["isDuplicateSecondary"] is False for x in res)


def test_different_floors_never_merged():
    a = _make_listing(id="ap_1", category="appartement", rooms=3, etage="1er étage", currentPrice=28_000_000, surfaceHabitable=78.0, surfaceTerrain=0.0)
    b = _make_listing(id="ap_2", category="appartement", rooms=3, etage="3ème étage", currentPrice=28_000_000, surfaceHabitable=78.0, surfaceTerrain=0.0)
    res = detect_and_annotate_duplicates([a, b])
    assert all(x["duplicateCount"] == 1 for x in res)


def test_synthetic_descriptions_different_agencies_not_merged():
    a = _make_listing(
        id="syn_1",
        description="Annonce Appartement F2 à Nouméa. Contact: Agence Alpha.",
        agencyName="Agence Alpha",
        images=["https://example.com/x.jpg"],
        surfaceHabitable=0.0,
        surfaceTerrain=0.0,
    )
    b = _make_listing(
        id="syn_2",
        description="Annonce Appartement F2 à Nouméa. Contact: Agence Beta.",
        agencyName="Agence Beta",
        images=["https://example.com/y.jpg"],
        surfaceHabitable=0.0,
        surfaceTerrain=0.0,
    )
    res = detect_and_annotate_duplicates([a, b])
    assert all(x["duplicateCount"] == 1 for x in res)


def test_different_quartiers_not_merged():
    a = _make_listing(id="q_1", quartier="Anse Vata")
    b = _make_listing(id="q_2", quartier="Magenta")
    res = detect_and_annotate_duplicates([a, b])
    assert all(x["duplicateCount"] == 1 for x in res)
