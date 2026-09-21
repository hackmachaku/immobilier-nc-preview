"""
Module de structuration intelligente des descriptions d'annonces immobilières NC.
Applique le paradigme « Extraire et Router, ne jamais détruire » :
1. Extrait les contacts directs des négociateurs (noms + mobiles calédoniens) pour le bloc contact.
2. Nettoie la pollution typographique (ponctuations multiples, slogans tapageurs, puces hétéroclites).
3. Classe chaque information dans un ordre logique immuable en 6 rubriques standardisées.
4. Filet de sécurité absolu : aucune phrase informative n'est perdue.
"""

import re
from typing import Dict, List, Any, Optional

# Mots vides à ne pas considérer comme des noms de négociateurs
STOP_NAMES = {
    'appartement', 'maison', 'villa', 'terrain', 'dock', 'bureau', 'local',
    'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche',
    'agence', 'immo', 'immobilier', 'site', 'mail', 'email', 'merci', 'info',
    'notre', 'votre', 'visite', 'visites', 'contact', 'pour', 'avec', 'dans',
    'urgent', 'exclusivite', 'nouveau', 'prix', 'loyer', 'charges', 'tel', 'tél',
    'mobile', 'telephone', 'téléphone', 'bien', 'secteur', 'rdv', 'rendez-vous',
    'négociation', 'negociation', 'négociations', 'negociations', 'bureau', 'bail',
    'internet', 'lien', 'plus', 'toutes', 'tous', 'candidature', 'dossier'
}

# Slogans publicitaires sans valeur informative à filtrer
BOILERPLATE_PATTERNS = [
    re.compile(r'^\s*(?:✨|❗️|⭐|💥|🔥)?\s*(?:a\s+saisir|a\s+voir\s+absolument|coup\s+de\s+c[oœ]ur|exclusivit[eé]|urgent|nouveaut[eé]|a\s+visiter\s+rapidement|a\s+ne\s+pas\s+manquer|rare\s+sur\s+le\s+secteur|opportunit[eé]\s+[aà]\s+saisir)\s*(?:✨|❗️|⭐|💥|🔥)?\s*$', re.IGNORECASE),
    re.compile(r'^\s*il\s+comprend\s*:?\s*$', re.IGNORECASE),
    re.compile(r'^\s*description\s*:?\s*$', re.IGNORECASE),
    re.compile(r'^\s*composition\s*:?\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:lien\s+site\s+internet|site\s+web)\s*:?\s*https?://\S+\s*$', re.IGNORECASE),
]

# Dictionnaires sémantiques par rubrique ordonnée
CATEGORY_DEFINITIONS = [
    {
        "key": "cadre_vie",
        "title": "Cadre de vie & Résidence",
        "icon": "🏛️",
        "keywords": [
            "résidence", "residence", "résidentiel", "residentiel", "sécurisée", "securisee",
            "bien entretenue", "calme", "environnement", "étage", "etage", "ascenseur",
            "quartier", "proche", "commodités", "commodites", "école", "ecole", "commerces",
            "centre-ville", "centre ville", "marché", "marche", "emplacement", "vue",
            "situation", "plain-pied", "plain pied", "copropriété", "copropriete", "standing",
            "digicode", "interphone", "gardien", "immeuble"
        ]
    },
    {
        "key": "pieces_de_vie",
        "title": "Pièces de vie (Séjour & Cuisine)",
        "icon": "🛋️",
        "keywords": [
            "séjour", "sejour", "salon", "cuisine", "salle à manger", "salle a manger",
            "coin cuisine", "pièce de vie", "piece de vie", "espace de vie", "buanderie",
            "arrière-cuisine", "arriere cuisine", "cellier intérieur", "cellier attenant",
            "baie vitrée", "baie vitree", "baies vitrées", "baies vitrees", "îlot", "ilot",
            "plaque", "four", "hotte", "aménagée", "amenagee", "équipée", "equipee",
            "lumineux", "lumineuse", "carrelé", "carrele", "parquet", "hauteur sous plafond"
        ]
    },
    {
        "key": "espace_nuit",
        "title": "Espace Nuit & Sanitaires",
        "icon": "🛏️",
        "keywords": [
            "chambre", "suite parentale", "parentale", "dressing", "placard", "placards",
            "penderie", "salle d'eau", "salle d eau", "salle de bain", "salle de bains",
            "sdb", "sde", "douche", "baignoire", "vasque", "wc", "toilette", "toilettes",
            "climatisé", "climatise", "climatisée", "climatisee", "climatisées", "climatisees",
            "clim", "climatisation", "split", "volet", "volets"
        ]
    },
    {
        "key": "exterieurs",
        "title": "Extérieurs & Détente",
        "icon": "🌿",
        "keywords": [
            "terrasse", "varangue", "jardin", "balcon", "deck", "piscine", "bassin",
            "cour", "terrain", "arboré", "arbore", "clôturé", "cloture", "vue mer",
            "vue lagon", "vue dégagée", "vue degagee", "vue montagne", "vue imprenable",
            "store", "pergola", "patio", "fare", "barbecue", "espace extérieur", "espaces extérieurs",
            "extérieur", "exterieur", "extérieurs", "exterieurs"
        ]
    },
    {
        "key": "stationnement_annexes",
        "title": "Stationnement & Annexes",
        "icon": "🚗",
        "keywords": [
            "parking", "parkings", "stationnement", "stationnements", "garage", "garages",
            "carport", "box", "cellier", "celliers", "dock", "atelier", "remise", "hangar",
            "abri voiture", "place couverte", "places couvertes", "place sécurisée", "places sécurisées"
        ]
    },
    {
        "key": "conditions_modalites",
        "title": "Modalités & Conditions",
        "icon": "📋",
        "keywords": [
            "loyer", "charges", "dépôt de garantie", "depot de garantie", "caution",
            "bail", "disponible", "disponibilité", "disponibilite", "honoraires",
            "garant", "candidature", "dossier", "entrée", "entree", "option", "meublé",
            "non meublé", "non-meublé", "charges comprises", "hors charges"
        ]
    }
]


def extract_direct_contacts(text: str) -> List[Dict[str, str]]:
    """
    Extrait les contacts directs (négociateurs, conseillers) avec prénom/nom et mobile calédonien (6 chiffres).
    Retourne une liste de dictionnaires avec name, phone, whatsapp et raw_phone.
    """
    if not text:
        return []

    # Regex de détection des motifs de contact nominatifs
    patterns = [
        # "visites et négociations avec Tim au 85.46.55 ou Séverine au 90.57.97"
        re.compile(r'(?:visites?\s*(?:et\s*n[ée]gociations?)?\s*)?(?:avec|contacter|joindre|appeler|demander)\s+([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s+(?:au|t[ée]l\s*:?|mobile\s*:?|le)\s*(?:(?:\+687|00687)\s*)?([7-9]\d(?:[\s\.\-]?[0-9]{2}){2})', re.IGNORECASE),
        # "Tim au 85.46.55"
        re.compile(r'\b([A-ZÀ-Ÿ][a-zà-ÿ\-]+)\s+au\s+(?:(?:\+687|00687)\s*)?([7-9]\d(?:[\s\.\-]?[0-9]{2}){2})'),
        # "Contact : Marc DUPONT : 82.30.40"
        re.compile(r'(?:contact|conseill[eè]re?|n[ée]gociat(?:eur|rice)|r[ée]f[ée]rent|agent)\s*:?\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s*(?:au|:|\(|\-)?\s*(?:(?:\+687|00687)\s*)?([7-9]\d(?:[\s\.\-]?[0-9]{2}){2})', re.IGNORECASE),
        # "85.46.55 (Tim)"
        re.compile(r'(?:(?:\+687|00687)\s*)?([7-9]\d(?:[\s\.\-]?[0-9]{2}){2})\s*\(\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+)\s*\)', re.IGNORECASE),
    ]

    contacts: List[Dict[str, str]] = []
    seen_phones = set()

    for pat in patterns:
        for m in pat.finditer(text):
            g1, g2 = m.group(1).strip(), m.group(2).strip()
            # Si g1 est le numéro (motif 4)
            if re.match(r'^[7-9]\d', g1):
                phone_raw, name_raw = g1, g2
            else:
                name_raw, phone_raw = g1, g2

            # Nettoyage du prénom / nom
            clean_name = re.sub(r'^(?:ez|er|avec|au|de|le|la|du|un|une)\s+', '', name_raw, flags=re.IGNORECASE).strip()
            if clean_name.lower() in STOP_NAMES or len(clean_name) < 2:
                continue

            # Nettoyage du numéro
            digits = re.sub(r'[^\d]', '', phone_raw)
            if len(digits) > 6 and digits.startswith('687'):
                digits = digits[3:]
            if len(digits) != 6 or digits[0] not in ('7', '8', '9'):
                continue

            if digits in seen_phones:
                continue
            seen_phones.add(digits)

            formatted_phone = f"+687 {digits[:2]}.{digits[2:4]}.{digits[4:6]}"
            whatsapp_num = f"687{digits}"

            contacts.append({
                "name": clean_name.title(),
                "phone": formatted_phone,
                "whatsapp": whatsapp_num,
                "raw_phone": digits
            })

    return contacts


def clean_line_typography(line: str) -> str:
    """
    Nettoie la typographie d'une ligne ou puce :
    - Supprime les puces hétérogènes (✅, ✨, ❗️, •, -, *).
    - Réduit les ponctuations excessives (!!!!!!! -> !, ???? -> ?).
    - Supprime les espaces superflus.
    """
    if not line:
        return ""

    # Retrait des puces hétérogènes et emojis de liste en début de ligne (supporte les emojis répétés)
    clean = re.sub(r'^\s*(?:[✅✨❗️⭐💥🔥•\-\*👉✔►▪▫–—#\ufe0f]|(?:\[x\]|\(x\)))+\s*', '', line)
    # Retrait des emojis décoratifs en fin de ligne
    clean = re.sub(r'\s*(?:[✅✨❗️⭐💥🔥•\-\*👉✔►▪▫–—#\ufe0f]|(?:\[x\]|\(x\)))+\s*$', '', clean)

    # Réduction des ponctuations multiples
    clean = re.sub(r'!{2,}', '!', clean)
    clean = re.sub(r'\?{2,}', '?', clean)
    clean = re.sub(r'\.{3,}', '...', clean)

    # Nettoyage des espaces
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


def is_contact_or_boilerplate_line(line: str, direct_contacts: List[Dict[str, str]]) -> bool:
    """Détecte si une ligne est un slogan publicitaire ou une annonce de contact déjà routée."""
    if not line:
        return True

    clean = line.strip()
    clean_no_emoji = re.sub(r'[^\w\s]', ' ', clean).strip().lower()

    # Slogans publicitaires épurés
    slogans = {
        'a saisir', 'a voir absolument', 'coup de coeur', 'coup de coeur assure',
        'exclusivite', 'urgent', 'nouveaute', 'a visiter rapidement',
        'a ne pas manquer', 'rare sur le secteur', 'opportunite a saisir',
        'il comprend', 'description', 'composition', 'a visiter absolument'
    }
    if clean_no_emoji in slogans:
        return True

    for pat in BOILERPLATE_PATTERNS:
        if pat.match(clean) or pat.match(clean_no_emoji):
            return True

    # Vérifier si la ligne mentionne les contacts extraits (ex: "Visites avec Tim au 85.46.55")
    for contact in direct_contacts:
        if contact["raw_phone"] in clean or contact["name"].lower() in clean.lower():
            if re.search(r'(?:visite|visiter|négociation|contact|joindre|appeler|tél|tel|mobile)', clean, re.IGNORECASE):
                return True

    # Détecteur général de ligne de contact téléphonique résiduelle
    if re.search(r'(?:visite|visiter|contacter|joindre|appeler)\s+.*(?:au|t[ée]l)?\s*[7-9]\d[\s.]?\d{2}[\s.]?\d{2}', clean, re.IGNORECASE):
        return True

    return False


def classify_bullet_or_sentence(text: str) -> str:
    """
    Classe une phrase ou ligne dans l'une des 6 rubriques à l'aide d'un score lexical pondéré.
    Retourne la clé de catégorie correspondante.
    """
    norm = text.lower()

    # Priorité 0 : Cadre de vie & Présentation d'ensemble (intro de résidence/quartier)
    if re.search(r'\b(?:dans\s+une\s+r[ée]sidence|au\s+c[oœ]ur\s+d|situ[ée]e?\s+dans|quartier\s+r[ée]sidentiel|venez\s+d[ée]couvrir|bel\s+appartement|belle\s+villa|joli\s+studio|emplacement\s+central|proche\s+de\s+toutes)\b', norm):
        return "cadre_vie"

    # Priorité 1 : Stationnement & Annexes (évite que 'place sécurisée' aille dans cadre de vie)
    if any(k in norm for k in ["parking", "garage", "carport", "box", "cellier", "abri voiture", "stationnement"]):
        # Sauf si c'est purement une condition financière de location de parking optionnel
        if "option" not in norm and "loyer" not in norm:
            return "stationnement_annexes"

    # Priorité 2 : Espace Nuit & Sanitaires
    if any(k in norm for k in ["chambre", "dressing", "parentale", "salle d'eau", "salle de bain", "sde", "sdb", "douche", "baignoire", "wc", "toilette"]):
        return "espace_nuit"

    # Priorité 3 : Pièces de vie
    if any(k in norm for k in ["cuisine", "séjour", "salon", "salle à manger", "buanderie", "baie vitrée", "baies vitrées", "îlot"]):
        return "pieces_de_vie"

    # Priorité 4 : Extérieurs & Vue
    if any(k in norm for k in ["terrasse", "varangue", "jardin", "balcon", "deck", "piscine", "cour", "vue mer", "vue lagon", "vue dégagée", "extérieur", "extérieurs"]):
        return "exterieurs"

    # Priorité 5 : Conditions & Modalités
    if any(k in norm for k in ["loyer", "dépôt de garantie", "caution", "charges comprises", "hors charges", "bail", "candidature"]):
        return "conditions_modalites"

    scores: Dict[str, int] = {}
    for cat in CATEGORY_DEFINITIONS:
        score = sum(1 for kw in cat["keywords"] if kw in norm)
        if score > 0:
            scores[cat["key"]] = score

    if scores:
        return max(scores, key=scores.get)

    # Fallback par défaut : prestations complémentaires si descriptif de confort, sinon cadre de vie
    if any(term in norm for term in ["chauffe-eau", "solaire", "panneaux", "volets", "fibre", "alarme", "carrelage", "moustiquaire"]):
        return "prestations_complementaires"

    return "cadre_vie"


def structure_description(raw_description: Optional[str]) -> Dict[str, Any]:
    """
    Fonction principale de structuration sémantique de la description d'un bien.
    Retourne :
    {
        "has_structured": bool,
        "direct_contacts": [ {"name": "...", "phone": "...", "whatsapp": "..."} ],
        "sections": [
            {
                "key": "cadre_vie",
                "title": "Cadre de vie & Résidence",
                "icon": "🏛️",
                "items": ["..."]
            },
            ...
        ],
        "key_highlights": ["..."],
        "cleaned_full_text": "..."
    }
    """
    if not raw_description or not isinstance(raw_description, str) or not raw_description.strip():
        return {
            "has_structured": False,
            "direct_contacts": [],
            "sections": [],
            "key_highlights": [],
            "cleaned_full_text": ""
        }

    raw_text = raw_description.strip()

    # 1. Extraction et routage des contacts directs
    direct_contacts = extract_direct_contacts(raw_text)

    # 2. Découpage du texte en lignes et segments logiques
    # Remplace les balises HTML <br> ou <p> par des sauts de ligne
    clean_breaks = re.sub(r'<br\s*/?>', '\n', raw_text, flags=re.IGNORECASE)
    clean_breaks = re.sub(r'</?p>', '\n', clean_breaks, flags=re.IGNORECASE)

    raw_lines = [l.strip() for l in clean_breaks.split('\n') if l.strip()]

    # Si le texte est un seul bloc massif sans saut de ligne, tenter de découper par phrases
    if len(raw_lines) == 1 and len(raw_text) > 150:
        raw_lines = [s.strip() for s in re.split(r'(?<=[.!?])\s+(?=[A-ZÀ-Ÿ])', raw_text) if s.strip()]

    categorized_items: Dict[str, List[str]] = {cat["key"]: [] for cat in CATEGORY_DEFINITIONS}
    categorized_items["prestations_complementaires"] = []

    cleaned_lines_all = []

    for line in raw_lines:
        clean = clean_line_typography(line)
        if not clean or len(clean) < 3:
            continue

        # Filtrer le boilerplate et les contacts déjà extraits
        if is_contact_or_boilerplate_line(clean, direct_contacts):
            continue

        cat_key = classify_bullet_or_sentence(clean)
        if cat_key in categorized_items:
            categorized_items[cat_key].append(clean)
        else:
            categorized_items["prestations_complementaires"].append(clean)

        cleaned_lines_all.append(clean)

    # 3. Assembler les sections finales dans l'ordre strict
    sections = []
    category_titles = {c["key"]: (c["title"], c["icon"]) for c in CATEGORY_DEFINITIONS}
    category_titles["prestations_complementaires"] = ("Équipements & Prestations complémentaires", "✨")

    ordered_keys = [
        "cadre_vie",
        "pieces_de_vie",
        "espace_nuit",
        "exterieurs",
        "stationnement_annexes",
        "prestations_complementaires",
        "conditions_modalites"
    ]

    for k in ordered_keys:
        items = categorized_items.get(k, [])
        if items:
            title, icon = category_titles[k]
            sections.append({
                "key": k,
                "title": title,
                "icon": icon,
                "items": items
            })

    # 4. Extraire 3 à 5 badges atouts flash en tête
    highlights = []
    full_str = " ".join(cleaned_lines_all).lower()

    # Détection surface jardin / terrain
    m_jardin = re.search(r'(?:jardin|ensemble\s+ext[ée]rieur|terrain)\s*(?:d[\'\s]|de\s*)?(?:environ\s*)?(\d+[\s.]?\d*)\s*(?:m²|m2|ares?)', full_str)
    if m_jardin:
        val = m_jardin.group(1).replace(" ", "")
        unit = "ares" if "are" in m_jardin.group(0) else "m²"
        highlights.append(f"🌿 Jardin / Extérieurs ~{val} {unit}")

    # Détection terrasse
    m_terrasse = re.search(r'terrasse\s*(?:couverte)?\s*(?:de\s*)?(\d+)\s*(?:m²|m2)', full_str)
    if m_terrasse:
        highlights.append(f"☕ Terrasse {m_terrasse.group(1)} m²")

    # Détection parkings
    m_park = re.search(r'(\d+)\s*(?:places?\s*de\s*)?parkings?', full_str)
    if m_park:
        highlights.append(f"🚗 {m_park.group(1)} Parking{'s' if int(m_park.group(1)) > 1 else ''}")
    elif "garage" in full_str:
        highlights.append("🚗 Garage privatif")

    # Détection cellier
    if "cellier" in full_str:
        highlights.append("📦 Cellier privatif")

    # Détection climatisé
    if "climatis" in full_str or "climatisation" in full_str:
        highlights.append("❄️ Climatisé")

    # Détection piscine
    if "piscine" in full_str:
        highlights.append("🏊 Piscine")

    return {
        "has_structured": len(sections) > 0,
        "direct_contacts": direct_contacts,
        "sections": sections,
        "key_highlights": highlights[:5],
        "cleaned_full_text": "\n\n".join(cleaned_lines_all)
    }
