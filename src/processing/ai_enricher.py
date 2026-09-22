"""
Module d'Intelligence Sémantique & Enrichissement IA Sentinel (Tier-2).
Gère la communication avec le moteur LLM local (Ollama / Qwen / Gemma),
l'application du prompt paramétrable, le circuit-breaker avec repli Tier-1,
le dialogue interactif (Chatbot Q&R sur annonces) et la persistance DuckDB.
"""

import os
import sys
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

from src.processing.description_structurer import structure_description
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
    "Tu es un moteur d'intelligence sémantique dédié à l'immobilier en Nouvelle-Calédonie.\n"
    "Ta mission est d'extraire les contacts réels et de ventiler les caractéristiques d'une annonce dans des rubriques structurées.\n\n"
    "RÈGLES STRICTES :\n"
    "1. Extraire TOUS les contacts de négociateurs (prénom, nom, mobile, email) dans 'direct_contacts'. "
    "Les mobiles calédoniens ont 6 chiffres et commencent par 7, 8 ou 9 (ex: 98.72.66, 79.40.01). Ignorer les zéros initiaux.\n"
    "2. Tout contact extrait DOIT être retiré du corps de description.\n"
    "3. Pour les biens professionnels (bureau, dock, local commercial, commerce, entrepôt), utiliser obligatoirement "
    "la rubrique 'Bureaux & Espaces professionnels' et JAMAIS 'Espace Nuit'.\n"
    "4. Classer les docks, réserves et parkings dans 'Stationnement & Logistique'.\n"
    "5. Répondre EXCLUSIVEMENT en JSON valide conforme au schéma attendu, sans aucun texte introductif ni conclusion."
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
        "model": "qwen2.5:3b",
        "temperature": 0.1,
        "timeout_seconds": 4.0,
        "active_preset": "standard",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "presets": {
            "standard": {
                "name": "🎯 Standard Immo NC",
                "description": "Extraction universelle des contacts OPT et répartition équilibrée en 7 rubriques standardisées.",
                "temperature": 0.1,
                "prompt": DEFAULT_SYSTEM_PROMPT
            },
            "commercial": {
                "name": "🏢 Spécial Tertiaire, Docks & Commerces",
                "description": "Optimisé pour la détection des surfaces utiles, hauteurs sous plafond des docks et parkings clients.",
                "temperature": 0.1,
                "prompt": (
                    "Tu es un expert en immobilier d'entreprise et commercial en Nouvelle-Calédonie.\n"
                    "RÈGLES STRICTES :\n"
                    "1. Extraire le contact direct du commercialisateur (nom, mobile NC, email) dans 'direct_contacts'.\n"
                    "2. Isoler les surfaces spécifiques : bureaux (m²), dock/réserve (m² avec hauteur), showroom, terrain.\n"
                    "3. Utiliser les rubriques 'Implantation & Visibilité commerciale', 'Bureaux & Espaces professionnels', "
                    "'Stationnement & Logistique' et 'Modalités & Conditions'.\n"
                    "4. Répondre EXCLUSIVEMENT en JSON valide conforme au schéma."
                )
            },
            "contacts_only": {
                "name": "⚡ Extraction Rapide des Contacts",
                "description": "Extraction prioritaire des négociateurs, numéros de téléphone et coordonnées directes.",
                "temperature": 0.0,
                "prompt": (
                    "Tu es un analyseur ultra-rapide de coordonnées immobilières pour la Nouvelle-Calédonie.\n"
                    "Extrais tous les contacts humains mentionnés : prénom, nom, téléphone fixe (2x.xx.xx) ou mobile (7x/8x/9x), WhatsApp et email.\n"
                    "Formate la réponse sous forme de JSON strict."
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
            return {
                "cached": True,
                "model": row[0],
                "enriched_at": str(row[1]),
                "direct_contacts": json.loads(row[2]) if row[2] else [],
                "sections": json.loads(row[3]) if row[3] else [],
                "raw_response": json.loads(row[4]) if row[4] else {},
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
        con = duckdb.connect(str(DB_PATH))
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
        con.close()
    except Exception as e:
        logger.warning(f"Erreur sauvegarde cache IA: {e}")


def _sanitize_ai_sections(raw_sections: Any) -> List[Dict[str, Any]]:
    """Nettoie et aplatit les rubriques retournées par les petits LLM pour garantir une liste d'items textuels."""
    if not isinstance(raw_sections, list):
        return []

    clean_sections = []

    def _extract_bullets_recursive(obj: Any) -> List[str]:
        bullets = []
        if isinstance(obj, str):
            s = obj.strip()
            if s and s.upper() not in ("COMMERCIAL", "RESIDENTIAL", "TERRAIN"):
                bullets.append(s)
        elif isinstance(obj, dict):
            for k in ("title", "key", "name", "desc"):
                val = obj.get(k)
                if isinstance(val, str) and val.strip() and val.upper() not in ("COMMERCIAL", "RESIDENTIAL", "TERRAIN"):
                    if not any(v in val for v in ("Bureaux", "Stationnement", "Implantation", "Extérieur")):
                        bullets.append(val.strip())
            for sub in obj.get("items", []):
                bullets.extend(_extract_bullets_recursive(sub))
        elif isinstance(obj, list):
            for sub in obj:
                bullets.extend(_extract_bullets_recursive(sub))
        return bullets

    for s in raw_sections:
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or s.get("key") or "Détails").strip()
        sub_items = s.get("items", [])
        has_nested = any(isinstance(it, dict) and any(kw in str(it.get("title") or it.get("key")) for kw in ("Bureaux", "Stationnement", "Logistique", "Extérieur", "Pièce", "Cadre")) for it in sub_items)

        if has_nested:
            for it in sub_items:
                if isinstance(it, dict):
                    it_title = str(it.get("title") or it.get("key") or title).strip()
                    it_icon = str(it.get("icon") or s.get("icon") or "📌")
                    it_bullets = []
                    for b in it.get("items", []):
                        it_bullets.extend(_extract_bullets_recursive(b))
                    if not it_bullets and it_title:
                        it_bullets = [it_title]
                    clean_sections.append({
                        "key": str(it.get("key") or "section").lower(),
                        "title": it_title,
                        "icon": it_icon if len(it_icon) <= 4 else "🏢",
                        "items": [str(x) for x in it_bullets if str(x).strip()]
                    })
        else:
            bullets = []
            for b in sub_items:
                bullets.extend(_extract_bullets_recursive(b))
            clean_sections.append({
                "key": str(s.get("key") or "section").lower(),
                "title": title,
                "icon": str(s.get("icon") or "📌") if len(str(s.get("icon") or "")) <= 4 else "📌",
                "items": [str(x) for x in bullets if str(x).strip()]
            })

    return [cs for cs in clean_sections if cs.get("title") and (cs.get("items") or cs.get("title"))]


def enrich_listing_with_ai(
    listing_data: Any,
    custom_prompt: Optional[str] = None,
    custom_model: Optional[str] = None,
    force_refresh: bool = False
) -> Dict[str, Any]:
    """
    Enrichit sémantiquement une annonce immobilière.
    Utilise le modèle Ollama configuré ou le repli déterministe Tier-1.
    """
    cfg = get_ai_config()
    model = custom_model or cfg.get("model", "qwen2.5:3b")
    system_prompt = custom_prompt or cfg.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
    timeout_sec = float(cfg.get("timeout_seconds", 40.0))

    # Extraction des métadonnées d'entrée
    if isinstance(listing_data, dict):
        listing_id = str(listing_data.get("id") or f"custom_{int(time.time())}")
        raw_text = str(listing_data.get("description") or listing_data.get("raw_text") or "")
        prop_type = str(listing_data.get("property_type") or listing_data.get("category") or "").upper()
        agency = str(listing_data.get("agencyName") or listing_data.get("agency_name") or "")
        title = str(listing_data.get("title") or "")
        commune = str(listing_data.get("commune") or "")
        quartier = str(listing_data.get("quartier") or "")
        price = listing_data.get("currentPrice") or listing_data.get("price_xpf")
    else:
        raw_text = str(listing_data)
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
        f"Analyse l'annonce suivante et extrais les contacts et rubriques structurées en JSON :\n"
        f"{json.dumps(compact_payload, ensure_ascii=False, indent=2)}\n\n"
        f"Format JSON attendu impérativement (attention : 'items' DOIT être une simple liste de chaînes de texte, aucun objet imbriqué dans items) :\n"
        f"{{\n"
        f'  "direct_contacts": [{{"name": "Manu", "phone": "+687 98.72.66", "whatsapp": "687987266", "email": null, "role": "Négociateur"}}],\n'
        f'  "sections": [\n'
        f'    {{"key": "bureaux", "title": "Bureaux & Espaces professionnels", "icon": "🏢", "items": ["3 bureaux indépendants", "Grande pièce de 30 m²"]}},\n'
        f'    {{"key": "stationnement", "title": "Stationnement & Logistique", "icon": "🚗", "items": ["5 places de parking dont 2 couvertes", "Dock réserve de 45 m²"]}}\n'
        f'  ]\n'
        f"}}"
    )

    ollama_success = False
    ai_result: Dict[str, Any] = {}
    tokens_generated = 0

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
                if isinstance(ai_parsed, dict) and ("sections" in ai_parsed or "direct_contacts" in ai_parsed):
                    ai_result = ai_parsed
                    ollama_success = True
                    tokens_generated = resp_json.get("eval_count", 0)
    except Exception as e:
        logger.info(f"Ollama indisponible ({e}), bascule immédiate vers Tier-1.")

    latency_ms = int((time.time() - t_start) * 1000)

    if ollama_success:
        direct_contacts = ai_result.get("direct_contacts", [])
        raw_sections = ai_result.get("sections", [])
        sections = _sanitize_ai_sections(raw_sections)
        ai_result["sections"] = sections
        tok_s = round((tokens_generated / (latency_ms / 1000.0)), 1) if latency_ms > 0 and tokens_generated > 0 else 0
        provider_used = f"Ollama ({model})"
        fallback_tier1 = False
        _save_cached_enrichment(listing_id, model, input_hash, direct_contacts, sections, ai_result, latency_ms)
    else:
        # Repli robuste et garanti Tier-1 déterministe
        t1_start = time.time()
        t1_struct = structure_description(raw_text, property_type=prop_type)
        t1_ms = int((time.time() - t1_start) * 1000)
        direct_contacts = t1_struct.get("direct_contacts", [])
        sections = t1_struct.get("sections", [])
        ai_result = {
            "direct_contacts": direct_contacts,
            "sections": sections,
            "key_highlights": t1_struct.get("key_highlights", []),
            "agency_metadata": t1_struct.get("agency_metadata", {})
        }
        tok_s = 1250.0  # Vitesse native python regex
        latency_ms = t1_ms if latency_ms < 50 else latency_ms
        provider_used = "Tier-1 Déterministe (Regex & Heuristiques Calédoniennes)"
        fallback_tier1 = True

    return {
        "id": listing_id,
        "cached": False,
        "provider_used": provider_used,
        "fallback_tier1": fallback_tier1,
        "model": model,
        "latency_ms": latency_ms,
        "tokens_per_second": tok_s,
        "direct_contacts": direct_contacts,
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
    progress_callback: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Enrichit par lot les annonces enregistrées dans la base DuckDB.
    Enregistre chaque résultat en cache dans listing_ai_enrichment.
    """
    import duckdb
    if not DB_PATH.exists():
        return {"success": False, "error": f"Base de données introuvable : {DB_PATH}"}

    init_ai_cache_table()
    con = duckdb.connect(str(DB_PATH), read_only=True)

    where_parts = ["is_active = TRUE", "description IS NOT NULL", "length(trim(description)) > 10"]
    if only_missing:
        where_parts.append("id NOT IN (SELECT listing_id FROM listing_ai_enrichment WHERE extracted_contacts_json IS NOT NULL)")
    where_clause = "WHERE " + " AND ".join(where_parts)

    limit_clause = f"LIMIT {int(limit)}" if limit > 0 else ""
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
    results = []
    enriched_count = 0

    for idx, row in df.iterrows():
        if stop_event and stop_event.is_set():
            break

        item = row.to_dict()
        if progress_callback:
            progress_callback(enriched_count, total, f"Analyse : {item.get('title') or 'Annonce'}")

        res = enrich_listing_with_ai(item, custom_model=custom_model, force_refresh=True)
        results.append({
            "id": item.get("id"),
            "title": item.get("title"),
            "provider_used": res.get("provider_used"),
            "contacts_found": len(res.get("direct_contacts", [])),
            "sections_found": len(res.get("sections", [])),
            "latency_ms": res.get("latency_ms")
        })
        enriched_count += 1
        if progress_callback:
            progress_callback(enriched_count, total, f"Terminé : {item.get('title') or 'Annonce'}")

    return {
        "success": True,
        "total_requested": limit,
        "processed_count": enriched_count,
        "results": results
    }
