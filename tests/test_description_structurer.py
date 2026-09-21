"""
Tests unitaires pour le module de structuration sémantique des descriptions.
Valide l'extraction nominative des contacts directs, le tri en 6 rubriques immuables,
le nettoyage de la pollution visuelle et la garantie de non-perte d'informations.
"""

import pytest
from src.processing.description_structurer import (
    extract_direct_contacts,
    clean_line_typography,
    classify_bullet_or_sentence,
    structure_description,
)


def test_extract_direct_contacts_multiple_agents():
    # Exemple réel tiré de l'annonce F4 Haut-Magenta
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


def test_extract_direct_contacts_formats_and_stops():
    # Test format 'Contact : Marc Dupont au 78.12.34'
    text1 = "Contact : Marc DUPONT au 78.12.34 pour organiser une visite."
    c1 = extract_direct_contacts(text1)
    assert len(c1) == 1
    assert "Marc Dupont" in c1[0]["name"]
    assert c1[0]["raw_phone"] == "781234"

    # Test format avec indicatif international '+687 82.30.40'
    text2 = "Renseignements avec Sophie au +687 82.30.40"
    c2 = extract_direct_contacts(text2)
    assert len(c2) == 1
    assert c2[0]["name"] == "Sophie"
    assert c2[0]["raw_phone"] == "823040"

    # Test format inversé '81.22.33 (Alexandre)'
    text3 = "Discuter du dossier : 81.22.33 (Alexandre)"
    c3 = extract_direct_contacts(text3)
    assert len(c3) == 1
    assert c3[0]["name"] == "Alexandre"
    assert c3[0]["raw_phone"] == "812233"

    # Test exclusion de stop names ("Appartement au 75.00.00" -> pas un prénom)
    text_stop = "Appartement au 75.00.00 disponible pour visite immédiate."
    c_stop = extract_direct_contacts(text_stop)
    assert len(c_stop) == 0


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

    # Pièces de vie
    assert classify_bullet_or_sentence("Un grand séjour lumineux avec cuisine ouverte équipée") == "pieces_de_vie"
    assert classify_bullet_or_sentence("Cuisine moderne avec îlot central et buanderie attenante") == "pieces_de_vie"

    # Espace nuit
    assert classify_bullet_or_sentence("3 chambres climatisées avec placards intégrés") == "espace_nuit"
    assert classify_bullet_or_sentence("Une salle d'eau moderne avec douche à l'italienne et WC séparé") == "espace_nuit"

    # Extérieurs
    assert classify_bullet_or_sentence("Superbe terrasse couverte de 30 m² avec vue mer imprenable") == "exterieurs"
    assert classify_bullet_or_sentence("Jardin arboré et clôturé avec piscine sans vis-à-vis") == "exterieurs"

    # Stationnement & Annexes
    assert classify_bullet_or_sentence("Deux places de parking sécurisées en sous-sol et un cellier") == "stationnement_annexes"
    assert classify_bullet_or_sentence("Garage fermé et carport pour 2 véhicules") == "stationnement_annexes"

    # Conditions & Modalités
    assert classify_bullet_or_sentence("Loyer mensuel : 180 000 F XPF charges comprises") == "conditions_modalites"
    assert classify_bullet_or_sentence("Dépôt de garantie : un mois de loyer hors charges") == "conditions_modalites"


def test_structure_description_end_to_end():
    raw_desc = """
    ✨ A SAISIR ABSOLUMENT ! COUP DE COEUR ASSURE ! ✨
    Dans une résidence sécurisée et très recherchée de l'Orphelinat.
    
    Il comprend :
    • Un vaste séjour lumineux donnant sur terrasse
    • Une cuisine américaine aménagée avec plaque et hotte
    • 2 chambres climatisées avec dressing
    • Une salle d'eau contemporaine avec double vasque et WC
    • Une belle terrasse couverte de 22 m² avec vue dégagée
    • 2 places de parking privatives
    • Un cellier attenant
    • Chauffe-eau solaire et volets roulants électriques
    
    Loyer : 145.000 F / mois (charges comprises)
    
    Visites et négociations avec Tim au 85.46.55 ou Séverine au 90.57.97 !
    """

    res = structure_description(raw_desc)
    assert res["has_structured"] is True

    # 1. Contacts directs
    assert len(res["direct_contacts"]) == 2
    contact_names = [c["name"] for c in res["direct_contacts"]]
    assert "Tim" in contact_names
    assert "Séverine" in contact_names

    # 2. Sections ordonnées strictement
    section_keys = [s["key"] for s in res["sections"]]
    # Doit respecter l'ordre canonique
    expected_order = [
        "cadre_vie",
        "pieces_de_vie",
        "espace_nuit",
        "exterieurs",
        "stationnement_annexes",
        "prestations_complementaires",
        "conditions_modalites",
    ]
    indices = [expected_order.index(k) for k in section_keys if k in expected_order]
    assert indices == sorted(indices)

    # 3. Badges atouts flash
    highlights_str = " ".join(res["key_highlights"])
    assert "Terrasse 22 m²" in highlights_str or "Terrasse" in highlights_str
    assert "Parking" in highlights_str
    assert "Climatisé" in highlights_str

    # 4. Zero loss : les informations essentielles de confort sont préservées
    all_items = []
    for s in res["sections"]:
        all_items.extend(s["items"])

    assert any("séjour" in item.lower() for item in all_items)
    assert any("cuisine" in item.lower() for item in all_items)
    assert any("chambres" in item.lower() for item in all_items)
    assert any("terrasse" in item.lower() for item in all_items)
    assert any("parking" in item.lower() for item in all_items)
    assert any("cellier" in item.lower() for item in all_items)
    assert any("chauffe-eau solaire" in item.lower() for item in all_items)
    assert any("loyer" in item.lower() for item in all_items)


def test_structure_description_empty_and_fallback():
    res_empty = structure_description("")
    assert res_empty["has_structured"] is False
    assert res_empty["sections"] == []

    res_none = structure_description(None)
    assert res_none["has_structured"] is False
