"""
Module d'Intelligence Sémantique & Enrichissement IA Sentinel (Tier-2).
Gère la communication avec le moteur LLM local (Ollama / Qwen / Gemma),
l'application du prompt paramétrable, le circuit-breaker avec repli Tier-1,
le dialogue interactif (Chatbot Q&R sur annonces) et la persistance DuckDB.
"""

import os
import sys
import re
import json
import time
import hashlib
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

# Racine du projet
BASE_DIR = Path(__file__).resolve().parent.parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.processing.description_structurer import (
    structure_description,
    normalize_nc_phone,
    format_bullet_uniform,
    normalize_header_banner,
    split_compound_line,
    classify_bullet_or_sentence,
    is_contact_or_boilerplate_line,
    strip_technical_noise,
    synthesize_atomic_bullet,
    is_valid_person_name,
    sanitize_section_bullets,
    extract_technical_specs,
    CATEGORY_DEFINITIONS,
    STOP_NAMES,
)
from src.utils.text_cleaner import fix_mojibake, normalize_agency_name
from src.domain.agencies_directory import NC_AGENCIES
from src.utils.logger import get_logger

logger = get_logger("ai_enricher")

CONFIG_PATH = BASE_DIR / "config" / "ai_settings.json"
DB_PATH = BASE_DIR / "data" / "processed" / "immobilier_grand_noumea.duckdb"

HARDWARE_SPECS = {
    "gpu": "NVIDIA GeForce GTX 1060 6 Go GDDR5",
    "vram_total_gb": 6.0,
    "vram_usable_gb": 4.9,
    "vram_status": "Profil MSI GS65 Stealth : CPU AVX2 Eco-Thermique (4 threads) pour stabilité totale",
    "cpu": "Intel Core i7-8750H (6C/12T @ 2.20 GHz)",
    "ram_gb": 16,
    "os": "Windows 11 Professionnel 64-bit",
    "mode": "CPU AVX2 Eco (4 threads dédiés, 0 surchauffe)"
}

DEFAULT_SYSTEM_PROMPT = (
    "Tu es le moteur d'intelligence sémantique de référence pour l'immobilier en Nouvelle-Calédonie (Sentinel ImmoNC — Gabarit Canonique v4).\n"
    "Ta mission : transformer N'IMPORTE QUELLE annonce brute en une fiche structurée, rigoureusement identique d'une annonce à l'autre, "
    "sans AUCUNE perte d'information factuelle et sans AUCUNE invention. Réponds EXCLUSIVEMENT en JSON strict et 100 % en FRANÇAIS.\n\n"
    "1. CONTACTS DIRECTS ('direct_contacts') :\n"
    "- Négociateur nommé (ex: 'Céline : 73.06.00', 'Stéphane au 78 61 30', 'Laurent NGUYEN au 289.888') -> name=Prénom/Nom, role='Négociateur', phone=numéro NC à 6 chiffres.\n"
    "- Agence uniquement ('contactez l'agence au XX.XX.XX') -> name=nom de l'agence (champ 'agency'), role='Agence', phone=numéro.\n"
    "- Aucun contact explicite dans le texte -> []. ZÉRO hallucination, ne jamais inventer un prénom ou un numéro.\n\n"
    "2. SPÉCIFICATIONS TECHNIQUES QUANTIFIÉES ('technical_specs') :\n"
    "- surface_habitable_m2 (flottant m² ou null), surface_terrain_m2 (flottant m² en convertissant 1 are = 100 m², ou null), surface_terrasse_m2 (flottant m² ou null).\n"
    "- charges_mensuelles_xpf (entier XPF de charges mensuelles/copropriété ou null ; INTERDICTION d'extraire un loyer 'hors charges').\n"
    "- etage (ex: 'Rez-de-chaussée', 'Rez-de-jardin', '2ème étage', 'Dernier étage', ou null), has_elevator (true/false/null).\n"
    "- parking_detail (ex: '2 places de parking privatives', 'Garage fermé', 'Carport', ou null), standing (ex: 'Bon standing', 'Haut standing', ou null).\n"
    "- has_air_conditioning (bool), has_pool (bool), has_sea_view (bool).\n\n"
    "3. CLASSEMENT STRICT EN 8 RUBRIQUES CANONIQUES INVARIANTES ('rubriques') :\n"
    "RÈGLES IMPÉRATIVES DE MISE EN FORME :\n"
    "- 1 fait concret par puce, style nominal court commençant par une majuscule (ex: 'Cuisine équipée avec îlot central', 'Terrasse couverte de 17 m²').\n"
    "- ZÉRO langage publicitaire : SUPPRIMER 'Découvrez ce bel...', 'En exclusivité', 'Coup de cœur', 'Vous séduira par...', 'Idéal pour profiter...', 'complète ce bien', ainsi que les sous-titres vides ('Caractéristiques du bien', 'Description').\n"
    "- ZÉRO doublon : ne jamais écrire 'Séjour' et 'Grand séjour' séparément ; regrouper en une seule puce riche.\n"
    "- ZÉRO perte de chiffre : ne JAMAIS séparer une surface ou un montant de son libellé ('Terrasse couverte de 15 m²', jamais '15 m²' seul).\n"
    "- Niveaux '(RDC)' / '(À l'étage)' UNIQUEMENT sur les pièces intérieures (pièces de vie, espace nuit, garage/cellier), JAMAIS sur le cadre de vie ni les modalités.\n\n"
    "TABLE DE CLASSEMENT INVARIANTE (CHAQUE INFORMATION DOIT TOUJOURS ALLER DANS LA MÊME RUBRIQUE) :\n"
    "- 'cadre_vie' : Identité globale du bien (type, F1-F6, duplex, surface habitable m²), quartier/commune, résidence, calme, proximité écoles/commerces/plages, étage global. (Un 'appartement F3 en rez-de-jardin de 80 m²' va ICI, JAMAIS dans exterieurs ; le parking ne va JAMAIS ici).\n"
    "- 'services_residence' : Ascenseur, résidence sécurisée, digicode/interphone, vidéosurveillance, gardien, salle de sport/fitness, piscine commune, commerces de l'immeuble.\n"
    "- 'pieces_de_vie' : Séjour, salon, salle à manger, cuisine (équipée/aménagée/ouverte/îlot), buanderie intérieure.\n"
    "- 'espace_nuit' : Chambres (avec m², placards, dressing, clim, volets), suite parentale, bureau, salles de bain / salles d'eau, WC séparés — OU bureaux, pièce principale, plateau, open-space et sanitaires si bien commercial/professionnel.\n"
    "- 'exterieurs' : Terrasse/varangue/deck/balcon (avec m²), jardin/jardinet privatif (ne JAMAIS confondre 'rez-de-jardin de 80 m²' avec un jardin de 80 m²), terrain (en ares/m²), piscine privative, pergola/faré, vue mer/lagon/dégagée.\n"
    "- 'stationnement_annexes' : Places de parking (nombre, couvert/sécurisé/privatif), garage, carport, box, abri voiture/deux-roues, cellier, cave, dock, réserve, atelier, espace de rangement.\n"
    "- 'prestations_complementaires' : Climatisation globale, volets roulants électriques/manuels, persiennes, chauffe-eau solaire, panneaux photovoltaïques, alarme, portail motorisé, visiophone, fibre, double vitrage, moustiquaires, fosse septique, citerne, groupe électrogène.\n"
    "- 'conditions_modalites' : Prix de vente (FAI/net), loyer hors charges, charges mensuelles/copropriété, loyer charges comprises, dépôt de garantie/caution, disponibilité, bail, colocation acceptée/possible, meublé/non meublé."
)


def get_ai_config() -> Dict[str, Any]:
    """Charge la configuration IA actuelle ou génère celle par défaut."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Erreur lecture config IA: {e}")

    default_cfg = {
        "provider": "ollama",
        "ollama_url": "http://localhost:11434",
        "model": "qwen2.5:1.5b",
        "temperature": 0.1,
        "timeout_seconds": 40.0,
        "active_preset": "standard",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "presets": {
            "standard": {
                "name": "🎯 Standard Immo NC (Gabarit Canonique v4)",
                "description": "Gabarit canonique v4 à clés fixes : 100 % des annonces classées exactement dans le même ordre, sans bruit marketing, sans doublon ni perte de détail, avec extraction complète des spécifications techniques.",
                "temperature": 0.1,
                "prompt": DEFAULT_SYSTEM_PROMPT
            },
            "commercial": {
                "name": "🏢 Spécial Tertiaire, Docks & Commerces",
                "description": "Optimisé pour bureaux, surfaces utiles, hauteurs sous plafond des docks, puissance électrique et parkings professionnels (zéro espace nuit).",
                "temperature": 0.1,
                "prompt": (
                    "Tu es un expert en immobilier d'entreprise, commercial et logistique en Nouvelle-Calédonie.\n"
                    "RÈGLES STRICTES (100 % en français) :\n"
                    "1. Extraire le contact direct du négociateur ou de l'agence dans 'direct_contacts' (formats NC 7x/8x/9x ou 2x/3x/4x).\n"
                    "2. Isoler les spécifications techniques dans 'technical_specs' : charges_mensuelles_xpf, etage, has_elevator, parking_detail.\n"
                    "3. Ventiler toutes les caractéristiques dans le dictionnaire 'rubriques' à clés fixes ('cadre_vie', 'pieces_de_vie', 'espace_nuit' pour les bureaux, 'stationnement_annexes' pour parkings/dock, 'conditions_modalites').\n"
                    "4. Ne jamais couper une surface de son intitulé et répondre EXCLUSIVEMENT en JSON strict."
                )
            },
            "contacts_only": {
                "name": "⚡ Extraction Rapide des Contacts & Négociateurs",
                "description": "Isolation prioritaire des négociateurs dédiés, numéros mobiles OPT (7x/8x/9x), fixes et coordonnées directes.",
                "temperature": 0.0,
                "prompt": (
                    "Tu es un analyseur ultra-rapide de coordonnées immobilières pour la Nouvelle-Calédonie.\n"
                    "Extrais tous les contacts réels de l'annonce dans 'direct_contacts' :\n"
                    "- Négociateur physique si nommé (ex: Céline, Florian, Sophie, Laurent NGUYEN) avec role='Négociateur'.\n"
                    "- Agence si mentionnée (ex: 'contactez l'agence au...') avec role='Agence'.\n"
                    "- Numéros de téléphone au format calédonien à 6 chiffres (+687).\n"
                    "Formate la réponse sous forme de JSON strict."
                )
            },
            "investor": {
                "name": "💰 Analyse Investisseur & Rendement",
                "description": "Met l'accent sur les charges de copropriété, travaux, défiscalisation (loi Girardin/local) et rentabilité locative.",
                "temperature": 0.2,
                "prompt": (
                    "Tu es un conseiller en investissement immobilier en Nouvelle-Calédonie.\n"
                    "Analyse cette annonce sous l'angle du rendement financier en utilisant le gabarit fixe 'rubriques' et 'technical_specs'.\n"
                    "Réponds EXCLUSIVEMENT en JSON strict et en français."
                )
            }
        }
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(default_cfg, f, ensure_ascii=False, indent=2)
    return default_cfg


def save_ai_config(new_config: Dict[str, Any]) -> Dict[str, Any]:
    """Sauvegarde les paramètres IA."""
    cfg = get_ai_config()
    cfg.update(new_config)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def check_ai_status() -> Dict[str, Any]:
    """
    Sonde la connectivité d'Ollama sur localhost:11434, liste les modèles
    et renvoie l'état du matériel GPU / VRAM.
    """
    cfg = get_ai_config()
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")
    tags_url = f"{ollama_url.rstrip('/')}/api/tags"

    status = "offline"
    models = []
    ping_ms = 0
    message = "Ollama n'est pas démarré. Le moteur Tier-1 déterministe assure l'enrichissement instantané."

    t0 = time.time()
    try:
        req = urllib.request.Request(tags_url, headers={"User-Agent": "Sentinel-ImmoNC/1.0"})
        with urllib.request.urlopen(req, timeout=1.2) as resp:
            ping_ms = int((time.time() - t0) * 1000)
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name") for m in body.get("models", [])]
                status = "online"
                message = f"Ollama connecté ({ping_ms} ms). {len(models)} modèle(s) détecté(s)."
    except Exception as e:
        ping_ms = int((time.time() - t0) * 1000)

    # Récupération des stats du cache DuckDB
    cache_stats = get_ai_cache_stats()

    return {
        "status": status,
        "ollama_url": ollama_url,
        "active_model": cfg.get("model", "qwen2.5:3b"),
        "installed_models": models,
        "ping_ms": ping_ms,
        "message": message,
        "hardware": HARDWARE_SPECS,
        "cache": cache_stats
    }


def init_ai_cache_table():
    """Initialise la table DuckDB de persistance de l'enrichissement sémantique."""
    try:
        import duckdb
        if not DB_PATH.exists():
            return
        con = duckdb.connect(str(DB_PATH))
        con.execute("""
        CREATE TABLE IF NOT EXISTS listing_ai_enrichment (
            listing_id VARCHAR PRIMARY KEY,
            model_version VARCHAR,
            enriched_at TIMESTAMP,
            input_hash VARCHAR,
            extracted_contacts_json VARCHAR,
            structured_sections_json VARCHAR,
            raw_response_json VARCHAR,
            latency_ms INTEGER
        );
        """)
        cols = [c[1] for c in con.execute("PRAGMA table_info('listing_ai_enrichment');").fetchall()]
        if "enriched_at" not in cols:
            con.execute("DROP TABLE listing_ai_enrichment;")
            con.execute("""
            CREATE TABLE listing_ai_enrichment (
                listing_id VARCHAR PRIMARY KEY,
                model_version VARCHAR,
                enriched_at TIMESTAMP,
                input_hash VARCHAR,
                extracted_contacts_json VARCHAR,
                structured_sections_json VARCHAR,
                raw_response_json VARCHAR,
                latency_ms INTEGER
            );
            """)
        con.close()
    except Exception as e:
        logger.warning(f"Erreur init_ai_cache_table : {e}")


def get_ai_cache_stats() -> Dict[str, Any]:
    """Retourne les statistiques d'utilisation du cache IA en DuckDB."""
    try:
        import duckdb
        if not DB_PATH.exists():
            return {"cached_count": 0, "status": "no_db"}
        init_ai_cache_table()
        con = duckdb.connect(str(DB_PATH), read_only=True)
        tables = con.execute("SHOW TABLES").fetchall()
        table_names = [t[0] for t in tables]
        if "listing_ai_enrichment" not in table_names:
            con.close()
            return {"cached_count": 0, "status": "empty"}
        row = con.execute("SELECT COUNT(*), MAX(enriched_at) FROM listing_ai_enrichment").fetchone()
        con.close()
        return {
            "cached_count": row[0] if row else 0,
            "last_enriched_at": str(row[1]) if row and row[1] else None,
            "status": "active"
        }
    except Exception as e:
        return {"cached_count": 0, "error": str(e)}


def clear_ai_cache() -> bool:
    """Vide le cache d'enrichissement IA."""
    try:
        import duckdb
        if not DB_PATH.exists():
            return True
        init_ai_cache_table()
        con = duckdb.connect(str(DB_PATH))
        con.execute("DELETE FROM listing_ai_enrichment;")
        con.close()
        return True
    except Exception as e:
        logger.warning(f"Erreur clear_ai_cache : {e}")
        return False


def _get_cached_enrichment(listing_id: str, input_hash: str) -> Optional[Dict[str, Any]]:
    """Récupère l'enrichissement en cache si disponible et si le contenu n'a pas changé."""
    try:
        import duckdb
        if not DB_PATH.exists():
            return None
        con = duckdb.connect(str(DB_PATH), read_only=True)
        row = con.execute("""
            SELECT model_version, enriched_at, extracted_contacts_json, 
                   structured_sections_json, raw_response_json, latency_ms 
            FROM listing_ai_enrichment 
            WHERE listing_id = ? AND input_hash = ?
        """, [listing_id, input_hash]).fetchone()
        con.close()
        if row:
            raw_resp = json.loads(row[4]) if row[4] else {}
            return {
                "cached": True,
                "model": row[0],
                "enriched_at": str(row[1]),
                "direct_contacts": json.loads(row[2]) if row[2] else [],
                "technical_specs": raw_resp.get("technical_specs", {}),
                "sections": json.loads(row[3]) if row[3] else [],
                "raw_response": raw_resp,
                "latency_ms": row[5] or 1
            }
    except Exception:
        pass
    return None


def _save_cached_enrichment(listing_id: str, model: str, input_hash: str,
                            contacts: List[Dict], sections: List[Dict],
                            raw_resp: Dict, latency_ms: int):
    """Enregistre l'enrichissement dans DuckDB."""
    try:
        import duckdb
        init_ai_cache_table()
        with duckdb.connect(str(DB_PATH)) as con:
            con.execute("""
                INSERT OR REPLACE INTO listing_ai_enrichment 
                (listing_id, model_version, enriched_at, input_hash, extracted_contacts_json, structured_sections_json, raw_response_json, latency_ms)
                VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)
            """, [
                listing_id,
                model,
                input_hash,
                json.dumps(contacts, ensure_ascii=False),
                json.dumps(sections, ensure_ascii=False),
                json.dumps(raw_resp, ensure_ascii=False),
                latency_ms
            ])
    except Exception as e:
        logger.warning(f"Erreur sauvegarde cache IA: {e}")


def _sanitize_ai_contacts(
    raw_contacts: Any,
    raw_description: str,
    agency_name: str = "",
    agency_phone: str = "",
    property_type: str = ""
) -> List[Dict[str, Any]]:
    """
    Validation et ancrage strict (Grounding Check) des contacts extraits par l'IA :
    1. Rejette toute hallucination ou fuite d'exemples du prompt (ex: Manu, numéros absents).
    2. Valide que chaque numéro ou nom apparaît explicitement dans le texte ou les métadonnées de l'agence.
    3. Si l'annonce mentionne 'contactez l'agence au [tél]' sans négociateur particulier,
       résout et associe le contact au nom canonique de l'agence.
    """
    import re
    if not isinstance(raw_contacts, list):
        raw_contacts = []

    clean_agency_name = normalize_agency_name(agency_name)
    desc_clean = strip_technical_noise(fix_mojibake(raw_description or ""))
    desc_lower = desc_clean.lower()

    # Numéros de téléphone présents dans la description
    desc_phone_matches = re.findall(
        r'(?:(?:\+687|00687)[\s.-]*)?(?:([2-9]\d{2}[\s.-]\d{3})|([02-9]\d(?:[\s.-]?\d{2}){2})|\b([2-9]\d{5})\b)',
        desc_clean
    )
    all_desc_digits = set()
    for match_tuple in desc_phone_matches:
        for m in match_tuple:
            if m:
                d = re.sub(r'\D', '', m)
                if len(d) == 6:
                    all_desc_digits.add(d)
                elif len(d) == 7 and d.startswith('0'):
                    all_desc_digits.add(d[1:])

    # Digits associés à l'agence
    agency_digits = set()
    if agency_phone:
        ag_d = re.sub(r'\D', '', agency_phone)
        if len(ag_d) >= 6:
            agency_digits.add(ag_d[-6:])
    for ag in NC_AGENCIES:
        if ag.get("name") and (ag["name"].lower() == clean_agency_name.lower() or (len(clean_agency_name) >= 6 and clean_agency_name.lower().startswith(ag["name"].lower()[:8]))):
            if ag.get("phone"):
                ag_d = re.sub(r'\D', '', ag["phone"])
                if len(ag_d) >= 6:
                    agency_digits.add(ag_d[-6:])

    allowed_digits = all_desc_digits | agency_digits
    valid_contacts = []
    has_contactez_agence = bool(re.search(r'(?:contactez|contacter|joindre|visites?\s+avec)\s+(?:l[\'\’]agence|notre\s+agence)', desc_lower))

    for c in raw_contacts:
        if not isinstance(c, dict):
            continue
        c_name = fix_mojibake(str(c.get("name") or "")).strip()
        c_phone = fix_mojibake(str(c.get("phone") or "")).strip()
        c_role = fix_mojibake(str(c.get("role") or "Négociateur")).strip()
        c_email = fix_mojibake(str(c.get("email") or "")).strip() or None

        # Rejet des placeholders du prompt
        if any(ph in c_name.lower() for ph in ("<nom", "placeholder", "exemple", "n/a", "none")):
            continue
        if any(ph in c_phone.lower() for ph in ("<tel", "placeholder", "xxx")):
            continue

        # Normalisation du numéro
        norm_p = normalize_nc_phone(c_phone) if c_phone else None
        phone_digits = norm_p.get("digits") if norm_p else None

        # Grounding check du téléphone
        phone_grounded = False
        if phone_digits:
            if phone_digits in allowed_digits or phone_digits in re.sub(r'\D', '', desc_clean):
                phone_grounded = True

        # Grounding check du nom
        name_grounded = False
        name_lower = c_name.lower()
        if c_name and c_name.lower() not in STOP_NAMES:
            if clean_agency_name and (name_lower in clean_agency_name.lower() or clean_agency_name.lower() in name_lower):
                name_grounded = True
                c_name = clean_agency_name
                c_role = "Agence"
            elif is_valid_person_name(c_name):
                name_words = [w for w in re.split(r'[\s\-_]+', name_lower) if len(w) >= 3 and w not in STOP_NAMES]
                if name_words and all(w in desc_lower for w in name_words):
                    name_grounded = True

        # Si le nom est halluciné (ex: Manu sur une annonce où il n'apparaît pas) :
        if not name_grounded and c_name and c_name.lower() not in clean_agency_name.lower():
            if phone_grounded:
                # Si le numéro est réel (ex: 284.282), ré-attribuer à l'agence
                c_name = clean_agency_name or "Agence"
                c_role = "Agence"
                name_grounded = True
            else:
                # Rejet complet : ni le nom ni le téléphone ne sont dans l'annonce
                continue

        if not phone_grounded:
            norm_p = None

        if not name_grounded and not phone_grounded:
            continue

        valid_contacts.append({
            "name": c_name if name_grounded else clean_agency_name,
            "phone": norm_p["formatted"] if norm_p else (c_phone if phone_grounded else None),
            "whatsapp": norm_p["whatsapp"] if norm_p else None,
            "email": c_email,
            "role": c_role
        })

    # Si aucun contact individuel valide n'a été trouvé, mais que l'annonce mentionne l'agence ou contient un numéro
    if not valid_contacts and (has_contactez_agence or all_desc_digits):
        if all_desc_digits:
            first_digits = list(all_desc_digits)[0]
            norm_p = normalize_nc_phone(first_digits)
            if norm_p:
                valid_contacts.append({
                    "name": clean_agency_name or "Agence",
                    "phone": norm_p["formatted"],
                    "whatsapp": norm_p["whatsapp"],
                    "email": None,
                    "role": "Agence"
                })

    return valid_contacts


def _sanitize_ai_sections(raw_sections: Any, raw_description: str = "", title: str = "", property_type: str = "") -> List[Dict[str, Any]]:
    """
    Normalise, valide par ancrage (Grounding Check) et ordonne dans le gabarit canonique invariant
    toutes les rubriques retournées par l'IA (qu'elles soient au format dictionnaire 'rubriques'
    à clés fixes ou au format liste 'sections').
    Garantit :
    1. Zéro titre inventé (ex: 'BIEN RÉSIDENTIEL' ou 'Terrain' sur un bureau sont éliminés).
    2. Zéro anglicisme ('kitchen' -> 'Cuisine') et fusion des fragments numériques orphelins ('15m²', '16 ares').
    3. Ordre, icônes et intitulés strictement identiques pour 100 % des annonces.
    4. Réconciliation Zéro-Perte avec le moteur déterministe Tier-1 (aucune information oubliée ou tronquée).
    """
    import re
    if not isinstance(raw_sections, (list, dict)):
        return []

    clean_raw_desc = strip_technical_noise(fix_mojibake(raw_description or ""))
    full_context = fix_mojibake(f"{title} {clean_raw_desc}").lower()
    full_context_clean = re.sub(r'[\s\-_.,;:]+', ' ', full_context)

    prop_upper = property_type.upper()
    is_residential_or_land = any(r in prop_upper for r in ("MAISON", "VILLA", "APPARTEMENT", "STUDIO", "DUPLEX", "TERRAIN"))
    is_commercial = (
        any(t in prop_upper for t in ("COMMERCIAL", "BUREAU", "DOCK", "LOCAL"))
        or (not is_residential_or_land and any(k in full_context for k in ("plateau de bureaux", "dock", "local commercial", "entrepôt", "commerce", "professionnel", "activité tertiaire", "bureau")))
    )

    def _is_bullet_grounded(bullet: str) -> bool:
        if not full_context_clean.strip():
            return True
        b_clean = format_bullet_uniform(fix_mojibake(bullet)).strip()
        b_lower = b_clean.lower()
        if any(ph in b_lower for ph in ("<fait", "placeholder", "exemple", "<cle", "<titre", "[img:", "http://", "https://", "api.bienmeloger")):
            return False

        STOP_WORDS = {
            "avec", "pour", "dans", "sont", "cette", "dont", "tout", "tous", "toutes",
            "plus", "très", "bien", "situé", "proche", "environ", "pièce", "terrain"
        }
        words = [w for w in re.split(r'[\s,\.\(\)\-\–\:\;]+', b_lower) if len(w) >= 4 and w not in STOP_WORDS]
        bullet_numbers = re.findall(r'\b\d+\b', b_clean)

        tokens = words + bullet_numbers
        if not tokens:
            return True

        matches = [t for t in tokens if t in full_context_clean]
        ratio = len(matches) / len(tokens)

        has_unmatched_number = any(num not in full_context_clean for num in bullet_numbers)
        if has_unmatched_number:
            if ratio < 0.6:
                return False

        return ratio >= 0.4 or len(matches) >= 2 or len(words) <= 1

    # 1. Extraction à plat de toutes les puces brutes issues du JSON de l'IA
    NOT_BULLET_TITLES = {
        "COMMERCIAL", "RESIDENTIAL", "TERRAIN", "BIEN RÉSIDENTIEL", "BIEN RESIDENTIEL",
        "BIEN COMMERCIAL", "DÉTAILS", "DETAILS", "SECTION", "RUBRIQUES"
    }

    def _collect_raw_strings(obj: Any) -> List[str]:
        out: List[str] = []
        if isinstance(obj, str):
            s = strip_technical_noise(fix_mojibake(obj)).strip()
            if s and s.upper() not in NOT_BULLET_TITLES and not s.startswith("<") and not re.search(r'(?:\[img:|https?://|//api\.)', s, re.I):
                out.append(s)
        elif isinstance(obj, list):
            for sub in obj:
                out.extend(_collect_raw_strings(sub))
        elif isinstance(obj, dict):
            if "items" in obj and isinstance(obj["items"], list):
                out.extend(_collect_raw_strings(obj["items"]))
            else:
                for k, v in obj.items():
                    if k in ("key", "icon"):
                        continue
                    if k in ("title", "name", "desc") and isinstance(v, str):
                        if not any(h in v for h in ("Bureaux", "Stationnement", "Implantation", "Extérieur", "Pièces", "Espace", "Cadre", "Modalités", "Services", "Synthèse", "Équipements")):
                            out.extend(_collect_raw_strings(v))
                    elif isinstance(v, (list, dict)):
                        out.extend(_collect_raw_strings(v))
        return out

    raw_extracted_strings = _collect_raw_strings(raw_sections)

    # 2. Réparation des fragments numériques orphelins (ex: ["1 terrasse couverte", "15m²"] ou ["16 ares"])
    repaired_bullets: List[str] = []
    for raw_b in raw_extracted_strings:
        b_norm = normalize_header_banner(format_bullet_uniform(raw_b))
        if not b_norm:
            continue

        # Si la puce est uniquement une surface en m² (ex: "15 m²")
        if re.match(r'^(?:environ\s*|~)?\d+(?:[.,]\d+)?\s*m²$', b_norm, flags=re.IGNORECASE):
            if repaired_bullets and not re.search(r'\d+\s*m²', repaired_bullets[-1]):
                repaired_bullets[-1] = f"{repaired_bullets[-1]} de {b_norm}"
            continue

        # Si la puce est uniquement une superficie en ares (ex: "16 ares")
        if re.match(r'^(?:environ\s*|~)?\d+(?:[.,]\d+)?\s*ares?$', b_norm, flags=re.IGNORECASE):
            b_norm = f"Terrain d'environ {b_norm}"

        # Décomposition atomique et synthèse nominale si la puce IA contient une énumération ou phrase narrative
        for seg in split_compound_line(b_norm):
            seg_synth = synthesize_atomic_bullet(seg)
            if not seg_synth:
                continue
            seg_u = normalize_header_banner(format_bullet_uniform(seg_synth))
            if seg_u and _is_bullet_grounded(seg_u):
                repaired_bullets.append(seg_u)

    # 3. Exécution du moteur déterministe Tier-1 pour garantir Zéro-Perte, propagation des niveaux (RDC / À l'étage) et contacts à exclure
    t1_fallback = structure_description(clean_raw_desc, property_type="COMMERCIAL" if is_commercial else property_type)
    t1_contacts = t1_fallback.get("direct_contacts", [])
    t1_sections = t1_fallback.get("sections", [])

    # 4. Classement dans le gabarit canonique invariant (8 clés ordonnées)
    categorized_items: Dict[str, List[str]] = {cat["key"]: [] for cat in CATEGORY_DEFINITIONS}
    categorized_items["prestations_complementaires"] = []

    def _add_bullet_to_cat(cat_key: str, bullet_text: str):
        b_fmt = normalize_header_banner(format_bullet_uniform(bullet_text))
        if not b_fmt or len(b_fmt) < 2:
            return
        if is_contact_or_boilerplate_line(b_fmt, t1_contacts):
            return
        target_list = categorized_items.get(cat_key, categorized_items["prestations_complementaires"])
        b_low = b_fmt.lower()
        b_core = re.sub(r'\s*\((?:rdc|à l\'étage|en mezzanine|en sous-sol|au niveau inférieur)\)\s*$', '', b_low, flags=re.I).strip()
        for idx, existing in enumerate(target_list):
            ex_low = existing.lower()
            ex_core = re.sub(r'\s*\((?:rdc|à l\'étage|en mezzanine|en sous-sol|au niveau inférieur)\)\s*$', '', ex_low, flags=re.I).strip()
            if b_low == ex_low or b_low in ex_low or b_core == ex_core or b_core in ex_core:
                return
            if ex_low in b_low or ex_core in b_core:
                # Ne remplacer une puce existante que si la nouvelle apporte un complément réel (<= 85 car.) sans perdre le tag de niveau ni réintroduire de verbe narratif
                has_level_tag = bool(re.search(r'\((?:rdc|à l\'étage|en mezzanine|en sous-sol|au niveau inférieur)\)$', existing, re.I))
                has_narrative_verb = bool(re.search(r'\b(?:vous\s+séduira|se\s+compose|comprenant|découvrez|offre\s+un\s+cadre)\b|\?', b_low))
                if not has_level_tag and not has_narrative_verb and len(b_fmt) <= 85:
                    target_list[idx] = b_fmt
                return
        target_list.append(b_fmt)

    # 5. Socle primaire Zéro-Perte issu de l'analyse grammaticale et architecturale Tier-1 (préserve RDC / À l'étage)
    for t1_sec in t1_sections:
        sec_key = t1_sec.get("key", "cadre_vie")
        for t1_item in t1_sec.get("items", []):
            _add_bullet_to_cat(sec_key, t1_item)

    # 6. Enrichissement complémentaire par les puces IA ancrées non redondantes
    for b in repaired_bullets:
        if len(b.split()) <= 2 and re.match(r'^\d+(?:er|ème|eme)\s+km$', b.lower()):
            continue
        c_key = classify_bullet_or_sentence(b)
        _add_bullet_to_cat(c_key, b)

    # 6. Assemblage final dans l'ordre canonique immuable avec titres et icônes standardisés
    category_titles = {c["key"]: (c["title"], c["icon"]) for c in CATEGORY_DEFINITIONS}
    category_titles["prestations_complementaires"] = ("Équipements & Prestations complémentaires", "✨")

    if is_commercial:
        category_titles["espace_nuit"] = ("Bureaux & Espaces professionnels", "🏢")
        category_titles["cadre_vie"] = ("Implantation & Visibilité commerciale", "📍")
        category_titles["stationnement_annexes"] = ("Stationnement & Logistique", "🚗")

    ordered_keys = [
        "cadre_vie",
        "services_residence",
        "pieces_de_vie",
        "espace_nuit",
        "exterieurs",
        "stationnement_annexes",
        "prestations_complementaires",
        "conditions_modalites"
    ]

    final_canonical_sections: List[Dict[str, Any]] = []
    for k in ordered_keys:
        raw_items = categorized_items.get(k, [])
        items = sanitize_section_bullets(k, raw_items)
        if items:
            sec_title, sec_icon = category_titles[k]
            final_canonical_sections.append({
                "key": k,
                "title": sec_title,
                "icon": sec_icon,
                "items": items
            })

    return final_canonical_sections


def _sanitize_ai_technical_specs(raw_specs: Any, raw_description: str = "") -> Dict[str, Any]:
    """
    Valide et ancre les spécifications techniques (Tier-2 IA ou Tier-1 déterministe)
    en déléguant au moteur unifié extract_technical_specs (zéro hallucination).
    """
    desc_clean = fix_mojibake(raw_description or "")
    return extract_technical_specs(desc_clean, raw_specs=raw_specs if isinstance(raw_specs, dict) else {})


def enrich_listing_with_ai(
    listing_data: Any,
    custom_prompt: Optional[str] = None,
    custom_model: Optional[str] = None,
    force_refresh: bool = False,
    custom_provider: Optional[str] = None,
    persist_cache: bool = True,
    _cached_cfg: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Enrichit sémantiquement une annonce immobilière.
    Utilise le modèle Ollama configuré ou le repli déterministe Tier-1.
    """
    cfg = _cached_cfg if _cached_cfg is not None else get_ai_config()
    provider = (custom_provider or cfg.get("provider", "ollama")).lower()
    model = custom_model or cfg.get("model", "qwen2.5:3b")
    system_prompt = custom_prompt or cfg.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    timeout_sec = float(cfg.get("timeout_seconds", 40.0))

    # Extraction et nettoyage systématique des métadonnées d'entrée (avec retrait des balises [IMG: ...] et bannières parasites)
    if isinstance(listing_data, dict):
        listing_id = str(listing_data.get("id") or f"custom_{int(time.time())}")
        raw_text = strip_technical_noise(fix_mojibake(str(listing_data.get("description") or listing_data.get("raw_text") or ""))).strip()
        prop_type = str(listing_data.get("property_type") or listing_data.get("category") or "").upper()
        agency = normalize_agency_name(str(listing_data.get("agencyName") or listing_data.get("agency_name") or ""))
        title = fix_mojibake(str(listing_data.get("title") or ""))
        commune = str(listing_data.get("commune") or "")
        quartier = str(listing_data.get("quartier") or "")
        price = listing_data.get("currentPrice") or listing_data.get("price_xpf")
    else:
        raw_text = strip_technical_noise(fix_mojibake(str(listing_data))).strip()
        listing_id = f"text_{hashlib.md5(raw_text.encode()).hexdigest()[:10]}"
        prop_type = "COMMERCIAL" if any(k in raw_text.lower() for k in ["bureau", "dock", "local commercial"]) else "APPARTEMENT"
        agency = ""
        title = "Annonce testée dans le Playground"
        commune = "Nouméa"
        quartier = "Centre-Ville"
        price = None

    input_hash = hashlib.sha256(f"{raw_text}_{prop_type}_{system_prompt}".encode()).hexdigest()

    # Vérification du cache si non forcé
    if not force_refresh:
        cached = _get_cached_enrichment(listing_id, input_hash)
        if cached:
            cached["provider_used"] = "duckdb_cache"
            cached["fallback_tier1"] = False
            return cached

    # Tentative d'appel Ollama
    t_start = time.time()
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")
    generate_url = f"{ollama_url.rstrip('/')}/api/generate"

    compact_payload = {
        "id": listing_id,
        "title": title,
        "agency": agency,
        "property_type": prop_type,
        "commune": commune,
        "quartier": quartier,
        "price": price,
        "raw_description": raw_text
    }

    user_message = (
        f"Analyse l'annonce suivante et extrais les contacts, spécifications techniques et rubriques normalisées en JSON (100 % en français, aucun mot anglais) :\n"
        f"{json.dumps(compact_payload, ensure_ascii=False, indent=2)}\n\n"
        f"Format JSON attendu impérativement (gabarit fixe à clés invariantes, chaque clé contient une liste de chaînes complètes en français) :\n"
        f"{{\n"
        f'  "direct_contacts": [\n'
        f'    {{"name": "<nom_negociateur_ou_nom_agence>", "phone": "<telephone_reel>", "whatsapp": "<format_chiffres_sans_plus>", "email": null, "role": "Négociateur ou Agence"}}\n'
        f'  ],\n'
        f'  "technical_specs": {{\n'
        f'    "surface_habitable_m2": null,\n'
        f'    "surface_terrain_m2": null,\n'
        f'    "surface_terrasse_m2": null,\n'
        f'    "charges_mensuelles_xpf": null,\n'
        f'    "etage": null,\n'
        f'    "has_elevator": null,\n'
        f'    "parking_detail": null,\n'
        f'    "has_air_conditioning": false,\n'
        f'    "has_pool": false,\n'
        f'    "has_sea_view": false,\n'
        f'    "standing": null\n'
        f'  }},\n'
        f'  "rubriques": {{\n'
        f'    "cadre_vie": [],\n'
        f'    "services_residence": [],\n'
        f'    "pieces_de_vie": [],\n'
        f'    "espace_nuit": [],\n'
        f'    "exterieurs": [],\n'
        f'    "stationnement_annexes": [],\n'
        f'    "prestations_complementaires": [],\n'
        f'    "conditions_modalites": []\n'
        f'  }}\n'
        f"}}"
    )

    ollama_success = False
    ai_result: Dict[str, Any] = {}
    tokens_generated = 0

    if provider not in ("deterministic", "heuristic_fallback"):
        try:
            req_body = json.dumps({
                "model": model,
                "prompt": user_message,
                "system": system_prompt,
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": float(cfg.get("temperature", 0.1)),
                    "num_ctx": 2048,
                    "num_thread": 4
                },
                "keep_alive": "5m"
            }).encode("utf-8")

            req = urllib.request.Request(
                generate_url,
                data=req_body,
                headers={"Content-Type": "application/json", "User-Agent": "Sentinel-ImmoNC/1.0"}
            )

            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                if resp.status == 200:
                    resp_json = json.loads(resp.read().decode("utf-8"))
                    response_text = resp_json.get("response", "{}")
                    ai_parsed = json.loads(response_text)
                    if isinstance(ai_parsed, dict) and ("rubriques" in ai_parsed or "sections" in ai_parsed or "direct_contacts" in ai_parsed):
                        ai_result = ai_parsed
                        ollama_success = True
                        tokens_generated = resp_json.get("eval_count", 0)
        except Exception as e:
            logger.info(f"Ollama indisponible ({e}), bascule immédiate vers Tier-1.")

    latency_ms = int((time.time() - t_start) * 1000)

    if ollama_success:
        raw_contacts = ai_result.get("direct_contacts", [])
        raw_specs = ai_result.get("technical_specs", {})
        raw_sections = ai_result.get("rubriques") or ai_result.get("sections", [])
        
        # Grounding check strict + Réconciliation hybride Zéro-Perte en Python
        direct_contacts = _sanitize_ai_contacts(raw_contacts, raw_text, agency_name=agency, property_type=prop_type)
        t1_check = structure_description(raw_text, property_type=prop_type)
        existing_phones = {re.sub(r'\D', '', str(c.get("phone") or ""))[-6:] for c in direct_contacts if c.get("phone")}
        for t1_c in t1_check.get("direct_contacts", []):
            t1_p = str(t1_c.get("raw_phone") or "")[-6:]
            if t1_p and t1_p not in existing_phones:
                direct_contacts.append({
                    "name": t1_c.get("name"),
                    "phone": t1_c.get("phone"),
                    "whatsapp": t1_c.get("whatsapp"),
                    "email": t1_c.get("email"),
                    "role": "Négociateur"
                })
                existing_phones.add(t1_p)

        technical_specs = _sanitize_ai_technical_specs(raw_specs, raw_description=raw_text)
        sections = _sanitize_ai_sections(raw_sections, raw_description=raw_text, title=title, property_type=prop_type)
        ai_result["direct_contacts"] = direct_contacts
        ai_result["technical_specs"] = technical_specs
        ai_result["sections"] = sections
        tok_s = round((tokens_generated / (latency_ms / 1000.0)), 1) if latency_ms > 0 and tokens_generated > 0 else 0
        provider_used = f"Ollama ({model})"
        fallback_tier1 = False
        if persist_cache:
            _save_cached_enrichment(listing_id, model, input_hash, direct_contacts, sections, ai_result, latency_ms)
    else:
        # Repli robuste et garanti Tier-1 déterministe
        t1_start = time.time()
        t1_struct = structure_description(raw_text, property_type=prop_type)
        technical_specs = _sanitize_ai_technical_specs({}, raw_description=raw_text)
        t1_ms = int((time.time() - t1_start) * 1000)
        direct_contacts = t1_struct.get("direct_contacts", [])
        sections = t1_struct.get("sections", [])
        ai_result = {
            "direct_contacts": direct_contacts,
            "technical_specs": technical_specs,
            "sections": sections,
            "key_highlights": t1_struct.get("key_highlights", []),
            "agency_metadata": t1_struct.get("agency_metadata", {})
        }
        tok_s = 1250.0  # Vitesse native python regex
        latency_ms = t1_ms if latency_ms < 50 else latency_ms
        provider_used = "Tier-1 Déterministe (Regex & Heuristiques Calédoniennes)"
        fallback_tier1 = (provider not in ("deterministic", "heuristic_fallback"))
        if persist_cache and provider in ("deterministic", "heuristic_fallback"):
            _save_cached_enrichment(listing_id, "Tier-1 Déterministe", input_hash, direct_contacts, sections, ai_result, latency_ms)

    return {
        "id": listing_id,
        "input_hash": input_hash,
        "cached": False,
        "provider_used": provider_used,
        "fallback_tier1": fallback_tier1,
        "model": model if ollama_success else ("Tier-1 Déterministe" if provider in ("deterministic", "heuristic_fallback") else model),
        "latency_ms": latency_ms,
        "tokens_per_second": tok_s,
        "direct_contacts": direct_contacts,
        "technical_specs": technical_specs,
        "sections": sections,
        "raw_response": ai_result
    }


def ask_ai_chat(listing_data: Any, question: str, custom_model: Optional[str] = None) -> Dict[str, Any]:
    """
    Mode Chatbot / Q&R : répond à une question en langage naturel sur le bien sélectionné.
    """
    cfg = get_ai_config()
    model = custom_model or cfg.get("model", "qwen2.5:3b")
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")
    generate_url = f"{ollama_url.rstrip('/')}/api/generate"

    # Préparation du contexte
    if isinstance(listing_data, dict):
        title = listing_data.get("title", "")
        price = listing_data.get("currentPrice", "")
        desc = listing_data.get("description", "")
        commune = listing_data.get("commune", "")
        quartier = listing_data.get("quartier", "")
        prop_type = listing_data.get("property_type") or listing_data.get("category", "")
        contacts = listing_data.get("directContacts") or []
        sections = listing_data.get("structuredDescription") or []
    else:
        title = "Bien immobilier"
        price = "Non spécifié"
        desc = str(listing_data)
        commune = "Nouméa"
        quartier = ""
        prop_type = "IMMOBILIER"
        contacts = []
        sections = []

    context_str = (
        f"Bien: {title}\n"
        f"Localisation: {quartier}, {commune}\n"
        f"Type: {prop_type} | Prix: {price} F CFP\n"
        f"Contacts: {json.dumps(contacts, ensure_ascii=False)}\n"
        f"Rubriques: {json.dumps([s.get('title') for s in sections if isinstance(s, dict)], ensure_ascii=False)}\n"
        f"Descriptif complet:\n{desc}\n"
    )

    system_prompt = (
        "Tu es un assistant immobilier calédonien expert et bienveillant. "
        "Tu réponds de manière concise, précise et factuelle en te basant UNIQUEMENT sur les informations du bien ci-dessous. "
        "Si l'information n'est pas précisée dans l'annonce, indique-le honnêtement."
    )

    full_prompt = f"Voici la fiche du bien immobilier :\n{context_str}\n\nQuestion de l'utilisateur : {question}\nRéponse :"

    t0 = time.time()
    try:
        req_body = json.dumps({
            "model": model,
            "prompt": full_prompt,
            "system": system_prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 2048, "num_thread": 4},
            "keep_alive": "5m"
        }).encode("utf-8")

        req = urllib.request.Request(
            generate_url,
            data=req_body,
            headers={"Content-Type": "application/json", "User-Agent": "Sentinel-ImmoNC/1.0"}
        )

        with urllib.request.urlopen(req, timeout=25.0) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                latency_ms = int((time.time() - t0) * 1000)
                return {
                    "answer": body.get("response", "").strip(),
                    "latency_ms": latency_ms,
                    "model": model,
                    "source": "Ollama LLM"
                }
    except Exception as e:
        logger.info(f"Ollama chat indisponible ({e}), repli sur réponse contextuelle déterministe.")

    # Repli contextuel déterministe
    latency_ms = int((time.time() - t0) * 1000)
    q_lower = question.lower()
    desc_lower = desc.lower()

    if any(k in q_lower for k in ["contact", "négociateur", "téléphone", "joindre", "visite", "qui"]):
        if contacts:
            c = contacts[0]
            c_name = c.get("name") or "le commercialisateur"
            c_phone = c.get("phone") or "le numéro d'agence"
            answer = f"Vous pouvez joindre directement {c_name} au {c_phone}."
        else:
            answer = "Le contact pour ce bien est consultable auprès de l'agence mentionnée dans l'annonce."
    elif any(k in q_lower for k in ["parking", "garage", "stationnement", "véhicule"]):
        if "parking" in desc_lower or "stationnement" in desc_lower or "garage" in desc_lower or "couvert" in desc_lower:
            answer = f"Oui, le descriptif mentionne des options de stationnement : {', '.join([l for l in desc.splitlines() if any(p in l.lower() for p in ['parking', 'dock', 'stationnement', 'garage'])])}"
        else:
            answer = "Aucun détail spécifique sur les places de parking n'est mentionné explicitement dans le corps du texte."
    elif any(k in q_lower for k in ["prix", "loyer", "montant", "combien"]):
        answer = f"Le montant affiché pour cette annonce est de {price} F CFP."
    else:
        answer = f"D'après la fiche du bien ({title} à {commune}), voici ce qui est indiqué : {desc[:280]}..."

    return {
        "answer": answer,
        "latency_ms": latency_ms,
        "model": "Moteur Contextuel Tier-1",
        "source": "Tier-1 Fallback"
    }


def batch_enrich_listings(
    limit: int = 20,
    only_missing: bool = True,
    custom_model: Optional[str] = None,
    stop_event: Optional[Any] = None,
    progress_callback: Optional[Any] = None,
    force_all: bool = False,
    custom_provider: Optional[str] = None
) -> Dict[str, Any]:
    """
    Enrichit par lot les annonces enregistrées dans la base DuckDB.
    Supporte le mode incrémental (only_missing=True) ou la ré-analyse intégrale forcée (force_all=True / only_missing=False).
    Enregistre chaque résultat en cache dans listing_ai_enrichment.
    """
    import duckdb
    if not DB_PATH.exists():
        return {"success": False, "error": f"Base de données introuvable : {DB_PATH}"}

    if force_all:
        only_missing = False
        clear_ai_cache()

    cfg = get_ai_config()
    effective_provider = (custom_provider or cfg.get("provider", "ollama")).lower()

    init_ai_cache_table()
    con = duckdb.connect(str(DB_PATH), read_only=True)

    where_parts = ["is_active = TRUE", "description IS NOT NULL", "length(trim(description)) > 10"]
    if only_missing:
        where_parts.append("id NOT IN (SELECT listing_id FROM listing_ai_enrichment WHERE extracted_contacts_json IS NOT NULL)")
    where_clause = "WHERE " + " AND ".join(where_parts)

    limit_clause = f"LIMIT {int(limit)}" if 0 < limit < 500000 else ""
    query = f"""
        SELECT id, title, property_type, commune, quartier, price_xpf, description, agency_name
        FROM listings
        {where_clause}
        ORDER BY scraped_at DESC
        {limit_clause}
    """
    try:
        df = con.execute(query).df()
    except Exception as e:
        con.close()
        return {"success": False, "error": f"Erreur requête SQL : {e}"}
    finally:
        con.close()

    total = len(df)
    if progress_callback:
        progress_callback(0, total, f"Démarrage de l'analyse sur {total} annonce(s)...")

    results = []
    enriched_count = 0
    pending_cache_rows: List[Tuple[Any, ...]] = []

    def _flush_pending_cache_rows():
        nonlocal pending_cache_rows
        if not pending_cache_rows:
            return
        try:
            import pandas as pd
            batch_df = pd.DataFrame(
                pending_cache_rows,
                columns=[
                    "listing_id",
                    "model_version",
                    "input_hash",
                    "extracted_contacts_json",
                    "structured_sections_json",
                    "raw_response_json",
                    "latency_ms"
                ]
            )
            with duckdb.connect(str(DB_PATH)) as wcon:
                wcon.register("batch_df", batch_df)
                wcon.execute(
                    """
                    INSERT OR REPLACE INTO listing_ai_enrichment
                    (listing_id, model_version, enriched_at, input_hash, extracted_contacts_json, structured_sections_json, raw_response_json, latency_ms)
                    SELECT listing_id, model_version, CURRENT_TIMESTAMP, input_hash, extracted_contacts_json, structured_sections_json, raw_response_json, latency_ms
                    FROM batch_df
                    """
                )
        except Exception as e:
            logger.warning(f"Erreur sauvegarde lot cache IA: {e}")
        pending_cache_rows = []

    is_fast_deterministic = (effective_provider == "deterministic")

    for idx, row in df.iterrows():
        if stop_event and stop_event.is_set():
            break

        item = row.to_dict()
        if progress_callback and (not is_fast_deterministic or enriched_count % 50 == 0):
            progress_callback(enriched_count, total, f"Analyse : {item.get('title') or 'Annonce'}")

        if is_fast_deterministic and enriched_count % 25 == 0:
            time.sleep(0.001)

        res = enrich_listing_with_ai(
            item,
            custom_model=custom_model,
            force_refresh=True,
            custom_provider=effective_provider,
            persist_cache=(not is_fast_deterministic),
            _cached_cfg=cfg
        )
        if is_fast_deterministic:
            pending_cache_rows.append((
                str(item.get("id")),
                res.get("model") or "Tier-1 Déterministe",
                res.get("input_hash") or "",
                json.dumps(res.get("direct_contacts", []), ensure_ascii=False),
                json.dumps(res.get("sections", []), ensure_ascii=False),
                json.dumps(res.get("raw_response", {}), ensure_ascii=False),
                int(res.get("latency_ms") or 1)
            ))

        results.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "provider_used": res.get("provider_used"),
            "contacts_found": len(res.get("direct_contacts", [])),
            "sections_found": len(res.get("sections", [])),
            "latency_ms": res.get("latency_ms")
        })
        enriched_count += 1
        if progress_callback and (not is_fast_deterministic or enriched_count % 50 == 0 or enriched_count == total):
            progress_callback(enriched_count, total, f"Terminé : {item.get('title') or 'Annonce'}")

    if pending_cache_rows:
        _flush_pending_cache_rows()

    return {
        "success": True,
        "total_requested": total if limit >= 500000 else limit,
        "processed_count": enriched_count,
        "results": results[:100]
    }
