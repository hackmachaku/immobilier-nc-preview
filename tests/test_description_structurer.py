"""
Tests unitaires pour le module de structuration sémantique des descriptions.
Valide l'extraction nominative des contacts directs, le tri en 7 rubriques immuables,
la décomposition intra-ligne multi-attributs, le moteur universel de téléphonie NC
et la segmentation étanche de fin d'annonce (Footer Boundary).
"""

import pytest
from src.processing.description_structurer import (
    extract_direct_contacts,
    clean_line_typography,
    classify_bullet_or_sentence,
    structure_description,
    normalize_nc_phone,
    split_compound_line,
)


def test_universal_nc_phone_normalization():
    # Format triplets NC (ex: Chatelin Immobilier 289.888)
    p1 = normalize_nc_phone("289.888")
    assert p1 is not None
    assert p1["digits"] == "289888"
    assert p1["formatted"] == "+687 289.888"
    assert p1["is_mobile"] is False
    assert p1["whatsapp"] is None

    # Format paires fixe Nouméa
    p2 = normalize_nc_phone("28.98.88")
    assert p2 is not None
    assert p2["digits"] == "289888"
    assert p2["formatted"] == "+687 28.98.88"
    assert p2["is_mobile"] is False

    # Format mobile avec WhatsApp
    p3 = normalize_nc_phone("85.46.55")
    assert p3 is not None
    assert p3["digits"] == "854655"
    assert p3["formatted"] == "+687 85.46.55"
    assert p3["is_mobile"] is True
    assert p3["whatsapp"] == "687854655"

    # Format international complet
    p4 = normalize_nc_phone("+687 79 40 01")
    assert p4 is not None
    assert p4["digits"] == "794001"
    assert p4["is_mobile"] is True
    assert p4["whatsapp"] == "687794001"

    # Format continu (ex: Open Immobilier 252421)
    p5 = normalize_nc_phone("252421")
    assert p5 is not None
    assert p5["digits"] == "252421"
    assert p5["is_mobile"] is False


def test_split_compound_line():
    line = "83 m² habitables • Terrasse ~40 m² • Vue mer • 2 chambres • 2 salles d’eau • Grand dressing • 2 stationnements couverts • Cellier privatif"
    parts = split_compound_line(line)
    assert len(parts) == 8
    assert parts[0] == "83 m² habitables"
    assert parts[1] == "Terrasse ~40 m²"
    assert parts[2] == "Vue mer"
    assert parts[3] == "2 chambres"
    assert parts[4] == "2 salles d’eau"
    assert parts[5] == "Grand dressing"
    assert parts[6] == "2 stationnements couverts"
    assert parts[7] == "Cellier privatif"

    # Ligne simple sans séparateur
    simple = "Beau séjour lumineux donnant sur terrasse"
    assert split_compound_line(simple) == [simple]


def test_extract_direct_contacts_multiple_agents():
    raw_text = (
        "Idéal pour une famille ! Visites et négociations avec Tim au 85.46.55 "
        "ou Séverine au 90.57.97. Agence Immobilière NC."
    )
    contacts = extract_direct_contacts(raw_text)
    assert len(contacts) == 2

    names = {c["name"] for c in contacts}
    assert "Tim" in names
    assert "Séverine" in names

    phones = {c["raw_phone"] for c in contacts}
    assert "854655" in phones
    assert "905797" in phones

    tim = next(c for c in contacts if c["name"] == "Tim")
    assert tim["phone"] == "+687 85.46.55"
    assert tim["whatsapp"] == "687854655"


def test_clean_line_typography():
    assert clean_line_typography("✅ Grande terrasse couverte !!!!!!!") == "Grande terrasse couverte !"
    assert clean_line_typography("✨✨ Cuisine équipée et aménagée ???") == "Cuisine équipée et aménagée ?"
    assert clean_line_typography("• 2 chambres avec placards.....") == "2 chambres avec placards..."
    assert clean_line_typography("👉   Place de parking sécurisée   ") == "Place de parking sécurisée"
    assert clean_line_typography("❗️❗️ COUP DE COEUR ❗️❗️") == "COUP DE COEUR"


def test_classify_bullet_or_sentence():
    # Cadre de vie
    assert classify_bullet_or_sentence("Dans une résidence calme et sécurisée de standing") == "cadre_vie"
    assert classify_bullet_or_sentence("Idéalement situé au cœur de la Vallée des Colons") == "cadre_vie"

    # Services résidence
    assert classify_bullet_or_sentence("Salle de fitness et espace de musculation") == "services_residence"
    assert classify_bullet_or_sentence("2 restaurants et boutiques au pied de l'immeuble") == "services_residence"
    assert classify_bullet_or_sentence("Gardiennage et vidéosurveillance 24h/24") == "services_residence"

    # Pièces de vie
    assert classify_bullet_or_sentence("Un grand séjour lumineux avec cuisine ouverte équipée") == "pieces_de_vie"
    assert classify_bullet_or_sentence("Cuisine moderne avec îlot central et buanderie attenante") == "pieces_de_vie"

    # Espace nuit
    assert classify_bullet_or_sentence("3 chambres climatisées avec placards intégrés") == "espace_nuit"
    assert classify_bullet_or_sentence("Une salle d'eau moderne avec douche à l'italienne et WC séparé") == "espace_nuit"
    assert classify_bullet_or_sentence("2 salles d’eau") == "espace_nuit"

    # Extérieurs
    assert classify_bullet_or_sentence("Superbe terrasse couverte de 30 m² avec vue mer imprenable") == "exterieurs"
    assert classify_bullet_or_sentence("Jardin arboré et clôturé avec piscine sans vis-à-vis") == "exterieurs"

    # Stationnement & Annexes
    assert classify_bullet_or_sentence("Deux places de parking sécurisées en sous-sol et un cellier") == "stationnement_annexes"
    assert classify_bullet_or_sentence("Garage fermé et carport pour 2 véhicules") == "stationnement_annexes"

    # Conditions & Modalités
    assert classify_bullet_or_sentence("Loyer mensuel : 180 000 F XPF charges comprises") == "conditions_modalites"
    assert classify_bullet_or_sentence("Dépôt de garantie : un mois de loyer hors charges") == "conditions_modalites"
    assert classify_bullet_or_sentence("Prix : 49 000 000 F CFP") == "conditions_modalites"


def test_anse_vata_la_voile_du_rocher_case_study():
    """Test sur le cas réel de l'annonce F3 Anse Vata (Chatelin Immobilier / Laurent NGUYEN)."""
    raw_desc = """ANSE VATA — LA VOILE DU ROCHER
Une adresse privilégiée. La mer pour horizon.

À l’Anse Vata, il existe des adresses où l’on ne choisit pas seulement un appartement, mais un véritable art de vivre.

Au sein de La Voile du Rocher, découvrez ce superbe F3 de 83 m², prolongé par une généreuse terrasse couverte d’environ 40 m² avec vue mer.

Ici, tout invite à profiter pleinement du cadre de vie de l’Anse Vata : la mer à quelques pas, les promenades du littoral, les couchers de soleil et l’animation de l’un des quartiers les plus emblématiques de Nouméa.

UN QUOTIDIEN ENTRE MER & ÉVASION

Sortir de chez soi pour une promenade en bord de mer, rejoindre la plage, déjeuner au restaurant ou profiter des services du complexe sans prendre sa voiture…

La Voile du Rocher offre une véritable vie de quartier au sein même de la résidence, avec :

Salle de fitness • 2 restaurants • Coiffeurs • Boutiques de mode

Un environnement vivant et privilégié qui conjugue confort, loisirs et bord de mer.

UNE RÉSIDENCE CONTEMPORAINE

La partie extension représentant une construction neuve se distingue par son architecture moderne, ses espaces paysagers luxuriants et la qualité de ses prestations.

Sécurisée et équipée d’un système de vidéosurveillance, elle offre un cadre particulièrement soigné au cœur de l’Anse Vata.

UN APPARTEMENT OUVERT SUR L’EXTÉRIEUR

Situé au 4ᵉ étage, l’appartement offre de beaux volumes baignés de lumière.

Le séjour et la cuisine contemporaine entièrement équipée s'ouvrent sur une magnifique terrasse couverte d’environ 40 m², véritable prolongement de l’espace de vie avec vue sur la mer.

L’espace nuit comprend une suite parentale avec salle d’eau, grand dressing et WC, ainsi qu’une seconde chambre disposant de sa propre salle d’eau.

83 m² habitables • Terrasse ~40 m² • Vue mer • 2 chambres • 2 salles d’eau • Grand dressing • 2 stationnements couverts • Cellier privatif


Prix : 49 000 000 F CFP

Contactez-nous dès aujourd’hui pour en savoir plus ou planifier une visite :

Laurent NGUYEN
Tél. : 289.888
Email : accueil@chatelin.nc
Horaires :
Lundi au jeudi : 8h-12h / 13h-17h
Vendredi : 8h-12h / 13h-16h
CP N 2013 165T et 165G - Garantie Bancaire: BCI"""

    res = structure_description(raw_desc)
    assert res["has_structured"] is True

    # 1. Contact négociateur direct extrait avec le numéro en triplets
    assert len(res["direct_contacts"]) >= 1
    nguyen = next((c for c in res["direct_contacts"] if "NGUYEN" in c["name"].upper()), None)
    assert nguyen is not None
    assert "289.888" in nguyen["phone"] or "289888" in nguyen["raw_phone"]
    assert nguyen["email"] == "accueil@chatelin.nc"

    # 2. Métadonnées d'agence extraites
    meta = res["agency_metadata"]
    assert "289.888" in (meta["phone"] or "") or "289888" in str(meta["phone"])
    assert meta["email"] == "accueil@chatelin.nc"
    assert len(meta["hours"]) >= 2
    assert any("CP N" in leg for leg in meta["legal"])

    # 3. Rubrique Services & Équipements de la résidence
    sec_services = next((s for s in res["sections"] if s["key"] == "services_residence"), None)
    assert sec_services is not None
    services_text = " ".join(sec_services["items"]).lower()
    assert "fitness" in services_text
    assert "restaurant" in services_text
    assert "coiffeur" in services_text
    assert "boutique" in services_text

    # 4. Décomposition de la ligne multi-attributs
    sec_nuit = next((s for s in res["sections"] if s["key"] == "espace_nuit"), None)
    assert sec_nuit is not None
    nuit_text = " ".join(sec_nuit["items"]).lower()
    assert "chambre" in nuit_text
    assert "dressing" in nuit_text
    assert "salle" in nuit_text

    sec_ext = next((s for s in res["sections"] if s["key"] == "exterieurs"), None)
    assert sec_ext is not None
    ext_text = " ".join(sec_ext["items"]).lower()
    assert "terrasse" in ext_text
    assert "vue mer" in ext_text

    sec_park = next((s for s in res["sections"] if s["key"] == "stationnement_annexes"), None)
    assert sec_park is not None
    park_text = " ".join(sec_park["items"]).lower()
    assert "stationnement" in park_text or "parking" in park_text
    assert "cellier" in park_text

    # 5. Absence totale de lignes de signature/horaires dans les sections descriptives
    all_body_items = []
    for s in res["sections"]:
        all_body_items.extend(s["items"])
    all_body_str = " ".join(all_body_items)

    assert "accueil@chatelin.nc" not in all_body_str
    assert "Laurent NGUYEN" not in all_body_str
    assert "8h-12h" not in all_body_str
    assert "CP N 2013" not in all_body_str


def test_voip_and_triplet_phone_formats():
    """Test des numéros VoIP / spéciaux NC commençant par 5 (ex: 505.510)."""
    text = "Superbe appartement à louer. Visiter avec Antoine au 505.510."
    res = structure_description(text)
    assert len(res["direct_contacts"]) >= 1
    c = res["direct_contacts"][0]
    assert c["name"] == "Antoine"
    assert "505.510" in c["phone"] or "505510" in c["raw_phone"]


def test_scraped_single_line_immonc_format():
    """Test des annonces scrappées monolignes avec . Contact: (ex: flux Immo.nc / Immocal)."""
    text = "Annonce Appartement F3 à Anse Vata Nouméa. Contact: AGENCE SOLEIL (Tél: 43.97.67, Email: contact@soleil.nc)."
    res = structure_description(text)
    assert res["has_structured"] is True
    # Le titre/type doit être dans une section descriptive (cadre_vie)
    sec_cadre = next((s for s in res["sections"] if s["key"] == "cadre_vie"), None)
    assert sec_cadre is not None
    assert any("Anse Vata" in it for it in sec_cadre["items"])
    # Les contacts doivent être proprement extraits
    assert res["agency_metadata"]["email"] == "contact@soleil.nc"
    assert "43.97.67" in (res["agency_metadata"]["phone"] or "")


def test_inline_dash_separated_contact():
    """Test des annonces compactes avec contact séparé par un tiret."""
    text = "PàP loue F4 duplex de charme VDC, 100 m² terrasse, 3 chambres + 1 dressing, 2 parking sécurisés, vue mer, 135k/mois - aude@yahoo.fr - Tel : 822692"
    res = structure_description(text)
    assert res["has_structured"] is True
    assert res["agency_metadata"]["email"] == "aude@yahoo.fr"
    # Vérifier que les pièces sont bien catégorisées
    sec_nuit = next((s for s in res["sections"] if s["key"] == "espace_nuit"), None)
    assert sec_nuit is not None
    assert any("chambre" in it.lower() for it in sec_nuit["items"])
    # Le contact ne doit pas figurer dans le corps
    all_items_str = " ".join(" ".join(s["items"]) for s in res["sections"])
    assert "aude@yahoo.fr" not in all_items_str

