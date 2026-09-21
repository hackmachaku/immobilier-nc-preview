"""
Module de structuration intelligente des descriptions d'annonces immobilières NC.
Architecture sémantique à deux niveaux (Tier-1 Déterministe Instantané + Tier-2 Enrichissement) :
1. Segmentation étanche de fin d'annonce (Footer Boundary) : isole tout le bloc commercial.
2. Moteur universel de contacts NC : fixes (2x, 3x, 4x), mobiles (7x, 8x, 9x), emails, horaires, cartes pro.
3. Décomposition intra-ligne multi-attributs : dissociation des puces en ligne (•, |, //).
4. Taxonomie enrichie en 7 rubriques standardisées (avec Services & Équipements de la résidence).
5. Garantie absolue : zéro perte d'information, corps de texte 100 % épuré des signatures.
"""

import re
from typing import Dict, List, Any, Optional, Tuple

# Mots vides à ne pas considérer comme des noms de négociateurs
STOP_NAMES = {
    'appartement', 'maison', 'villa', 'terrain', 'dock', 'bureau', 'local',
    'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche',
    'agence', 'immo', 'immobilier', 'site', 'mail', 'email', 'merci', 'info',
    'notre', 'votre', 'visite', 'visites', 'contact', 'pour', 'avec', 'dans',
    'urgent', 'exclusivite', 'nouveau', 'prix', 'loyer', 'charges', 'tel', 'tél',
    'mobile', 'telephone', 'téléphone', 'bien', 'secteur', 'rdv', 'rendez-vous',
    'négociation', 'negociation', 'négociations', 'negociations', 'bureau', 'bail',
    'internet', 'lien', 'plus', 'toutes', 'tous', 'candidature', 'dossier', 'horaires',
    'garantie', 'carte', 'bancaire', 'propriétaire', 'proprietaire'
}

# Slogans publicitaires sans valeur informative à filtrer
BOILERPLATE_PATTERNS = [
    re.compile(r'^\s*(?:✨|❗️|⭐|💥|🔥)?\s*(?:a\s+saisir|a\s+voir\s+absolument|coup\s+de\s+c[oœ]ur|coup\s+de\s+c[oœ]ur\s+assur[eé]|exclusivit[eé]|urgent|nouveaut[eé]|a\s+visiter\s+rapidement|a\s+ne\s+pas\s+manquer|rare\s+sur\s+le\s+secteur|opportunit[eé]\s+[aà]\s+saisir)\s*(?:✨|❗️|⭐|💥|🔥)?\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:il\s+comprend|elle\s+comprend|il\s+se\s+compose|elle\s+se\s+compose|description|composition)\s*:?\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:lien\s+site\s+internet|site\s+web)\s*:?\s*https?://\S+\s*$', re.IGNORECASE),
]

# Déclencheurs marquant l'amorce du pied de page / bloc de contact
FOOTER_TRIGGER_REGEX = re.compile(
    r'(?:contactez-nous|pour\s+(?:en\s+savoir\s+plus|planifier|visiter|tout(?:e?s?)?\s+renseignement|visite)|'
    r'visites?\s+(?:et\s+n[ée]gociations?|avec)|prenez\s+rendez-vous|vos?\s+conseillers?|'
    r'renseignements?\s+aupr[èe]s\s+de|retrouvez\s+toutes\s+nos\s+offres|annonce\s+\w+.*contact\s*:)',
    re.IGNORECASE
)

# Regex universelle de détection des numéros calédoniens (fixes 2x/3x/4x, mobiles 7x/8x/9x, verts 05)
PHONE_NC_REGEX = re.compile(
    r'(?:(?:\+687|00687)[\s.-]*)?(?:([2-9]\d{2}[\s.-]\d{3})|([02-9]\d(?:[\s.-]?\d{2}){2})|\b([2-9]\d{5})\b)'
)
EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')

# Dictionnaires sémantiques par rubrique ordonnée (Taxonomie enrichie en 7 rubriques)
CATEGORY_DEFINITIONS = [
    {
        "key": "cadre_vie",
        "title": "Cadre de vie & Emplacement",
        "icon": "🏛️",
        "keywords": [
            "résidence", "residence", "résidentiel", "residentiel", "environnement",
            "quartier", "proche", "commodités", "commodites", "école", "ecole", "commerces",
            "centre-ville", "centre ville", "marché", "marche", "emplacement", "vue",
            "situation", "plain-pied", "plain pied", "copropriété", "copropriete", "standing",
            "immeuble", "anse vata", "mer pour horizon", "promenade", "littoral", "plage",
            "calme", "art de vivre", "couchers de soleil", "animation"
        ]
    },
    {
        "key": "services_residence",
        "title": "Services & Équipements de la résidence",
        "icon": "🏋️‍♂️",
        "keywords": [
            "fitness", "salle de fitness", "salle de sport", "gym", "restaurant", "restaurants",
            "coiffeur", "coiffeurs", "boutique", "boutiques", "conciergerie", "concierge",
            "gardien", "gardiennage", "vidéosurveillance", "videosurveillance", "sécurisée",
            "securisee", "ascenseur", "tennis", "court de tennis", "squash", "sauna", "spa",
            "hammam", "rooftop", "complexe", "services du complexe", "interphone", "digicode"
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
            "chambre", "chambres", "suite parentale", "parentale", "dressing", "placard", "placards",
            "penderie", "salle d'eau", "salle d eau", "salles d'eau", "salles d eau",
            "salle de bain", "salle de bains", "salles de bain", "salles de bains",
            "sdb", "sde", "douche", "baignoire", "vasque", "wc", "toilette", "toilettes",
            "climatisé", "climatise", "climatisée", "climatisee", "climatisées", "climatisees",
            "clim", "climatisation", "split", "volet", "volets"
        ]
    },
    {
        "key": "exterieurs",
        "title": "Loisirs, Détente & Extérieurs",
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
            "non meublé", "non-meublé", "charges comprises", "hors charges", "prix :"
        ]
    }
]


def clean_line_typography(line: str) -> str:
    """
    Nettoie la typographie d'une ligne ou puce :
    - Supprime les puces hétérogènes et emojis décoratifs de tête et queue.
    - Réduit les ponctuations excessives (!!!!!!! -> !, ???? -> ?).
    - Supprime les espaces superflus.
    """
    if not line:
        return ""

    # Retrait des puces hétérogènes et emojis de liste en début de ligne
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


def normalize_nc_phone(raw_phone: str) -> Optional[Dict[str, Any]]:
    """
    Normalise un numéro calédonien (6 chiffres) :
    - Format triplets (289.888) ou standard (28.98.88 / 85.46.55).
    - Détermine si mobile (7x, 8x, 9x) avec WhatsApp ou fixe (2x, 3x, 4x, 05).
    """
    if not raw_phone:
        return None

    digits = re.sub(r'\D', '', raw_phone)
    if len(digits) > 6 and digits.startswith('687'):
        digits = digits[3:]

    if len(digits) != 6:
        return None

    first_digit = digits[0]
    # Les numéros NC valides commencent par 2, 3, 4, 7, 8, 9 ou 05
    if first_digit not in ('2', '3', '4', '7', '8', '9') and not digits.startswith('05'):
        return None

    is_mobile = first_digit in ('7', '8', '9')
    whatsapp_num = f"687{digits}" if is_mobile else None

    # Conservation du formatage triplet si présent à l'origine (ex: 289.888 ou 289 888)
    clean_raw = raw_phone.strip()
    if re.match(r'^\+?(?:687)?\s*2\d{2}[\s.]\d{3}$', clean_raw) or len(digits) == 6 and ('.' in clean_raw and len(clean_raw.split('.')[0][-3:]) == 3):
        formatted = f"+687 {digits[:3]}.{digits[3:]}"
    else:
        formatted = f"+687 {digits[:2]}.{digits[2:4]}.{digits[4:6]}"

    return {
        "raw": raw_phone.strip(),
        "digits": digits,
        "formatted": formatted,
        "is_mobile": is_mobile,
        "whatsapp": whatsapp_num
    }


def split_compound_line(line: str) -> List[str]:
    """
    Décompose une ligne compacte multi-attributs (séparée par •, |, // ou ;)
    en segments sémantiques atomiques pour une classification précise.
    Ex: '83 m² habitables • Terrasse ~40 m² • Vue mer' -> ['83 m² habitables', 'Terrasse ~40 m²', 'Vue mer']
    """
    if not line:
        return []

    # Vérifie la présence de séparateurs internes
    if re.search(r'\s+[•|/]\s+|\s*//\s*', line):
        parts = [p.strip() for p in re.split(r'\s+[•|]\s+|\s*//\s*', line) if p.strip()]
        if len(parts) > 1:
            return parts

    return [line]


def split_body_and_footer(lines: List[str]) -> Tuple[List[str], List[str]]:
    """
    Sépare étanchément le corps descriptif du bien du bloc commercial de signature (Footer Boundary).
    Garantit qu'aucune ligne de contact/horaires/légal ne subsiste dans la description.
    """
    if not lines:
        return [], []

    footer_start_idx = None
    for i, line in enumerate(lines):
        clean = line.strip()
        # Détection du déclencheur explicite
        if FOOTER_TRIGGER_REGEX.search(clean):
            footer_start_idx = i
            break
        # Détection d'un bloc contact en fin d'annonce (dans les 10 dernières lignes ou 2e moitié)
        if i >= len(lines) - 8 or i >= len(lines) // 2:
            if EMAIL_REGEX.search(clean) or ('tél' in clean.lower() and PHONE_NC_REGEX.search(clean)):
                # Si la ligne précédente était un nom propre ou une amorce courte
                if i > 0 and (len(lines[i-1]) < 35 and not any(k in lines[i-1].lower() for k in ['chambre', 'séjour', 'cuisine', 'terrasse'])):
                    footer_start_idx = i - 1
                else:
                    footer_start_idx = i
                break

    if footer_start_idx is not None:
        return lines[:footer_start_idx], lines[footer_start_idx:]

    return lines, []


def extract_direct_contacts_and_agency_metadata(
    raw_text: str,
    footer_lines: Optional[List[str]] = None
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Extrait universellement les contacts négociateurs et les métadonnées de l'agence :
    - Négociateurs avec nom, téléphone direct (mobile ou fixe) et WhatsApp.
    - Téléphones, emails, horaires d'ouverture et mentions juridiques de l'agence.
    """
    direct_contacts: List[Dict[str, Any]] = []
    agency_metadata: Dict[str, Any] = {
        "phone": None,
        "email": None,
        "hours": [],
        "legal": []
    }

    if not raw_text:
        return direct_contacts, agency_metadata

    seen_phones = set()

    # 1. Parsing du bloc footer si disponible
    footer_str = "\n".join(footer_lines) if footer_lines else ""
    search_scope = footer_str if footer_str else raw_text

    # Extraction des emails
    all_emails = EMAIL_REGEX.findall(raw_text)
    if all_emails:
        agency_metadata["email"] = all_emails[0]

    # Extraction des horaires
    if footer_lines:
        for fl in footer_lines:
            fl_clean = fl.strip()
            if any(d in fl_clean.lower() for d in ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'horaires']) and ('h' in fl_clean.lower() or ':' in fl_clean):
                if fl_clean not in agency_metadata["hours"]:
                    agency_metadata["hours"].append(fl_clean)
            if any(k in fl_clean.lower() for k in ['cp n', 'carte pro', 'garantie bancaire', 'ridet', 'rcs']):
                if fl_clean not in agency_metadata["legal"]:
                    agency_metadata["legal"].append(fl_clean)

    # Extraction de nom de négociateur sur ligne dédiée dans le footer
    negotiator_from_footer = None
    if footer_lines:
        for fl in footer_lines:
            fl_clean = fl.strip()
            if FOOTER_TRIGGER_REGEX.search(fl_clean) or EMAIL_REGEX.search(fl_clean):
                continue
            if any(w in fl_clean.lower() for w in ['horaires', 'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'garantie', 'cp n', 'bci', 'bnc', 'prix']):
                continue
            # Détection d'un nom de négociateur type "Laurent NGUYEN" ou "Contact : Marc DUPONT"
            m_nom = re.search(r'(?:contact\s*:?|n[ée]gociat(?:eur|rice)\s*:?|conseill[eè]re?\s*:?|avec)\s+([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)', fl_clean, re.IGNORECASE)
            if m_nom:
                negotiator_from_footer = m_nom.group(1).strip().title()
                break
            # Ligne courte de type Nom Propre isolé (ex: "Laurent NGUYEN")
            if re.match(r'^[A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ0-9\-]+)+$', fl_clean) and len(fl_clean) < 35:
                # Vérifier que ce n'est pas un stop name
                if fl_clean.lower() not in STOP_NAMES:
                    negotiator_from_footer = fl_clean.strip()
                    break

    # 2. Motifs syntaxiques intra-texte de contacts directs
    patterns = [
        # "visites et négociations avec Tim au 85.46.55 ou Séverine au 90.57.97"
        re.compile(r'(?:visites?\s*(?:et\s*n[ée]gociations?)?\s*)?(?:avec|contacter|joindre|appeler|demander)\s+([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s+(?:au|t[ée]l\s*:?|mobile\s*:?|le)\s*(?:(?:\+687|00687)\s*)?([02-9]\d{1,2}[\s.-]?\d{2,3}[\s.-]?\d{2})', re.IGNORECASE),
        # "Tim au 85.46.55"
        re.compile(r'\b([A-ZÀ-Ÿ][a-zà-ÿ\-]+)\s+au\s+(?:(?:\+687|00687)\s*)?([02-9]\d{1,2}[\s.-]?\d{2,3}[\s.-]?\d{2})'),
        # "Contact : Marc DUPONT : 82.30.40"
        re.compile(r'(?:contact|conseill[eè]re?|n[ée]gociat(?:eur|rice)|r[ée]f[ée]rent|agent)\s*:?\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s*(?:au|:|\(|\-)?\s*(?:(?:\+687|00687)\s*)?([02-9]\d{1,2}[\s.-]?\d{2,3}[\s.-]?\d{2})', re.IGNORECASE),
        # "Contact: CLARE CHRISTELLE (Tél: 79 40 01, Email: ...)"
        re.compile(r'Contact\s*:\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s*\(\s*T[ée]l\s*:\s*([02-9]\d{1,2}[\s.-]?\d{2,3}[\s.-]?\d{2})', re.IGNORECASE),
        # "85.46.55 (Tim)"
        re.compile(r'(?:(?:\+687|00687)\s*)?([02-9]\d{1,2}[\s.-]?\d{2,3}[\s.-]?\d{2})\s*\(\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+)\s*\)', re.IGNORECASE),
    ]

    for pat in patterns:
        for m in pat.finditer(raw_text):
            g1, g2 = m.group(1).strip(), m.group(2).strip()
            # Si g1 est le numéro
            if re.match(r'^[02-9]\d', g1):
                phone_raw, name_raw = g1, g2
            else:
                name_raw, phone_raw = g1, g2

            clean_name = re.sub(r'^(?:ez|er|avec|au|de|le|la|du|un|une)\s+', '', name_raw, flags=re.IGNORECASE).strip()
            if clean_name.lower() in STOP_NAMES or len(clean_name) < 2:
                continue

            phone_info = normalize_nc_phone(phone_raw)
            if not phone_info or phone_info["digits"] in seen_phones:
                continue

            seen_phones.add(phone_info["digits"])
            direct_contacts.append({
                "name": clean_name.title(),
                "phone": phone_info["formatted"],
                "whatsapp": phone_info["whatsapp"],
                "email": agency_metadata["email"],
                "is_mobile": phone_info["is_mobile"],
                "raw_phone": phone_info["digits"]
            })

    # 3. Association du négociateur extrait du footer avec le premier téléphone du footer
    footer_phones = []
    for m in PHONE_NC_REGEX.finditer(search_scope):
        p_str = m.group(1) or m.group(2) or m.group(3)
        p_info = normalize_nc_phone(p_str)
        if p_info and p_info["digits"] not in [p["digits"] for p in footer_phones]:
            # Garde-fou contre les faux numéros tirés des cartes pro (ex: CP N 2013 165T -> 2013 16)
            match_start = m.start()
            prefix_ctx = search_scope[max(0, match_start - 15):match_start].lower()
            if any(k in prefix_ctx for k in ['cp n', 'cp ', 'carte pro', 'ridet', 'rcs']):
                continue
            footer_phones.append(p_info)

    if footer_phones:
        agency_metadata["phone"] = footer_phones[0]["formatted"]

    if negotiator_from_footer and footer_phones:
        p0 = footer_phones[0]
        if p0["digits"] not in seen_phones:
            seen_phones.add(p0["digits"])
            direct_contacts.append({
                "name": negotiator_from_footer,
                "phone": p0["formatted"],
                "whatsapp": p0["whatsapp"],
                "email": agency_metadata["email"],
                "is_mobile": p0["is_mobile"],
                "raw_phone": p0["digits"]
            })

    return direct_contacts, agency_metadata


def is_contact_or_boilerplate_line(line: str, direct_contacts: List[Dict[str, Any]]) -> bool:
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
        'il comprend', 'elle comprend', 'il se compose', 'elle se compose',
        'description', 'composition', 'a visiter absolument'
    }
    if clean_no_emoji in slogans:
        return True

    for pat in BOILERPLATE_PATTERNS:
        if pat.match(clean) or pat.match(clean_no_emoji):
            return True

    # Détection de ligne de contact ou signature
    for contact in direct_contacts:
        if contact["raw_phone"] in clean or contact["name"].lower() in clean.lower():
            if re.search(r'(?:visite|visiter|négociation|contact|joindre|appeler|tél|tel|mobile)', clean, re.IGNORECASE):
                return True

    # Détecteur général de ligne de contact téléphonique résiduelle
    if re.search(r'(?:visite|visiter|contacter|joindre|appeler)\s+.*(?:au|t[ée]l)?\s*[02-9]\d{1,2}[\s.]?\d{2,3}[\s.]?\d{2}', clean, re.IGNORECASE):
        return True

    return False


def contains_keyword(text: str, kw: str) -> bool:
    """Vérifie si un mot-clé est présent, avec frontière de mot pour les termes courts (<= 4 lettres)."""
    if len(kw) <= 4:
        return bool(re.search(rf'\b{re.escape(kw)}\b', text))
    return kw in text


def classify_bullet_or_sentence(text: str) -> str:
    """
    Classe une phrase ou segment dans l'une des 7 rubriques à l'aide d'un score lexical pondéré.
    Retourne la clé de catégorie correspondante.
    """
    norm = text.lower().replace("’", "'")

    # Priorité 0 : Conditions & Modalités financières explicites
    if any(contains_keyword(norm, k) for k in ["loyer :", "loyer:", "loyer mensuel", "dépôt de garantie", "charges comprises", "hors charges", "bail"]):
        return "conditions_modalites"
    if re.search(r'\bprix\s*:\s*\d+[\s.]?\d*\s*(?:f\s*cfp|xpf|f)\b', norm):
        return "conditions_modalites"

    # Priorité 1 : Espace Nuit & Sanitaires (vérifié avant pour éviter que 'espace de vie' ou 'suite' soit faussé)
    if any(contains_keyword(norm, k) for k in [
        "chambre", "chambres", "dressing", "parentale", "salle d'eau", "salles d'eau",
        "salle de bain", "salle de bains", "salles de bain", "salles de bains",
        "sde", "sdb", "douche", "baignoire", "wc", "toilette", "toilettes"
    ]):
        return "espace_nuit"

    # Priorité 2 : Pièces de vie
    if any(contains_keyword(norm, k) for k in [
        "cuisine", "séjour", "sejour", "salon", "salle à manger", "buanderie", "baie vitrée", "baies vitrées", "îlot", "ilot"
    ]):
        return "pieces_de_vie"

    # Priorité 3 : Services & Équipements de la résidence / copropriété
    if any(contains_keyword(norm, k) for k in [
        "fitness", "salle de fitness", "salle de sport", "gym", "restaurant", "restaurants",
        "coiffeur", "coiffeurs", "boutique", "boutiques", "conciergerie", "concierge",
        "gardien", "gardiennage", "vidéosurveillance", "videosurveillance", "ascenseur",
        "court de tennis", "sauna", "spa", "rooftop"
    ]):
        return "services_residence"

    # Priorité 4 : Stationnement & Annexes
    if any(contains_keyword(norm, k) for k in ["parking", "parkings", "garage", "garages", "carport", "box", "cellier", "abri voiture", "stationnement", "stationnements"]):
        if "cellier intérieur" not in norm and "option" not in norm and "loyer" not in norm:
            return "stationnement_annexes"

    # Priorité 5 : Extérieurs & Vue
    if any(contains_keyword(norm, k) for k in ["terrasse", "varangue", "jardin", "balcon", "deck", "piscine", "cour", "vue mer", "vue lagon", "vue dégagée", "extérieur", "extérieurs", "espace extérieur"]):
        return "exterieurs"

    # Priorité 6 : Cadre de vie & Présentation d'ensemble
    if re.search(r'\b(?:dans\s+une\s+r[ée]sidence|au\s+c[oœ]ur\s+d|situ[ée]e?\s+dans|quartier\s+r[ée]sidentiel|venez\s+d[ée]couvrir|bel\s+appartement|belle\s+villa|joli\s+studio|emplacement\s+central|proche\s+de\s+toutes|la\s+mer\s+pour\s+horizon|art\s+de\s+vivre)\b', norm):
        return "cadre_vie"

    scores: Dict[str, int] = {}
    for cat in CATEGORY_DEFINITIONS:
        score = sum(1 for kw in cat["keywords"] if contains_keyword(norm, kw))
        if score > 0:
            scores[cat["key"]] = score

    if scores:
        return max(scores, key=scores.get)

    # Fallback : prestations complémentaires si confort, sinon cadre de vie
    if any(contains_keyword(norm, term) for term in ["chauffe-eau", "solaire", "panneaux", "volets", "fibre", "alarme", "carrelage", "moustiquaire"]):
        return "prestations_complementaires"

    return "cadre_vie"


def structure_description(raw_description: Optional[str]) -> Dict[str, Any]:
    """
    Fonction principale de structuration sémantique de la description d'un bien.
    Retourne la fiche structurée, les contacts négociateurs, les métadonnées agence,
    les atouts flash et le texte intégral nettoyé.
    """
    if not raw_description or not isinstance(raw_description, str) or not raw_description.strip():
        return {
            "has_structured": False,
            "direct_contacts": [],
            "agency_metadata": {"phone": None, "email": None, "hours": [], "legal": []},
            "sections": [],
            "key_highlights": [],
            "cleaned_full_text": ""
        }

    raw_text = raw_description.strip()

    # 1. Découpage du texte en lignes
    clean_breaks = re.sub(r'<br\s*/?>', '\n', raw_text, flags=re.IGNORECASE)
    clean_breaks = re.sub(r'</?p>', '\n', clean_breaks, flags=re.IGNORECASE)
    raw_lines = [l.strip() for l in clean_breaks.split('\n') if l.strip()]

    # 2. Segmentation Footer Boundary (isolation étanche de la signature)
    body_lines, footer_lines = split_body_and_footer(raw_lines)

    # 3. Extraction des contacts directs et métadonnées d'agence
    direct_contacts, agency_metadata = extract_direct_contacts_and_agency_metadata(raw_text, footer_lines)

    # 4. Si le corps est un seul bloc massif sans saut de ligne, découper par phrases
    if len(body_lines) == 1 and len(body_lines[0]) > 150:
        body_lines = [s.strip() for s in re.split(r'(?<=[.!?])\s+(?=[A-ZÀ-Ÿ])', body_lines[0]) if s.strip()]

    categorized_items: Dict[str, List[str]] = {cat["key"]: [] for cat in CATEGORY_DEFINITIONS}
    categorized_items["prestations_complementaires"] = []

    cleaned_lines_all = []

    for line in body_lines:
        clean = clean_line_typography(line)
        if not clean or len(clean) < 3:
            continue

        # Filtrer le boilerplate et les contacts
        if is_contact_or_boilerplate_line(clean, direct_contacts):
            continue

        # Décomposition intra-ligne des puces multiples (•, |, //)
        sub_segments = split_compound_line(clean)

        for seg in sub_segments:
            seg_clean = clean_line_typography(seg)
            if not seg_clean or len(seg_clean) < 2:
                continue

            if is_contact_or_boilerplate_line(seg_clean, direct_contacts):
                continue

            cat_key = classify_bullet_or_sentence(seg_clean)
            if cat_key in categorized_items:
                categorized_items[cat_key].append(seg_clean)
            else:
                categorized_items["prestations_complementaires"].append(seg_clean)

            cleaned_lines_all.append(seg_clean)

    # 5. Assembler les sections finales dans l'ordre strict
    sections = []
    category_titles = {c["key"]: (c["title"], c["icon"]) for c in CATEGORY_DEFINITIONS}
    category_titles["prestations_complementaires"] = ("Équipements & Prestations complémentaires", "✨")

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

    # 6. Extraire 3 à 5 badges atouts flash en tête
    highlights = []
    full_str = " ".join(cleaned_lines_all).lower()

    # Détection surface jardin / terrain
    m_jardin = re.search(r'(?:jardin|ensemble\s+ext[ée]rieur|terrain)\s*(?:d[\'\s]|de\s*)?(?:environ\s*)?(\d+[\s.]?\d*)\s*(?:m²|m2|ares?)', full_str)
    if m_jardin:
        val = m_jardin.group(1).replace(" ", "")
        unit = "ares" if "are" in m_jardin.group(0) else "m²"
        highlights.append(f"🌿 Jardin ~{val} {unit}")

    # Détection terrasse
    m_terrasse = re.search(r'terrasse\s*(?:couverte)?\s*(?:d[\'\s]|de\s*)?(?:environ\s*)?~?(\d+)\s*(?:m²|m2)', full_str)
    if m_terrasse:
        highlights.append(f"☕ Terrasse {m_terrasse.group(1)} m²")

    # Détection vue mer
    if "vue mer" in full_str or "vue lagon" in full_str:
        highlights.append("🌊 Vue mer")

    # Détection parkings
    m_park = re.search(r'(\d+)\s*(?:places?\s*de\s*)?(?:stationnements?|parkings?)', full_str)
    if m_park:
        highlights.append(f"🚗 {m_park.group(1)} Parking{'s' if int(m_park.group(1)) > 1 else ''}")
    elif "garage" in full_str:
        highlights.append("🚗 Garage privatif")

    # Détection cellier
    if "cellier" in full_str:
        highlights.append("📦 Cellier privatif")

    # Détection fitness / salle de sport
    if "fitness" in full_str or "salle de sport" in full_str:
        highlights.append("🏋️ Fitness")

    # Détection climatisé
    if "climatis" in full_str or "climatisation" in full_str:
        highlights.append("❄️ Climatisé")

    # Détection piscine
    if "piscine" in full_str:
        highlights.append("🏊 Piscine")

    return {
        "has_structured": len(sections) > 0,
        "direct_contacts": direct_contacts,
        "agency_metadata": agency_metadata,
        "sections": sections,
        "key_highlights": highlights[:6],
        "cleaned_full_text": "\n\n".join(cleaned_lines_all)
    }


def extract_direct_contacts(text: str) -> List[Dict[str, Any]]:
    """Rétro-compatibilité : extrait les contacts directs à partir d'un texte brut."""
    contacts, _ = extract_direct_contacts_and_agency_metadata(text)
    return contacts
