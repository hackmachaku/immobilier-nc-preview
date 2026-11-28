import pytest
from src.ingestion.detail_parser import parse_immonc_detail_html
from src.ingestion.live_scraper import LiveNCScraper
from src.models.listing import RawListing, PropertyType, TransactionType
from src.processing.cleaner import ListingCleaner
from src.storage.database import PropertyDatabase
from src.utils.completeness_auditor import CompletenessAuditor


SAMPLE_IMMONC_HTML = """
<!DOCTYPE html>
<html>
<head><title>Appartement F2 à vendre à Vallée de Tina, Nouméa | ImmoNC</title></head>
<body>
  <div class="listing-details-card">
    <div class="detailbox">
      <h3>Général</h3>
      <p>Type de bien : Appartement</p>
      <p>Nombre de pièces : 2</p>
      <p>Nombre de chambres : 1</p>
      <p>Étage : 4ème</p>
      <p>Ascenseur : Oui</p>
    </div>
    <div class="detailbox">
      <h3>Surfaces</h3>
      <p>Surface habitable : 44 m²</p>
      <p>Surface terrasse : 17 m²</p>
    </div>
    <div class="detailbox">
      <h3>Finances</h3>
      <p>Charges de copropriété : 12 000 F CFP/m</p>
      <p>Prix de vente : 17 900 000 F CFP</p>
    </div>
    <div class="detailbox">
      <h3>Annexe</h3>
      <p>Carport : 1</p>
    </div>
  </div>

  <div class="listing-description-card">
    <h2>Descriptif du bien</h2>
    <div class="pera">
      <p>À VENDRE – F2 DERNIER ÉTAGE AVEC VUE DÉGAGÉE – MAGENTA / VALLÉE DE TINA</p>
      <p>Situé au dernier étage d'une résidence sécurisée, découvrez ce bel appartement de bon standing, idéalement agencé et offrant une agréable vue dégagée.</p>
      <p>Il se compose de :</p>
      <p>✅ Une cuisine équipée ouverte sur le séjour</p>
      <p>✅ Une chambre climatisée avec placard intégré</p>
      <p>✅ Une large terrasse idéale pour profiter de l’extérieur</p>
      <p>✅ Un espace buanderie discret sur la terrasse</p>
      <p>✅ Une place de parking couverte sécurisée</p>
      <p>📞 Contactez Florian au 75.02.30 pour organiser une visite !</p>
    </div>
  </div>
</body>
</html>
"""


def test_detail_parser_on_sample_html():
    parsed = parse_immonc_detail_html(SAMPLE_IMMONC_HTML)
    assert parsed["surface_habitable_m2"] == 44.0
    assert parsed["surface_terrasse_m2"] == 17.0
    assert parsed["charges_mensuelles_xpf"] == 12000
    assert parsed["etage"] == "4"
    assert parsed["has_elevator"] is True
    assert parsed["direct_negotiator"] == "Florian"
    assert parsed["direct_phone"] == "+687 75.02.30"
    assert not parsed["full_description"].endswith("...")
    assert "â€“" not in parsed["full_description"]
    assert "✅" in parsed["full_description"]
    assert "📞 Contactez Florian" in parsed["full_description"]


def test_cleaner_preserves_extracted_specs():
    raw = RawListing(
        source="immonc",
        source_id="test_f2",
        url="https://www.immonc.com/annonce/vente/appartement-f2/noumea-vallee-de-tina-5297448",
        title="Appartement F2 à vendre à Vallée de Tina, Nouméa",
        description="Situé au dernier étage d'une résidence sécurisée. 📞 Contactez Florian au 75.02.30",
        raw_price="17 900 000 F CFP",
        raw_surface="44.0",
        raw_rooms="2",
        raw_location="Vallée de Tina, Nouméa",
        transaction_type_declared="Vente",
        property_type_declared="Appartement",
        raw_charges="12000",
        raw_terrace="17.0",
        raw_floor="4",
        direct_negotiator="Florian",
        direct_phone="+687 75.02.30",
    )

    cleaner = ListingCleaner()
    cleaned = cleaner.clean(raw)

    assert cleaned.surface_habitable_m2 == 44.0  # Heuristic 50 m² must NOT override 44.0 m²
    assert cleaned.surface_terrasse_m2 == 17.0
    assert cleaned.charges_mensuelles_xpf == 12000
    assert cleaned.etage == "4ème étage"
    assert cleaned.direct_negotiator == "Florian"
    assert cleaned.direct_phone == "+687 75.02.30"


def test_database_persistence_5297448():
    db = PropertyDatabase(read_only=True)
    df = db.query("SELECT * FROM listings WHERE id = 'immonc_5297448'")
    assert not df.empty, "L'annonce immonc_5297448 doit être présente en DuckDB"
    row = df.iloc[0].to_dict()

    assert row["surface_habitable_m2"] == 44.0
    assert row["surface_terrasse_m2"] == 17.0
    assert row["charges_mensuelles_xpf"] == 12000
    assert str(row["etage"]).strip() in ("4", "4ème", "4ème étage")
    assert bool(row["has_elevator"]) is True
    assert row["direct_negotiator"] == "Florian"
    assert row["direct_phone"] == "+687 75.02.30"
    assert not str(row["description"]).strip().endswith("...")
    assert len(str(row["description"])) >= 500


def test_completeness_auditor():
    auditor = CompletenessAuditor()

    # Test texte tronqué
    truncated_res = auditor.audit_text("Bel appartement F2 situé au dernier étage... une lar...")
    assert truncated_res["is_truncated"] is True

    # Test texte sain
    healthy_res = auditor.audit_text("Bel appartement F2 de 44 m² avec terrasse de 17 m². Contactez Florian au 75.02.30.")
    assert healthy_res["is_truncated"] is False
    assert healthy_res["has_mojibake"] is False

    # Test mojibake
    mojibake_res = auditor.audit_text("Appartement rÃ©cent avec vue dÃ©gagÃ©e â€“ terrasse")
    assert mojibake_res["has_mojibake"] is True
