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


def test_ai_cache_operations():
    """Vérifie les opérations sur le cache DuckDB."""
    stats = get_ai_cache_stats()
    assert "cached_count" in stats
    cleared = clear_ai_cache()
    assert cleared is True
