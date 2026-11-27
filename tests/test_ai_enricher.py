"""
Tests unitaires pour le moteur d'intelligence sémantique Tier-2 Sentinel (src/processing/ai_enricher.py).
Vérifie la gestion des statuts, le paramétrage des prompts/presets, le circuit-breaker Tier-1,
le dialogue interactif et la persistance DuckDB.
"""

import pytest
from src.processing.ai_enricher import (
    check_ai_status,
    get_ai_config,
    save_ai_config,
    enrich_listing_with_ai,
    ask_ai_chat,
    get_ai_cache_stats,
    clear_ai_cache
)


def test_ai_status_and_hardware_profile():
    """Vérifie que la sonde matérielle et l'état de connectivité sont correctement reportés."""
    status = check_ai_status()
    assert "status" in status
    assert status["status"] in ("online", "offline")
    assert "hardware" in status
    hw = status["hardware"]
    assert "GTX 1060" in hw["gpu"]
    assert hw["vram_total_gb"] == 6.0
    assert hw["vram_usable_gb"] == 4.9
    assert "i7-8750H" in hw["cpu"]


def test_ai_config_and_presets():
    """Vérifie le chargement et la sauvegarde de la configuration IA avec ses presets."""
    cfg = get_ai_config()
    assert "model" in cfg
    assert "presets" in cfg
    assert "standard" in cfg["presets"]
    assert "commercial" in cfg["presets"]
    assert "contacts_only" in cfg["presets"]

    # Modification temporaire et sauvegarde
    saved = save_ai_config({"temperature": 0.15})
    assert saved["temperature"] == 0.15

    # Rétablissement
    save_ai_config({"temperature": 0.1})


def test_enrich_listing_with_ai_tier1_fallback():
    """
    Vérifie que même si Ollama est hors ligne, le circuit breaker
    active le repli déterministe Tier-1 et produit un résultat conforme au schéma JSON.
    """
    test_text = (
        "A VENDRE - LOCAL COMMERCIAL / BUREAUX 6ème KM (Rue Iekawé)\n"
        "Grande pièce principale de 30 m², 3 bureaux.\n"
        "Dock / réserve de 45 m² (hauteur 3 m).\n"
        "5 places de parking dont 2 couvertes.\n"
        "WC séparé, volets roulants électriques, climatisation intégrale.\n"
        "PRIX DE VENTE : 26 MF\n"
        "Contact : Manu – 98.72.66\n"
    )

    result = enrich_listing_with_ai(
        listing_data=test_text,
        force_refresh=True
    )

    assert "direct_contacts" in result
    assert "sections" in result
    assert "provider_used" in result

    # Vérification extraction de Manu
    contacts = result["direct_contacts"]
    assert len(contacts) >= 1
    assert any(c["name"] == "Manu" and "98.72.66" in c["phone"] for c in contacts)

    # Vérification que la taxonomie commerciale est bien appliquée (pas d'Espace Nuit)
    sections = result["sections"]
    section_titles = [s["title"] for s in sections]
    assert any("Bureaux" in t for t in section_titles)
    assert not any("Espace Nuit" in t for t in section_titles)
    assert any("Stationnement" in t for t in section_titles)


def test_ask_ai_chat_deterministic():
    """Vérifie le fonctionnement du mode dialogue sur annonce en repli déterministe."""
    listing = {
        "title": "Bureau 6ème KM",
        "currentPrice": 26000000,
        "commune": "Nouméa",
        "description": "Bureau 85 m² avec dock 45 m² et 5 parkings. Manu au 98.72.66.",
        "directContacts": [{"name": "Manu", "phone": "+687 98.72.66"}]
    }

    # Question contact
    resp_contact = ask_ai_chat(listing, "Qui dois-je contacter pour ce bureau ?")
    assert "Manu" in resp_contact["answer"]
    assert "98.72.66" in resp_contact["answer"]

    # Question parking
    resp_parking = ask_ai_chat(listing, "Y a-t-il des parkings disponibles ?")
    assert "parking" in resp_parking["answer"].lower() or "stationnement" in resp_parking["answer"].lower()


def test_ai_cache_operations(tmp_path, monkeypatch):
    """Vérifie les opérations sur le cache DuckDB sur une base isolée (ne touche jamais la prod)."""
    import duckdb
    import src.processing.ai_enricher as ai_mod

    temp_db = tmp_path / "test_ai_cache.duckdb"
    with duckdb.connect(str(temp_db)) as con:
        con.execute("CREATE TABLE listings (id VARCHAR PRIMARY KEY)")
    monkeypatch.setattr(ai_mod, "DB_PATH", temp_db)

    stats = get_ai_cache_stats()
    assert "cached_count" in stats
    cleared = clear_ai_cache()
    assert cleared is True


def test_sanitize_ai_contacts_grounding_rejects_hallucinations():
    """
    Vérifie que le Grounding Check Python rejette formellement toute fuite de prompt
    (ex: Manu 98.72.66) et résout 'contactez l'agence au 284.282' vers l'agence.
    """
    from src.processing.ai_enricher import _sanitize_ai_contacts

    raw_text = (
        "immeuble roger berard au 4ème étage, plateau de bureaux aménagés "
        "bénéficiant de 8 places de stationnement. pour les visites, contactez l'agence au 284.282 ..."
    )

    # Cas 1 : L'IA a halluciné Manu et 98.72.66 à cause d'une fuite d'exemple
    fake_contacts = [
        {"name": "Manu", "phone": "+687 98.72.66", "whatsapp": "687987266", "role": "Négociateur"}
    ]
    sanitized = _sanitize_ai_contacts(
        fake_contacts,
        raw_description=raw_text,
        agency_name="CalÃ©donienne d",
        agency_phone="+687 28.42.82"
    )

    # Manu doit impérativement être éliminé !
    assert not any(c.get("name") == "Manu" for c in sanitized)
    assert not any("98.72.66" in str(c.get("phone")) for c in sanitized)

    # Le contact doit être attribué à l'agence Calédonienne d'Immobilier au 284.282
    assert len(sanitized) >= 1
    agency_contact = sanitized[0]
    assert agency_contact["name"] == "Calédonienne d'Immobilier"
    assert "284.282" in agency_contact["phone"] or "28.42.82" in agency_contact["phone"]
    assert agency_contact["role"] == "Agence"


def test_sanitize_ai_sections_grounding_rejects_fake_bullets():
    """Vérifie que les puces de sections inventées sans fondement textuel sont éliminées."""
    from src.processing.ai_enricher import _sanitize_ai_sections

    raw_text = "immeuble roger berard au 4ème étage, plateau de bureaux aménagés bénéficiant de 8 places de stationnement."
    fake_sections = [
        {
            "key": "bureaux",
            "title": "Bureaux & Espaces professionnels",
            "items": ["Plateau de bureaux aménagés au 4ème étage", "Dock réserve de 45 m²"]
        },
        {
            "key": "stationnement",
            "title": "Stationnement & Logistique",
            "items": ["5 places de parking dont 2 couvertes", "8 places de stationnement"]
        }
    ]

    clean = _sanitize_ai_sections(fake_sections, raw_description=raw_text, title="Bureau Roger Bérard")
    all_bullets = [it for s in clean for it in s.get("items", [])]

    # Vrais éléments du texte conservés
    assert any("plateau de bureaux" in b.lower() for b in all_bullets)
    assert any("8 places" in b.lower() for b in all_bullets)

    # Hallucinations (dock 45m², 5 places) éliminées
    assert not any("dock" in b.lower() for b in all_bullets)
    assert not any("45 m²" in b.lower() for b in all_bullets)
    assert not any("5 places" in b.lower() for b in all_bullets)


def test_mojibake_and_agency_normalization():
    """Vérifie la réparation du mojibake et la résolution d'agence tronquée."""
    from src.utils.text_cleaner import fix_mojibake, normalize_agency_name

    assert normalize_agency_name("CalÃ©donienne d") == "Calédonienne d'Immobilier"
    assert normalize_agency_name("caledonienne d") == "Calédonienne d'Immobilier"
    assert fix_mojibake("bureau aménagé au 4ème étage") == "bureau aménagé au 4ème étage"
    assert "Â" not in fix_mojibake("immeuble roger berardÂ  au 4ème étage")


def test_sanitize_ai_technical_specs_grounding():
    """Vérifie l'ancrage et la détection haute fidélité des spécifications techniques."""
    from src.processing.ai_enricher import _sanitize_ai_technical_specs

    desc = (
        "Appartement F2 au dernier étage d'une résidence sécurisée avec ascenseur. "
        "Large terrasse couverte de 14 m². Place de parking couverte sécurisée. "
        "Charges de copropriété : 12 000 F/mois. Bon standing."
    )

    # 1. Spécifications conformes retournées par l'IA
    raw_specs = {
        "charges_mensuelles_xpf": 12000,
        "etage": "Dernier étage",
        "has_elevator": True,
        "parking_detail": "Place de parking couverte",
        "surface_terrasse_m2": 14.0,
        "standing": "Bon standing"
    }
    clean = _sanitize_ai_technical_specs(raw_specs, raw_description=desc)
    assert clean["charges_mensuelles_xpf"] == 12000
    assert clean["etage"] == "Dernier étage"
    assert clean["has_elevator"] is True
    assert clean["surface_terrasse_m2"] == 14.0
    assert "couverte" in clean["parking_detail"].lower()
    assert clean["standing"] == "Bon standing"

    # 2. Spécifications hallucinées par l'IA (50 000 F de charges inexistantes)
    fake_specs = {
        "charges_mensuelles_xpf": 50000,
        "surface_terrasse_m2": 85.0
    }
    clean_fake = _sanitize_ai_technical_specs(fake_specs, raw_description=desc)
    # 50 000 doit être rejeté et remplacé par les 12 000 réels trouvés dans la description
    assert clean_fake["charges_mensuelles_xpf"] == 12000
    # 85 m² doit être rejeté et remplacé par 14.0 m² réel
    assert clean_fake["surface_terrasse_m2"] == 14.0


def test_kone_f4_canonical_formatting_and_zero_loss():
    """
    Valide sur le cas réel 'Maison F4 à Extérieur (Koné)' que :
    1. Une sortie LLM dégradée ('BIEN RÉSIDENTIEL', 'kitchen équipée', '16 ares', '15m²' coupés)
       est intégralement reformatée dans le gabarit canonique invariant.
    2. Les anglicismes ('kitchen') et puces numériques orphelines ('15m²', '16 ares') disparaissent.
    3. Aucune information n'est perdue ('dont 1 privative', 'Au calme', 'Idéale pour la famille').
    4. Le contact direct 'Céline : 73.06.00' est extrait.
    """
    from src.processing.ai_enricher import _sanitize_ai_sections
    from src.processing.description_structurer import structure_description

    raw_kone = (
        "!! A VENDRE !!\n\n"
        "Seulement 18 000 000F\n\n"
        "Maison F4 INDIVIDUELLE sur un très beau terrain d'environ 16 ares avec 3 chambres climatisées, "
        "cuisine équipée, 2 salles de bain dont 1 privative, une terrasse couverte de 15m², un garage fermé.\n\n"
        "Au calme. Idéale pour la famille et 1er achat!\n\n"
        "Contactez-nous pour une visite :\n\n"
        "Céline : 73.06.00"
    )

    # Simulation de la sortie dégradée observée sur qwen2.5:1.5b avant refonte
    degraded_ai_output = [
        {
            "key": "bien_residentiel",
            "title": "BIEN RÉSIDENTIEL",
            "icon": "🏢",
            "items": [
                "Maison F4 INDIVIDUELLE",
                "16 ares",
                "3 chambres climatisées",
                "kitchen équipée",
                "2 salles de bain",
                "1 terrasse couverte",
                "15m²",
                "garage fermé"
            ]
        }
    ]

    sections = _sanitize_ai_sections(degraded_ai_output, raw_description=raw_kone, title="Maison F4 à Extérieur (Koné)", property_type="MAISON")
    sec_by_key = {s["key"]: s for s in sections}

    # 1. Aucun titre 'BIEN RÉSIDENTIEL' ne doit subsister
    assert not any("BIEN RÉSIDENTIEL" in s["title"].upper() for s in sections)

    # 2. Les rubriques canoniques attendues sont toutes présentes dans l'ordre exact
    expected_order = ["cadre_vie", "pieces_de_vie", "espace_nuit", "exterieurs", "stationnement_annexes", "conditions_modalites"]
    actual_keys = [s["key"] for s in sections if s["key"] in expected_order]
    assert actual_keys == expected_order

    # 3. Aucun anglicisme ('kitchen') ni fragment orphelin ('15m²' seul)
    all_items = [it for s in sections for it in s["items"]]
    assert not any("kitchen" in it.lower() for it in all_items)
    assert not any(it.strip() in ("15m²", "15 m²", "16 ares") for it in all_items)

    # 4. Vérification du contenu de chaque rubrique
    assert any("cuisine équipée" in it.lower() for it in sec_by_key["pieces_de_vie"]["items"])
    assert any("3 chambres climatisées" in it.lower() for it in sec_by_key["espace_nuit"]["items"])
    assert any("dont 1 privative" in it.lower() for it in sec_by_key["espace_nuit"]["items"])
    assert any("16 ares" in it.lower() for it in sec_by_key["exterieurs"]["items"])
    assert any("15 m²" in it.lower() for it in sec_by_key["exterieurs"]["items"])
    assert any("garage fermé" in it.lower() for it in sec_by_key["stationnement_annexes"]["items"])
    assert any("calme" in it.lower() or "famille" in it.lower() for it in sec_by_key["cadre_vie"]["items"])

    # 5. Contact direct Céline extrait
    t1 = structure_description(raw_kone)
    assert any(c["name"] == "Céline" and "73.06.00" in c["phone"] for c in t1["direct_contacts"])


def test_bureau_pk6_canonical_reconciliation_no_terrain():
    """
    Valide sur le cas réel 'Bureau P.k. 6' que si le LLM crée à tort une rubrique 'Terrain: 6ème KM'
    et oublie 'Surface principale : 85 m²' et '3 bureaux', la réconciliation hybride :
    - supprime le faux 'Terrain: 6ème KM',
    - restaure toutes les surfaces et les 3 bureaux dans 'Bureaux & Espaces professionnels'.
    """
    from src.processing.ai_enricher import _sanitize_ai_sections

    raw_pk6 = (
        "A VENDRE – LOCAL COMMERCIAL / BUREAUX 6ème KM (Rue Iekawé)\n"
        "Local commercial aménagé, bénéficiant d'une excellente visibilité en rez-de-chaussée d'une résidence récente au 6ème km.\n"
        "Idéal pour une activité tertiaire, médicale, restauration légère ou services.\n"
        "Surface principale : 85 m²\n"
        "Grande pièce principale de 30 m²\n"
        "3 bureaux\n"
        "Dock / réserve de 45 m² (hauteur 3 m)\n"
        "5 places de parking dont 2 couvertes\n"
        "WC séparé\n"
        "Volets roulants électriques\n"
        "Climatisation intégrale\n"
        "Contact : Manu – 98.72.66"
    )

    degraded_pk6_ai = [
        {"key": "terrain", "title": "Terrain", "items": ["6ème KM"]},
        {"key": "bureaux", "title": "Bureaux & Espaces professionnels", "items": ["WC séparé", "Volets roulants électriques", "Climatisation intégrale"]},
        {"key": "stationnement", "title": "Stationnement & Logistique", "items": ["Dock / réserve de 45 m² (hauteur 3 m)", "5 places de parking dont 2 couvertes"]}
    ]

    sections = _sanitize_ai_sections(degraded_pk6_ai, raw_description=raw_pk6, title="Bureau PK6", property_type="COMMERCIAL")
    titles = [s["title"] for s in sections]

    assert "Terrain" not in titles
    sec_bureaux = next(s for s in sections if s["key"] == "espace_nuit")
    assert sec_bureaux["title"] == "Bureaux & Espaces professionnels"
    bureaux_str = " ".join(sec_bureaux["items"]).lower()
    assert "85 m²" in bureaux_str
    assert "30 m²" in bureaux_str
    assert "3 bureaux" in bureaux_str


def test_haut_magenta_and_paita_ai_reconciliation_zero_loss():
    """
    Valide que _sanitize_ai_sections et _sanitize_ai_technical_specs :
    1. Éliminent les fragments d'URL '[IMG: https' et '//api.bienmeloger.nc' même si le LLM les a recopiés.
    2. Conservent les tags de niveau '(RDC)' et '(À l'étage)' ainsi que la rubrique 'prestations_complementaires'
       sur la Villa F5 Duplex Haut Magenta sans doublon.
    3. Corrigent les charges mensuelles '3000.00' en 3000 F (et jamais 300 000 F) et rejettent 'hors charges'.
    """
    from src.processing.ai_enricher import _sanitize_ai_sections, _sanitize_ai_technical_specs

    raw_magenta = """[IMG: https://api.bienmeloger.nc/ listings/images/79263869-c7ac-4bb4-b421-01ab8282b2fd] EXCLUSIVITÉ

Située dans un environnement calme, sur le secteur de Haut Magenta et à proximité immédiate de toutes les commodités, découvrez cette charmante villa F5 duplex, en copropriété, offrant une superficie habitable de 135 m² sur un terrain clos de 4 ares avec piscine. 

Au rez-de-chaussée, la maison offre un bel espace de vie composé d'un séjour/salle à manger ouvrant sur une terrasse couverte, une cuisine ouverte entièrement aménagée et équipée, un dégagement distribuant un bureau et deux chambres avec placards, dont une bénéficiant de sa propre salle de bains avec WC. Une seconde salle d'eau, un WC indépendant ainsi qu'un garage avec espace buanderie viennent compléter ce niveau.

À l'étage, vous trouverez une troisième chambre avec dressing, accompagnée de sa salle d'eau avec WC.

Pour un confort optimal, la villa est entièrement climatisée et dispose d'un système d'alarme, d'un chauffe-eau solaire ainsi que de panneaux photovoltaïques.

Contactez Stéphane au 78 61 30 pour tout renseignement complémentaire ou organiser une visite."""

    polluted_ai_rubriques = {
        "cadre_vie": ["[IMG: https", "//api.bienmeloger.nc/ listings/images/79263869-c7ac-4bb4-b421-01ab8282b2fd] EXCLUSIVITÉ"],
        "espace_nuit": [
            "Au rez-de-chaussée, la maison offre un bel espace de vie composé d'un séjour/salle à manger ouvrant sur une terrasse couverte, une cuisine ouverte entièrement aménagée et équipée, un dégagement distribuant un bureau et deux chambres avec placards, dont une bénéficiant de sa propre salle de bains avec WC",
            "Une seconde salle d'eau, un WC indépendant ainsi qu'un garage avec espace buanderie viennent compléter ce niveau",
            "Pour un confort optimal, la villa est entièrement climatisée et dispose d'un système d'alarme, d'un chauffe-eau solaire ainsi que de panneaux photovoltaïques"
        ],
        "exterieurs": [
            "Située dans un environnement calme, sur le secteur de Haut Magenta et à proximité immédiate de toutes les commodités, découvrez cette charmante villa F5 duplex, en copropriété, offrant une superficie habitable de 135 m² sur un terrain clos de 4 ares avec piscine"
        ]
    }

    sections = _sanitize_ai_sections(polluted_ai_rubriques, raw_description=raw_magenta, title="Maison F5 à Nouméa (Haut-magenta)", property_type="MAISON")
    sec_by_key = {s["key"]: s["items"] for s in sections}
    all_items = [it for s in sections for it in s["items"]]
    all_str = " | ".join(all_items).lower()

    assert "img:" not in all_str
    assert "bienmeloger" not in all_str
    assert "prestations_complementaires" in sec_by_key
    assert any("alarme" in it.lower() for it in sec_by_key["prestations_complementaires"])
    assert any("chauffe-eau solaire" in it.lower() for it in sec_by_key["prestations_complementaires"])
    assert any("panneaux photovoltaïques" in it.lower() for it in sec_by_key["prestations_complementaires"])
    assert any("garage avec espace buanderie (rdc)" in it.lower() for it in sec_by_key["stationnement_annexes"])
    assert any("3ème chambre avec dressing" in it.lower() and "(à l'étage)" in it.lower() for it in sec_by_key["espace_nuit"])

    # Vérification anti-.00 x100 sur les charges mensuelles
    specs_dec = _sanitize_ai_technical_specs({"charges_mensuelles_xpf": "3000.00"}, raw_description="Charges mensuelles : 3000.00 F")
    assert specs_dec["charges_mensuelles_xpf"] == 3000

    # Vérification anti-'hors charges'
    specs_hc = _sanitize_ai_technical_specs({}, raw_description="Loyer hors charges : 150 000 F CFP/mois")
    assert specs_hc["charges_mensuelles_xpf"] is None


def test_villa_f4_kone_immonc_5332720_ai_reconciliation():
    """
    Valide sur le cas signalé par l'utilisateur 'Maison F4 à Autre (Koné)' (immonc_5332720) :
    Même si le LLM recopie les paragraphes bruts non découpés (question rhétorique,
    bannière 'A LOUER -', énumération partitive 'Elle se compose d'un séjour...'),
    _sanitize_ai_sections les décompose et les ventile proprement sans résidu narratif.
    """
    from src.processing.ai_enricher import _sanitize_ai_sections

    raw_kone = """A LOUER - VILLA F4 / VILLAGE DE KONE<br /><br />Vous cherchez de l'espace, du confort et un emplacement pratique ? Cette villa F4 d'environ 115 m² vous séduira par ses volumes et sa situation idéale en plein centre du village.<br /><br />Elle se compose d'un séjour lumineux, d'une cuisine fonctionnelle, de trois chambres, d'une buanderie, d'une salle d'eau, d'un WC séparé, d'un espace de rangement ainsi que d'un garage.<br /><br />Située à proximité immédiate de toutes les commodités, elle offre un cadre de vie pratique et agréable.<br /><br />Loyer charges comprises (Eau) : 105.000 XPF<br /><br />Disponible immédiatement.<br /><br />Pour plus d'informations ou organiser une visite :<br />Contactez notre équipe au 47.25.55"""

    unsplit_ai_rubriques = {
        "cadre_vie": [
            "A LOUER - VILLA F4 / VILLAGE DE KONE",
            "Vous cherchez de l'espace, du confort et un emplacement pratique ? Cette villa F4 d'environ 115 m² vous séduira par ses volumes et sa situation idéale en plein centre du village.",
            "Située à proximité immédiate de toutes les commodités, elle offre un cadre de vie pratique et agréable."
        ],
        "pieces_de_vie": [
            "Elle se compose d'un séjour lumineux, d'une cuisine fonctionnelle, de trois chambres, d'une buanderie, d'une salle d'eau, d'un WC séparé, d'un espace de rangement ainsi que d'un garage."
        ],
        "conditions_modalites": [
            "Loyer charges comprises (Eau) : 105.000 XPF"
        ]
    }

    sections = _sanitize_ai_sections(unsplit_ai_rubriques, raw_description=raw_kone, title="Maison F4 à Autre (Koné)", property_type="MAISON")
    sec_by_key = {s["key"]: s["items"] for s in sections}
    all_items = [it for s in sections for it in s["items"]]
    all_str = " | ".join(all_items)

    assert "?" not in all_str
    assert "vous cherchez" not in all_str.lower()
    assert "vous séduira" not in all_str.lower()
    assert "elle se compose" not in all_str.lower()
    assert "A LOUER -" not in all_str

    assert any("séjour lumineux" == it.lower() for it in sec_by_key["pieces_de_vie"])
    assert any("cuisine fonctionnelle" == it.lower() for it in sec_by_key["pieces_de_vie"])
    assert any("buanderie" == it.lower() for it in sec_by_key["pieces_de_vie"])
    assert any("3 chambres" == it.lower() for it in sec_by_key["espace_nuit"])
    assert any("salle d'eau" == it.lower() for it in sec_by_key["espace_nuit"])
    assert any("wc séparé" == it.lower() for it in sec_by_key["espace_nuit"])
    assert any("espace de rangement" == it.lower() for it in sec_by_key["stationnement_annexes"])
    assert any("garage" == it.lower() for it in sec_by_key["stationnement_annexes"])


def test_batch_enrich_listings_force_all_rescan(tmp_path, monkeypatch):
    """
    Valide que batch_enrich_listings permet de forcer la ré-analyse complète
    même pour les annonces déjà enrichies (force_all=True / only_missing=False)
    sur une base isolée (sans toucher la base de production).
    """
    import duckdb
    import src.processing.ai_enricher as ai_mod
    from src.processing.ai_enricher import batch_enrich_listings, get_ai_cache_stats

    temp_db = tmp_path / "test_batch_ai.duckdb"
    with duckdb.connect(str(temp_db)) as con:
        con.execute("""
            CREATE TABLE listings (
                id VARCHAR PRIMARY KEY,
                title VARCHAR,
                property_type VARCHAR,
                commune VARCHAR,
                quartier VARCHAR,
                price_xpf BIGINT,
                description VARCHAR,
                agency_name VARCHAR,
                scraped_at TIMESTAMP,
                is_active BOOLEAN
            )
        """)
        for i in range(1, 6):
            con.execute(
                "INSERT INTO listings VALUES (?, ?, 'APPARTEMENT', 'NOUMEA', 'Anse Vata', 25000000, ?, 'Agence Test', CURRENT_TIMESTAMP, TRUE)",
                [f"test_{i}", f"Appartement F3 #{i}", f"Bel appartement F3 climatisé avec terrasse de 15 m² et parking #{i}."]
            )
    monkeypatch.setattr(ai_mod, "DB_PATH", temp_db)

    # 1. Premier passage sur 5 annonces
    res1 = batch_enrich_listings(limit=5, only_missing=False, force_all=True, custom_provider="deterministic")
    assert res1["success"] is True
    assert res1["processed_count"] == 5

    stats1 = get_ai_cache_stats()
    assert stats1["cached_count"] >= 5

    # 2. Second passage avec force_all=True : doit re-scanner les 5 mêmes annonces (et non 0)
    progress_steps = []
    res2 = batch_enrich_listings(
        limit=5,
        only_missing=False,
        force_all=True,
        custom_provider="deterministic",
        progress_callback=lambda c, t, m: progress_steps.append((c, t))
    )
    assert res2["success"] is True
    assert res2["processed_count"] == 5
    assert len(progress_steps) >= 2
    assert progress_steps[0] == (0, 5)
    assert progress_steps[-1] == (5, 5)


def test_v4_canonical_invariance_dedup_and_specs():
    """
    Valide la refonte v4 (Gabarit Canonique Invariant) sur des cas réels de la base NC :
    - Suppression des accroches commerciales ('Découvrez', 'En exclusivité') et sous-titres vides ('Caractéristiques du bien').
    - Déduplication intelligente ('Grand séjour' + 'Séjour' -> 'Grand séjour').
    - Classement strict ('Possibilité de garer une voiture...' -> stationnement_annexes ; 'rez de jardin de 80 m²' -> cadre_vie).
    - Garde-fou anti faux-positif highlights ('rez de jardin de 80 m²' ne produit pas 'Jardin ~80 m²').
    - Hygiène stricte des tags de niveau (pas de '(À l'étage)' sur 'Disponible tout de suite').
    - Extraction complète des technical_specs (charges, terrasse, surface habitable, étage, parking).
    """
    from src.processing.description_structurer import structure_description, extract_technical_specs

    raw_f4_tanamera = (
        "F4 — MAGENTA — RESIDENCE TANAMERA\n"
        "Découvrez ce bel appartement F4 de 95.12 m² habitables, à Magenta.\n"
        "Caractéristiques du bien\n"
        "- Grand séjour\n"
        "- Séjour\n"
        "- Cuisine équipée\n"
        "- Suite parentale climatisée avec volet roulant électrique, placard et salle de bain\n"
        "- Terrasse de 17 m²\n"
        "- Ce bien est complété par un cellier et 2 places de parking privatives.\n"
        "Loyer hors charges : 160.000f\n"
        "Charges (hors eau et électricité) : 15.000f\n"
        "Loyer charges comprises : 175.000f"
    )
    st1 = structure_description(raw_f4_tanamera, property_type="APPARTEMENT")
    sec1 = {s["key"]: s["items"] for s in st1["sections"]}
    all1 = " | ".join(it for items in sec1.values() for it in items).lower()

    assert "caractéristiques du bien" not in all1
    assert "découvrez" not in all1
    assert not any(it.strip().lower() == "séjour" for it in sec1.get("pieces_de_vie", []))
    assert any("grand séjour" in it.lower() for it in sec1.get("pieces_de_vie", []))
    assert st1["technical_specs"]["charges_mensuelles_xpf"] == 15000
    assert st1["technical_specs"]["surface_terrasse_m2"] == 17.0
    assert st1["technical_specs"]["surface_habitable_m2"] == 95.12
    assert "2 places de parking" in (st1["technical_specs"]["parking_detail"] or "").lower()

    raw_f3_rdj = (
        "EN EXCLUSIVITÉ – F3 DUPLEX AVEC BEL ESPACE EXTÉRIEUR À MAGENTA STADE\n\n"
        "Découvrez ce spacieux appartement F3 en rez de jardin de 80m2.\n"
        "Son véritable atout : un agréable espace extérieur privatif d'environ 45 m².\n"
        "À l'étage : une chambre de 17 m² avec dressing.\n"
        "Disponible tout de suite.\n"
        "Possibilité de garer une voiture et abri pour véhicule deux roues.\n"
        "Charges de copropriété : seulement 6.900 F CFP/ mois"
    )
    st2 = structure_description(raw_f3_rdj, property_type="APPARTEMENT")
    sec2 = {s["key"]: s["items"] for s in st2["sections"]}

    assert not any("80" in h and "jardin" in h.lower() for h in st2["key_highlights"])
    assert any("garer une voiture" in it.lower() for it in sec2.get("stationnement_annexes", []))
    assert not any("garer une voiture" in it.lower() for it in sec2.get("cadre_vie", []))
    assert any("80 m²" in it for it in sec2.get("cadre_vie", []))
    assert any(it == "Disponible tout de suite" for it in sec2.get("conditions_modalites", []))
    assert not any("(à l'étage)" in it.lower() for it in sec2.get("conditions_modalites", []))
    assert st2["technical_specs"]["charges_mensuelles_xpf"] == 6900
    assert st2["technical_specs"]["etage"] == "Rez-de-jardin"





