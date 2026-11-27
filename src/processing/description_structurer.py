"""
Module de Structuration Sémantique Universelle et Décomposition Atomique des Descriptions Immobilières.
Conçu pour unifier le rendu de 100 % des annonces de Nouvelle-Calédonie (toutes agences et portails),
sans aucune perte d'information critique (surfaces, niveaux RDC/Étage, pièces, équipements, contacts) :
- Nettoyage en amont des pollutions techniques ([IMG: https://...], bannières HTML statiques immonc).
- Moteur Universel de Téléphonie Calédonienne (OPT 6 chiffres) & Filtrage strict des faux noms ('Réf', 'Nous', 'Loyer Hc'...).
- Isolation étanche de la signature d'agence (Footer Boundary Segmentation).
- Propagation du niveau architectural (RDC, À l'étage, Mezzanine, Sous-sol, Niveau inférieur).
- Décomposition grammaticale en propositions nominales atomiques (accroches combinées, énumérations partitives d'un/d'une/de, pivots internes comprenant/composé de, connecteurs et/ainsi que/ouvrant sur, listes inline ; / + :-).
- Classification déterministe en 8 rubriques canoniques invariantes (ordre, icônes et titres fixes).
"""

import re
import html
from typing import Dict, List, Any, Optional, Tuple


# ============================================================================
# EXPRESSIONS RÉGULIÈRES DE DÉTECTION (TÉLÉPHONIE NC, EMAILS, FOOTERS, BRUIT)
# ============================================================================

# Numérotation calédonienne à 6 chiffres (OPT) :
# - Format triplets : 289.888, 289 888, 794-001, 505.510
# - Format paires : 28.98.88, 79 40 01, 0 79.40.01 (avec 0 de tête parfois utilisé)
# - Format continu : 289888, 794001
PHONE_NC_REGEX = re.compile(
    r'(?:(?:\+687|00687)[\s.-]*)?'
    r'(?:'
    r'([2-9]\d{2}[\s.-]\d{3})'
    r'|'
    r'([02-9]\d(?:[\s.-]?\d{2}){2})'
    r'|'
    r'\b([2-9]\d{5})\b'
    r')'
)

EMAIL_REGEX = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')

# Déclencheurs de pied d'annonce (Footer / Signature commerciale)
FOOTER_TRIGGER_REGEX = re.compile(
    r'(?:'
    r'contactez[- ]nous\b|'
    r'n[\'\’]h[ée]sitez\s+pas\s+[àa]\s+nous\s+contacter|'
    r'pour\s+(?:plus\s+d[\'’]|tout(?:e?s?)?\s+|un\s+|une\s+)?(?:renseignements?|informations?|infos?|visites?|organiser\s+une\s+visite|planifier\s+une\s+visite|en\s+savoir\s+plus)|'
    r'visites?\s+et\s+(?:n[ée]gociations?|renseignements?)|'
    r'infos?\s+et\s+visites?\b|'
    r'horaires?\s*(?:d[\'\’]ouverture)?\s*:|'
    r'\bcp\s*n[°º\s]*\d{4}\b|'
    r'carte\s+professionnelle\b|'
    r'garantie\s+(?:bancaire|financi[èe]re)\b|'
    r'suivez[- ]nous\s+sur\b|'
    r'retrouvez\s+toutes\s+nos\s+annonces'
    r')',
    re.IGNORECASE
)

# Bannières parasites statiques injectées par certains portails (ex: immonc sidebar)
IMMONC_PROMO_BANNER_REGEX = re.compile(
    r'<h5>\s*ideal\s+investissement\s*,\s*actuellement\s+lou[ée]\s*</h5>\s*<h4>\s*f4\s+avec\s+jardin\s*</h4>',
    re.IGNORECASE
)
IMMONC_PROMO_TEXT_REGEX = re.compile(
    r'^\s*ideal\s+investissement\s*,\s*actuellement\s+lou[ée]\s*(?:f4\s+avec\s+jardin)?\s*$',
    re.IGNORECASE | re.MULTILINE
)

# Questions rhétoriques publicitaires
RHETORICAL_QUESTION_REGEX = re.compile(
    r'^\s*(?:'
    r'vous\s+(?:cherchez|recherchez|r[êe]vez|souhaitez|voulez|d[ée]sirez|attendiez|avez\s+envie|[êe]tes\s+[àa]\s+la\s+recherche|[êe]tes\s+int[ée]ress[ée]|en\s+avez\s+assez|aimez)\b|'
    r'envie\s+(?:de\b|d[\'’])|'
    r'besoin\s+(?:de\b|d[\'’])|'
    r'[àa]\s+la\s+recherche\s+(?:de\b|d[\'’])|'
    r'pourquoi\s+(?:choisir|acheter|louer|investir|ce\s+bien)\b|'
    r'qu[\'’]attendez[- ]vous\b|'
    r'quoi\s+de\s+mieux\b|'
    r'que\s+diriez[- ]vous\b|'
    r'qui\s+n[\'’]a\s+jamais\s+r[êe]v[ée]\b|'
    r'pr[êe]ts?\s+[àa]\s+(?:poser\s+vos\s+valises|emm[ée]nager)\b|'
    r'(?:le\s+vrai\s+luxe|son\s+vrai\s+atout|le\s+petit\s+plus|le\s+plus\s+de\s+ce\s+bien|c[ôo]t[ée]\s+pratique)\s*\?'
    r').*\?\s*(?:ne\s+cherchez\s+plus\s*!?)?\s*$',
    re.IGNORECASE
)

# Amorces de composition orphelines (ex: "Elle se compose de :", "La villa comprend :")
ORPHAN_COMPOSITION_HEADER_REGEX = re.compile(
    r'^\s*(?:d[\'’]une\s+superficie[^,]+,\s*)?'
    r'(?:(?:la|le|l[\'’]|cette|ce|cet)\s+(?:maison|villa|appartement|studio|duplex|propri[ée]t[ée]|bien|logement|habitation|partie\s+nuit|partie\s+jour|espace\s+nuit|espace\s+vie|plateau|local|dock|ensemble|rez[- ]de[- ]chauss[ée]e|[ée]tage)|elle|il|on\s+y|un\s+d[ée]gagement)\s+'
    r'(?:se\s+compose(?:\s+ainsi|\s+comme\s+suit|\s+de|\s+d[\'’])?|est\s+compos[ée]e?(?:\s+de|\s+d[\'’]|\s+comme\s+suit)?|comprend|comprenant|dispose(?:\s+de)?|offre|vous\s+propose|dessert(?:\s+l[\'’]espace\s+nuit\s+compos[ée]\s+de)?)\s*:?\s*$',
    re.IGNORECASE
)

# Phrases génériques / slogans / sous-titres vides sans valeur informative
BOILERPLATE_PATTERNS = [
    re.compile(
        r'^\s*(?:description(?:\s+du\s+bien)?|descriptif(?:\s+du\s+bien)?|caract[ée]ristiques?(?:\s+du\s+bien|\s+principales|\s+g[ée]n[ée]rales)?|'
        r'composition(?:\s+du\s+bien)?|d[ée]tails?(?:\s+compl[ée]mentaires)?|en\s+r[ée]sum[ée]|(?:les\s+)?atouts?|les\s+plus|'
        r'en\s+compl[ée]ment|infos?\s+compl[ée]mentaires?|informations?\s+compl[ée]mentaires?|prestations?(?:\s+compl[ée]mentaires)?|'
        r'[ée]quipements?(?:\s+et\s+prestations)?|ext[ée]rieurs?|int[ée]rieur|points?\s+forts?|[àa]\s+d[ée]couvrir|en\s+exclusivit[ée])\s*[:!\-–—]*\s*$',
        re.IGNORECASE
    ),
    re.compile(r'^\s*(?:[àa]\s+saisir|[àa]\s+voir\s+absolument|coup\s+de\s+c[oœ]ur(?:\s+assur[ée])?|exclusivit[ée]|urgent|nouveaut[ée](?:\s+[àa]\s+la\s+vente)?|rare\s+sur\s+le\s+secteur|[àa]\s+visiter\s+sans\s+tarder|opportunit[ée]\s+[àa]\s+saisir|[àa]\s+vendre|[àa]\s+louer|prix\s+de\s+vente|vendu\s*!?|sous\s+compromis|sous\s+offre(?:\s+en\s+\d+\s+semaines?)?|remise\s+[àa]\s+la\s+vente|frais\s+d[\'’]agence\s+offerts?)\s*!*\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:il\s+comprend|elle\s+comprend|il\s+se\s+compose|elle\s+se\s+compose|comprenant|compos[ée]e?\s+de|comme\s+suit|le\s+logement\s+comprend|la\s+villa\s+comprend|la\s+maison\s+comprend|l[\'’]appartement\s+comprend)\s*:?\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:r[ée]f(?:[ée]rence)?\s*(?:annonce|mandat)?\s*[:\-]?\s*[A-Za-z0-9\-_/]+)\s*$', re.IGNORECASE),
    re.compile(r'^\s*(?:photos?\s*:\s*)?(?:www\.[a-zA-Z0-9\-]+\.[a-z]{2,}|https?://\S+)\s*$', re.IGNORECASE),
    RHETORICAL_QUESTION_REGEX,
    ORPHAN_COMPOSITION_HEADER_REGEX,
]

# Liste exhaustive des mots interdits comme prénom/nom de négociateur
STOP_NAMES = {
    'nous', 'vous', 'moi', 'lui', 'elle', 'eux', 'leur', 'leurs',
    'le', 'la', 'les', 'un', 'une', 'des', 'du', 'de', 'au', 'aux', 'en', 'et', 'ou', 'ni', 'ne', 'pas',
    'ce', 'cet', 'cette', 'ces', 'mon', 'ton', 'son', 'ma', 'ta', 'sa', 'mes', 'tes', 'ses', 'notre', 'votre',
    'qui', 'que', 'quoi', 'dont', 'où', 'par', 'pour', 'sur', 'dans', 'avec', 'sans', 'sous', 'vers', 'chez',
    'agence', 'immobilière', 'immobilier', 'immo', 'cabinet', 'groupe', 'étude', 'etude', 'office', 'syndic',
    'équipe', 'equipe', 'service', 'services', 'standard', 'accueil', 'secrétariat', 'secretariat', 'direction',
    'conseiller', 'conseillère', 'conseillere', 'négociateur', 'negociateur', 'négociatrice', 'negociatrice',
    'commercial', 'commerciale', 'agent', 'courtier', 'mandataire', 'gestionnaire', 'propriétaire', 'proprietaire',
    'particulier', 'vendeur', 'bailleur', 'locataire', 'client', 'expert', 'spécialiste', 'specialiste', 'partenaire',
    'tél', 'tel', 'téléphone', 'telephone', 'mobile', 'portable', 'gsm', 'fixe', 'fax', 'whatsapp', 'mail', 'email',
    'courriel', 'contact', 'contacts', 'contactez', 'contacter', 'appelez', 'appeler', 'joindre', 'demander', 'demandez',
    'écrire', 'ecrire', 'envoyer', 'envoyez', 'message', 'sms', 'mp', 'rdv', 'rendez', 'visite', 'visites', 'visiter',
    'info', 'infos', 'information', 'informations', 'renseignement', 'renseignements', 'détail', 'details', 'plus',
    'réf', 'ref', 'référence', 'reference', 'mandat', 'lot', 'lots', 'dossier', 'annonce', 'offre', 'exclusivité',
    'exclusivite', 'nouveau', 'nouveauté', 'nouveaute', 'urgent', 'rare', 'dispo', 'disponible', 'immédiatement',
    'suite', 'libre', 'loué', 'loue', 'vendu',
    'villa', 'maison', 'appartement', 'studio', 'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 't1', 't2', 't3', 't4', 't5',
    'duplex', 'triplex', 'penthouse', 'loft', 'terrain', 'parcelle', 'propriété', 'propriete', 'immeuble', 'local',
    'bureau', 'bureaux', 'commerce', 'commercial', 'dock', 'entrepôt', 'entrepot', 'hangar', 'fonds', 'parking',
    'garage', 'box', 'carport', 'cellier', 'cave', 'terrasse', 'varangue', 'deck', 'jardin', 'piscine', 'balcon',
    'vue', 'mer', 'lagon', 'montagne', 'calme', 'sécurisé', 'securise', 'résidence', 'residence', 'lotissement',
    'quartier', 'secteur', 'centre', 'ville', 'proche', 'proximité', 'proximite', 'commodités', 'commodites',
    'plage', 'baie', 'école', 'ecole', 'commerces', 'cuisine', 'séjour', 'sejour', 'salon', 'chambre', 'chambres',
    'salle', 'bain', 'eau', 'wc', 'volets', 'roulants', 'climatisation', 'climatisé', 'meublé', 'équipé', 'joli', 'beau', 'belle', 'grand',
    'prix', 'loyer', 'charges', 'hc', 'cc', 'ttc', 'ht', 'fai', 'xpf', 'cfp', 'francs', 'millions', 'mf',
    'caution', 'garantie', 'dépôt', 'depot', 'honoraires', 'frais', 'bail', 'mensuel', 'mois', 'an', 'ans',
    'investissement', 'investisseur', 'rendement', 'rentabilité', 'rentabilite', 'défiscalisation',
    'nouméa', 'noumea', 'dumbéa', 'dumbea', 'païta', 'paita', 'mont', 'dore', 'bourail', 'koné', 'kone', 'koumac',
    'la', 'foa', 'boulouparis', 'farino', 'sarraméa', 'thio', 'yaté', 'lifou', 'maré', 'ouvéa', 'îles',
    'vata', 'citrons', 'orphelinat', 'motor', 'pool', 'receiving', 'trianon', 'val', 'plaisance', 'magenta',
    'ouémo', 'ouemo', 'tina', 'koutio', 'auteuil', 'yahoué', 'yahoue', 'katiramona', ' Robinson', 'robinson', 'boulari', 'plum',
    'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche', 'janvier', 'février', 'mars', 'avril',
    'mai', 'juin', 'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre', 'horaires', 'ouverture',
    'ez-nous au', 'ez-nous', 'er-nous', 'loyer hc', 'loyer cc', 'notre commercial', 'notre commerciale',
    'notre agence', 'l agence', 'votre conseiller', 'votre conseillère', 'un conseiller', 'une visite'
}

INVALID_NAME_TOKENS = {
    'ez', 'er', 'ai', 'as', 'ont', 'est', 'sont', 'sera', 'soit', 'été',
    'toute', 'tous', 'tout', 'toutes', 'autre', 'autres', 'même', 'meme',
    'très', 'tres', 'bien', 'bon', 'bonne', 'vite', 'tard', 'tôt', 'tot',
    'ici', 'là', 'la', 'oui', 'non', 'car', 'donc', 'or', 'mais', 'comme',
    'hors', 'compris', 'comprises', 'inclus', 'incluse', 'option', 'possible'
}


# ============================================================================
# DÉFINITION DES 8 RUBRIQUES CANONIQUES INVARIANTES (ORDRE FIXE)
# ============================================================================

CATEGORY_DEFINITIONS = [
    {
        "key": "cadre_vie",
        "title": "Cadre de vie & Environnement",
        "icon": "📍",
        "keywords": [
            "quartier", "secteur", "emplacement", "adresse", "plage", "mer à quelques pas",
            "littoral", "promenade", "coucher de soleil", "vie de quartier", "proximité",
            "proche", "commodités", "écoles", "commerces", "calme", "paisible", "verdoyant",
            "résidence", "architecture", "contemporaine", "moderne", "sécurisée",
            "environnement", "situé", "située", "emplacement privilégié", "idéalement",
            "au cœur de", "coeur de", "anse vata", "baie des citrons", "centre-ville",
            "visibilité", "axe passant", "zone industrielle", "zone commerciale", "activité",
            "tertiaire", "médicale", "restauration", "emplacement n°1", "habitable", "habitables"
        ]
    },
    {
        "key": "services_residence",
        "title": "Services & Équipements de la résidence",
        "icon": "🏢",
        "keywords": [
            "salle de fitness", "fitness", "salle de sport", "gym", "restaurants",
            "coiffeur", "coiffeurs", "boutique", "boutiques", "complexe", "gardien",
            "gardiennage", "conciergerie", "vidéosurveillance", "videosurveillance",
            "digicode", "interphone", "portail sécurisé", "ascenseur", "parties communes",
            "espaces paysagers", "piscine de la résidence", "piscine commune", "tennis"
        ]
    },
    {
        "key": "pieces_de_vie",
        "title": "Pièces de vie (Séjour & Cuisine)",
        "icon": "🛋️",
        "keywords": [
            "séjour", "sejour", "salon", "pièce de vie", "piece de vie", "espace de vie",
            "salle à manger", "cuisine", "américaine", "équipée", "equipee", "aménagée",
            "amenagee", "îlot", "ilot", "cellier intérieur", "buanderie", "passe-plat",
            "lumineux", "lumineuse", "volumes", "baignée de lumière", "baignés de lumière",
            "climatisé", "climatise", "accueil", "cafétéria", "cafeteria", "espace repas"
        ]
    },
    {
        "key": "espace_nuit",
        "title": "Espace Nuit & Sanitaires",
        "icon": "🛏️",
        "keywords": [
            "chambre", "chambres", "suite parentale", "parentale", "espace nuit", "coin nuit",
            "dressing", "placard", "placards", "rangement", "salle d'eau", "salle d’eau",
            "salles d'eau", "salles d’eau", "salle de bain", "salle de bains", "sde", "sdb",
            "douche", "baignoire", "wc", "toilette", "toilettes", "sanitaire", "sanitaires",
            "mezzanine", "bureau", "bureaux", "open space", "open-space", "salle de réunion",
            "pièce principale", "surface principale", "plateau", "cloisonné"
        ]
    },
    {
        "key": "exterieurs",
        "title": "Extérieurs, Terrasse & Vue",
        "icon": "🌴",
        "keywords": [
            "terrasse", "varangue", "balcon", "loggia", "deck", "jardin", "arboré", "arbore",
            "clôturé", "cloture", "clos", "piscine", "spa", "jacuzzi", "faré", "fare", "carbet",
            "pergola", "barbecue", "cuisine extérieure", "vue mer", "vue sur la mer",
            "vue lagon", "vue dégagée", "vue montagne", "sans vis-à-vis", "extérieur",
            "exterieur", "terrain", "parcelle", "ares", "cour", "véranda"
        ]
    },
    {
        "key": "stationnement_annexes",
        "title": "Stationnement & Annexes",
        "icon": "🚗",
        "keywords": [
            "parking", "parkings", "stationnement", "stationnements", "garage", "garages",
            "carport", "box", "cellier", "celliers", "dock", "atelier", "remise", "hangar",
            "abri voiture", "place couverte", "places couvertes", "place sécurisée", "places sécurisées",
            "espace de rangement", "débarras", "debarras", "container"
        ]
    },
    {
        "key": "prestations_complementaires",
        "title": "Équipements & Prestations complémentaires",
        "icon": "✨",
        "keywords": [
            "climatisé", "climatise", "climatisée", "climatisee", "climatisées", "climatisees",
            "climatisation", "clim", "split", "volet", "volets", "volets roulants", "persienne",
            "persiennes", "alarme", "système d'alarme", "chauffe-eau", "chauffe eau", "solaire",
            "photovoltaïque", "photovoltaique", "photovoltaïques", "photovoltaiques",
            "panneaux solaires", "panneaux photovoltaïques", "fosse septique", "tout-à-l'égout",
            "groupe électrogène", "citerne", "surpresseur", "adoucisseur", "fibre", "fibre optique",
            "portail électrique", "portail motorisé", "visiophone", "moustiquaire", "moustiquaires",
            "double vitrage", "isolation", "borne de recharge", "domotique", "onduleur", "pompe à chaleur"
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


def strip_technical_noise(raw_text: str) -> str:
    """
    Élimine dès l'entrée toutes les pollutions techniques :
    - Balises techniques [IMG: https://...]
    - Bannière promotionnelle statique immonc (<h5>ideal investissement...</h5><h4>f4 avec jardin</h4>)
    - Entités HTML (&nbsp;, &rsquo;, etc.)
    """
    if not raw_text:
        return ""
    s = re.sub(r'\[IMG:\s*https?://[^\]]+\]\s*', '', raw_text, flags=re.IGNORECASE)
    s = IMMONC_PROMO_BANNER_REGEX.sub('', s)
    s = IMMONC_PROMO_TEXT_REGEX.sub('', s)
    s = html.unescape(s)
    # Nettoyage des descriptions synthétiques immonc contenant 'None' ou des blocs Contact vides
    s = re.sub(
        r'\bAnnonce\s+(Maison|Villa|Appartement|Terrain|Local(?:\s+commercial)?|Immeuble|Dock|Bureau|Propri[ée]t[ée]|Colocation|Bien(?:\s+meubl[ée])?|Autre)\s+None\b',
        r'\1',
        s,
        flags=re.IGNORECASE
    )
    s = re.sub(r'\bAnnonce\s+([A-ZÀ-Ÿ][a-zà-ÿ]+(?:\s+[A-Z0-9]+)?)\s+[àa]\s+None\s+', r'\1 à ', s, flags=re.IGNORECASE)
    s = re.sub(r'\b[àa]\s+None\s+(?=[A-ZÀ-Ÿ])', 'à ', s)
    s = re.sub(r'\bNone\b', '', s)
    s = re.sub(r'\(\s*T[ée]l\s*:\s*0?\s*,?\s*Email\s*:\s*[^)]*\)', '', s, flags=re.IGNORECASE)
    s = re.sub(r'\bContact\s*:\s*\.', '', s, flags=re.IGNORECASE)
    s = re.sub(r'[ \t]{2,}', ' ', s)
    return s


def clean_line_typography(line: str) -> str:
    """
    Nettoie la typographie d'une ligne ou puce :
    - Supprime les balises HTML inline résiduelles.
    - Supprime les puces hétérogènes, les emojis corrompus ('?', '?️') et emojis décoratifs de tête/queue.
    - Réduit les ponctuations excessives (!!!!!!! -> !, ???? -> ?).
    - Supprime les espaces superflus et doubles virgules.
    """
    if not line:
        return ""

    clean = re.sub(r'</?[a-zA-Z][^>]*>', ' ', line)
    clean = html.unescape(clean)

    # Retrait des emojis corrompus en '?' / '????' / '?️' / '?‍♂️' en début, milieu ou fin de ligne
    clean = re.sub(r'^\s*(?:\?+[\ufe0f\u200d♂♀]*\s*)+(?=[A-Za-zÀ-ÿ0-9])', '', clean)
    clean = re.sub(r'\?[\ufe0f\u200d♂♀]+', '', clean)
    if not RHETORICAL_QUESTION_REGEX.match(clean):
        clean = re.sub(r'\s+\?{2,}[\ufe0f\u200d♂♀]*(?=\s|$|[.,;!])', '', clean)
        clean = re.sub(r'\s+\?\s*$', '', clean)

    # Retrait des puces hétérogènes et emojis de liste en début de ligne
    clean = re.sub(r'^\s*(?:[✅✨❗️⭐💥🔥•·\-\*_👉✔►▪▫–—#\ufe0f]|(?:\[x\]|\(x\)))+\s*', '', clean)
    # Retrait des emojis décoratifs en fin de ligne
    clean = re.sub(r'\s*(?:[✅✨❗️⭐💥🔥•·\-\*_👉✔►▪▫–—#\ufe0f]|(?:\[x\]|\(x\)))+\s*$', '', clean)

    # Réduction des ponctuations multiples et doubles virgules
    clean = re.sub(r',\s*,+', ',', clean)
    clean = re.sub(r'!{2,}', '!', clean)
    clean = re.sub(r'\?{2,}', '?', clean)
    clean = re.sub(r'\.{3,}', '...', clean)

    # Nettoyage des espaces
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean


HEADER_TRANSACTION_PREFIX_REGEX = re.compile(
    r'^\s*(?:(?:[àa]\s+louer|[àa]\s+vendre|en\s+location|en\s+vente|location\s+meubl[ée]e|location|vente|en\s+exclusivit[ée]|exclusivit[ée]|nouveaut[ée]|urgent|sous\s+offre|coup\s+de\s+c[oœ]ur)\b[\s\-–—:/|!]*)+',
    re.IGNORECASE
)

NC_PROPER_ACCENTS = {
    "kone": "Koné", "noumea": "Nouméa", "dumbea": "Dumbéa", "paita": "Païta",
    "poindimie": "Poindimié", "koumac": "Koumac", "bourail": "Bourail",
    "boulouparis": "Boulouparis", "la foa": "La Foa", "lifou": "Lifou",
    "ouvea": "Ouvéa", "mare": "Maré", "voh": "Voh", "pouembout": "Pouembout"
}


def normalize_header_banner(line: str) -> str:
    """
    Nettoie les bandeaux d'en-tête de type 'A LOUER - VILLA F4 / VILLAGE DE KONE' :
    - Supprime le préfixe commercial ('A LOUER - ', 'A VENDRE - ').
    - Si le reste ne contient qu'un type de bien nu ('VILLA F4'), renvoie '' (filtré).
    - Sinon, normalise les séparateurs (' / ', ' - ' -> ' — ') et la casse MAJUSCULE en casse lisible.
    """
    if not line:
        return ""
    raw_s = line.strip()
    # Conserver le mot seul 'EXCLUSIVITÉ' s'il est isolé, mais retirer 'A LOUER - ' / 'A VENDRE - '
    if re.match(r'^exclusivit[ée]\s*!*$', raw_s, flags=re.IGNORECASE):
        return "Exclusivité"

    s = HEADER_TRANSACTION_PREFIX_REGEX.sub('', raw_s).strip(' -–—:/|!')
    if not s:
        return ""

    # Si l'en-tête commençait par 'A LOUER / A VENDRE' suivi d'un type générique ('VILLA F4 / VILLAGE DE KONE'), ne garder que le lieu/détail factuel
    if s != raw_s:
        s = re.sub(
            r'^(?:villa|maison|appartement|studio|duplex)(?:\s+(?:de\s+type\s+)?[ft]\d)?\s*(?:/|[-–—])\s*',
            '',
            s,
            flags=re.IGNORECASE
        ).strip()

    # Si le bandeau restant est uniquement "VILLA F4" ou "APPARTEMENT F3" sans lieu ni détail -> redondant
    if re.match(r'^(?:villa|maison|appartement|studio|duplex|terrain|dock|local(?:\s+commercial)?|bureau[x]?)(?:\s+(?:de\s+type\s+)?[ft]\d)?$', s, flags=re.IGNORECASE):
        return ""

    if s != raw_s or (s.isupper() and len(s) > 6):
        s = re.sub(r'\s+(?:/|[-–—])\s+', ' — ', s)
        if s.isupper():
            words = s.split()
            norm_words = []
            for idx, w in enumerate(words):
                w_low = w.lower()
                if re.match(r'^(?:[ft]\d|rdc|wc|xpf|cfp|nc|pk|km|sde|sdb|r\+\d|r-\d|—|n°\d+)$', w_low):
                    norm_words.append(w.upper())
                elif w_low in NC_PROPER_ACCENTS:
                    norm_words.append(NC_PROPER_ACCENTS[w_low])
                elif idx > 0 and w_low in ("de", "du", "des", "d'", "la", "le", "les", "en", "sur", "à", "au", "aux", "et", "avec"):
                    norm_words.append(w_low)
                else:
                    norm_words.append(w.capitalize())
            s = " ".join(norm_words)
            for k_raw, k_acc in NC_PROPER_ACCENTS.items():
                s = re.sub(rf'\b{k_raw}\b', k_acc, s, flags=re.IGNORECASE)

    return s


def normalize_nc_phone(raw_phone: str) -> Optional[Dict[str, Any]]:
    """
    Normalise n'importe quel format de numéro de téléphone calédonien en :
    - digits : les 6 chiffres significatifs (ex: '289888' ou '854655')
    - formatted : '+687 289.888' ou '+687 85.46.55'
    - is_mobile : True si mobile (préfixe 7, 8, 9 ou 5), False si fixe (2, 3, 4)
    - whatsapp : '687854655' si mobile, None sinon
    """
    if not raw_phone:
        return None

    cleaned = re.sub(r'^(?:\+687|00687)[\s.-]*', '', raw_phone.strip())
    digits_only = re.sub(r'\D', '', cleaned)

    if len(digits_only) == 7 and digits_only.startswith('0'):
        digits_only = digits_only[1:]

    if len(digits_only) != 6:
        return None

    first_digit = digits_only[0]
    if first_digit not in '2345789':
        return None

    is_triplet = bool(re.match(r'^\d{3}[\s.-]\d{3}$', cleaned.strip()))
    if is_triplet:
        formatted = f"+687 {digits_only[:3]}.{digits_only[3:]}"
    else:
        formatted = f"+687 {digits_only[:2]}.{digits_only[2:4]}.{digits_only[4:]}"

    is_mobile = first_digit in ('5', '7', '8', '9')
    whatsapp = f"687{digits_only}" if is_mobile else None

    return {
        "digits": digits_only,
        "formatted": formatted,
        "is_mobile": is_mobile,
        "whatsapp": whatsapp
    }


def format_bullet_uniform(text: str) -> str:
    """
    Garantit un formatage d'item 100 % uniforme sur toutes les annonces :
    - Nettoyage typographique complet.
    - Traduction systématique des anglicismes éventuels produits par de petits LLMs.
    - Normalisation des unités (m2 -> m², F -> F CFP).
    - Première lettre alphabétique systématiquement en majuscule.
    - Suppression des ponctuations finales parasites (, ; :).
    """
    s = clean_line_typography(text)
    if not s:
        return ""

    anglicisms = [
        (r'\bkitchen\b', 'cuisine'),
        (r'\bbedrooms?\b', 'chambres'),
        (r'\bbathrooms?\b', 'salles de bain'),
        (r'\bbathroom\b', 'salle de bain'),
        (r'\bliving\s+room\b', 'séjour'),
        (r'\bswimming\s+pool\b', 'piscine'),
        (r'\bgarden\b', 'jardin'),
    ]
    for pat, repl in anglicisms:
        s = re.sub(pat, repl, s, flags=re.IGNORECASE)

    s = re.sub(r'(\d)\s*(?:m2|m²)\b', r'\1 m²', s, flags=re.IGNORECASE)
    s = re.sub(r'(\d[\d\s.]*\d{3})\s*(?:F|Frs?)\b(?!\s*CFP)', r'\1 F CFP', s, flags=re.IGNORECASE)
    # Nettoyage des accroches commerciales de tête résiduelles
    s = PROMO_LEADIN_REGEX.sub('', s).strip()
    # Nettoyage des queues verbales orphelines ("complète ce bien", "viennent compléter ce niveau")
    s = TRAILING_COMPLETION_REGEX.sub('', s).strip()
    s = MARKETING_TAIL_REGEX.sub('', s).strip()
    s = GENERIC_LIFESTYLE_TAIL_REGEX.sub('', s).strip()
    s = re.sub(r'[,;:]+\s*$', '', s).strip()
    # Retrait du point final isolé pour uniformité des puces courtes
    if s.endswith('.') and not s.endswith('...') and len(s) < 120 and '.' not in s[:-1]:
        s = s[:-1].strip()

    if s and s[0].isalpha() and s[0].islower():
        s = s[0].upper() + s[1:]

    return s


# ============================================================================
# MOTEUR DE DÉCOMPOSITION SÉMANTIQUE ATOMIQUE & PROPAGATION DES NIVEAUX
# ============================================================================

ROOM_AND_FEATURE_NOUNS_PATTERN = (
    r'cuisine|s[ée]jour|salon|salle|chambres?|suite|sdb|sde|wc|toilettes?|douche|baignoire|'
    r'buanderie|lingerie|cellier|dressing|placards?|penderie|rangement|d[ée]barras|'
    r'espace|coin|pi[èe]ces?|entr[ée]e|hall|sas|d[ée]gagement|couloir|palier|mezzanine|'
    r'bureaux?|terrasses?|varangues?|jardin|piscine|balcons?|deck|v[ée]randa|loggia|patio|'
    r'pergola|far[ée]|fare|carbet|cour|terrain|parcelle|garages?|parkings?|stationnements?|'
    r'places?|carports?|box|dock|atelier|remise|cave|sous-sol|studio|bungalow|d[ée]pendance|'
    r'annexe|local|accueil|caf[ée]t[ée]ria|sanitaires?|container|abri|vue|climatis|volets?|'
    r'chauffe-eau|alarme|panneaux?|portail|spa|jacuzzi'
)

LEVEL_HEADER_REGEX = re.compile(
    r'^\s*(?P<marker>'
    r'(?:au\s+|en\s+)?(?:rez[- ]de[- ]chauss[ée]e|rez[- ]de[- ]jardin|rdc|niveau\s+principal|premier\s+niveau|1er\s+niveau)'
    r'|(?:[àa]\s+l[\'’]\s*[ée]tage|au\s+(?:1er|premier|2e|2[èe]me|second|dernier)\s+[ée]tage|(?:au|en)\s+r\s*\+\s*\d+|au\s+niveau\s+sup[ée]rieur|en\s+mezzanine|sur\s+la\s+mezzanine)'
    r'|(?:au\s+niveau\s+inf[ée]rieur|en\s+sous[- ]sol|au\s+sous[- ]sol|en\s+r\s*-\s*1|au\s+r\s*-\s*1)'
    r')\b(?!\s+(?:de\s+la|de\s+l[\'’]|du\s+|d[\'’]un))\s*[,:\-–]?\s*(?P<rest>.*)$',
    re.IGNORECASE
)

LEVEL_RESET_REGEX = re.compile(
    r'^\s*(?:pour\s+(?:un\s+|votre\s+)?confort|c[ôo]t[ée]\s+(?:confort|[ée]quipements?|ext[ée]rieur|jardin|prestations?)|'
    r'[àa]\s+l[\'’]ext[ée]rieur|en\s+ext[ée]rieur|les\s+plus\b|prestations?\s*:|conditions?\s*:|prix\s*:|loyer\s*:)',
    re.IGNORECASE
)

INTRO_LAND_TAIL_REGEX = re.compile(
    r'^(?P<head>.*?\b(?:villa|maison|appartement|duplex|triplex|studio|propri[ée]t[ée]|habitation|bungalow|f\d|t\d|m²|m2|habitable[s]?)\b[^.;]*?)'
    r'\s*,?\s*(?:[ée]difi[ée]e?\s+|implant[ée]e?\s+|b[âa]tie?\s+|construite?\s+|sis[e]?\s+)?'
    r'sur\s+(?:un\s+|une\s+)?(?P<land>(?:tr[èe]s\s+)?(?:beau\s+|belle\s+|grand\s+|grande\s+|joli[e]?\s+|vaste\s+)?(?:terrain|parcelle)\b.+)$',
    re.IGNORECASE
)

INTRO_PIVOT_REGEX = re.compile(
    r'^(?P<loc_part>.*?),\s*'
    r'(?:(?:d[ée]couvrez|venez\s+d[ée]couvrir|nous\s+vous\s+proposons|laissez-vous\s+s[ée]duire\s+par|vous\s+serez\s+s[ée]duits?\s+par|profitez\s+de)\s+)?'
    r'(?:cette|ce|cet|une|un)\s+'
    r'(?:tr[èe]s\s+)?(?:charmante?|belle|beau|magnifique|superbe|joli[e]?|agr[ée]able|vaste|grande?|spacieuse?|spacieux|rare)?\s*'
    r'(?P<prop_part>(?:villa|maison|appartement|studio|duplex|triplex|propri[ée]t[ée]|habitation|bungalow|bas\s+de\s+villa|haut\s+de\s+villa|f\d|t\d)\b.+)$',
    re.IGNORECASE
)

SEDUCTION_HOOK_REGEX = re.compile(
    r'^(?:cette|ce|cet|une|un|la|le)\s+'
    r'(?:tr[èe]s\s+)?(?:charmante?|belle|beau|magnifique|superbe|joli[e]?|agr[ée]able|vaste|grande?)?\s*'
    r'(?P<subj>(?:villa|maison|appartement|studio|duplex|triplex|propri[ée]t[ée]|habitation|bungalow|f\d|t\d)\b[^,.;]*?)\s+'
    r'(?:vous\s+s[ée]duira|saura\s+vous\s+s[ée]duire|vous\s+charmera|vous\s+plaira|se\s+distingue|se\s+caract[ée]rise)\s+par\s+'
    r'(?P<qualities>.+)$',
    re.IGNORECASE
)

# Accroches promotionnelles de début de phrase (supprime l'habillage marketing, conserve 100 % des faits)
PROMO_LEADIN_REGEX = re.compile(
    r'^\s*(?:'
    r'(?:en\s+exclusivit[ée]|exclusivit[ée]|coup\s+de\s+c[oœ]ur|opportunit[ée]\s+(?:unique|rare|[àa]\s+saisir)|[àa]\s+ne\s+pas\s+manquer)\s*[\-–—:!,]*\s*'
    r'|(?:venez\s+d[ée]couvrir|d[ée]couvrez|laissez[- ]vous\s+s[ée]duire\s+par|nous\s+vous\s+proposons|offrez\s+[àa]\s+votre\s+activit[ée]|profitez\s+de|ne\s+manquez\s+pas)\s+(?:ce|cet|cette|un|une|le|la|les)?\s*(?:tr[èe]s\s+)?(?:bel|beau|belle|charmant[e]?|spacieux|spacieuse|magnifique|superbe|joli[e]?|agr[ée]able|vaste|grand[e]?|rare|lumineux|lumineuse)?\s*'
    r'|(?:les\s+atouts\s+de\s+ce\s+bien\s+ne\s+s[\'’]arr[êe]tent\s+pas\s+l[àa]\s*:\s*(?:il|elle)\s+dispose\s+(?:[ée]galement\s+)?(?:d[\'’]un[e]?\s+|de\s+|d[\'’])?)'
    r'|(?:(?:son|le)\s+v[ée]ritable\s+atout\s*:\s*(?:un[e]?\s+)?(?:agr[ée]able\s+|bel\s+|beau\s+|belle\s+)?)'
    r'|(?:[àa]\s+noter\s+que\s+(?:la\s+|le\s+|l[\'’])?)'
    r'|(?:beaucoup\s+de\s+charme\s+et\s+de\s+beaux\s+volumes\s+pour\s+(?:ce|cet|cette)\s+)'
    r'|(?:id[ée]alement\s+(?=situ[ée]e?\b))'
    r')',
    re.IGNORECASE
)

GENERIC_LIFESTYLE_TAIL_REGEX = re.compile(
    r',?\s*(?:elle|il|cette\s+(?:villa|maison|propri[ée]t[ée])|cet\s+appartement|ce\s+bien)?\s*'
    r'(?:vous\s+)?(?:offre|propose|promet|assure|garantit|constituera)\s+'
    r'(?:un\s+(?:cadre\s+de\s+vie|environnement|lieu\s+de\s+vie|confort\s+de\s+vie)\s+'
    r'(?:tr[èe]s\s+)?(?:pratique|agr[ée]able|id[ée]al|paisible|privil[ée]gi[ée]|exceptionnel|unique|calme|chaleureux|convivial|fonctionnel)'
    r'(?:\s+et\s+(?:pratique|agr[ée]able|id[ée]al|paisible|privil[ée]gi[ée]|calme|chaleureux|convivial|fonctionnel))?'
    r'(?:,\s*avec\s+des\s+espaces\s+fonctionnels[^.]*)?'
    r'|id[ée]al[e]?\s+pour\s+(?:une\s+famille|un\s+couple|investir|habiter))\b[^.]*\.?$',
    re.IGNORECASE
)

EQUIPMENT_SENTENCE_REGEX = re.compile(
    r'^(?:pour\s+(?:un\s+|votre\s+)?confort\s+(?:optimal|absolu|au\s+quotidien|parfait)\s*[,:\-–]?\s*)?'
    r'(?:c[ôo]t[ée]\s+(?:confort|[ée]quipements?|prestations?|technique)\s*[,:\-–]?\s*)?'
    r'(?:(?P<subj>la\s+villa|la\s+maison|l[\'’]appartement|le\s+bien|le\s+logement|cette\s+villa|cette\s+maison|cet\s+appartement|elle|il)\s+'
    r'(?:est\s+(?P<state_feat>(?:enti[èe]rement\s+|totalement\s+|compl[èe]tement\s+)?climatis[ée]e?(?:\s+dans\s+toutes\s+les\s+pi[èe]ces)?)\s+et\s+)?)?'
    r'(?:(?P<subj2>la\s+villa|la\s+maison|l[\'’]appartement|le\s+bien|le\s+logement|cette\s+villa|cette\s+maison|cet\s+appartement|elle|il)\s*,?\s*(?:bien\s+entretenue?\s*,\s*)?)?'
    r'(?:est\s+[ée]quip[ée]e?|est\s+dot[ée]e?|[ée]quip[ée]e?|dot[ée]e?|dispose|b[ée]n[ée]ficie|profite|est\s+munie?)\s+'
    r'(?:[ée]galement\s+|aussi\s+|en\s+outre\s+)?'
    r'(?:d[\'’]un[e]?\s+|de\s+la\s+|du\s+|des\s+|de\s+)(?P<equip_list>.+)$',
    re.IGNORECASE
)

LEADING_NARRATIVE_WRAPPER_REGEX = re.compile(
    r'^\s*(?:'
    r'(?:(?:d[èe]s\s+l[\'’]entr[ée]e|[àa]\s+l[\'’]int[ée]rieur|c[ôo]t[ée]\s+(?:nuit|jour|vie|cuisine|int[ée]rieur|pratique)|en\s+partie\s+(?:nuit|jour))\s*[,:\-–]?\s*)?'
    r'(?:(?:cette|ce|cet|la|le|l[\'’])\s*(?:maison(?:\s+en\s+dur)?|villa|propri[ée]t[ée]|appartement|studio|duplex|triplex|haut\s+de\s+villa|bas\s+de\s+villa|bien|logement|habitation|int[ée]rieur|niveau|[ée]tage|espace\s+nuit|espace\s+jour|espace\s+vie|coin\s+nuit|coin\s+jour|partie\s+nuit|partie\s+jour|plateau|local|dock|ensemble)|elle|il)\s+'
    r'(?:offre|propose|comprend|comporte|poss[èe]de|pr[ée]sente|inclut|dessert|distribue|accueille|r[ée]unit|'
    r'se\s+compose\s*(?:[ée]galement\s*)?(?:en\s+rez[- ]de[- ]chauss[ée]e\s+|au\s+rdc\s+|[àa]\s+l[\'’][ée]tage\s+)?(?:comme\s+suit\s*:\s*|:?\s*d[\'’]un[e]?\s+|:?\s*de\s+|:?\s*d[\'’]|:?\s+)|'
    r'est\s+compos[ée]e?\s*(?:en\s+rez[- ]de[- ]chauss[ée]e\s+|au\s+rdc\s+|[àa]\s+l[\'’][ée]tage\s+)?(?:comme\s+suit\s*:\s*|:?\s*d[\'’]un[e]?\s+|:?\s*de\s+|:?\s*d[\'’])|'
    r'est\s+constitu[ée]e?\s+(?:d[\'’]un[e]?\s+|de\s+|d[\'’])|'
    r'dispose\s+(?:[ée]galement\s+)?(?:d[\'’]un[e]?\s+|de\s+|d[\'’])|'
    r'b[ée]n[ée]ficie\s+(?:[ée]galement\s+)?(?:d[\'’]un[e]?\s+|de\s+|d[\'’])|'
    r's[\'’]ouvre\s+sur\s+)\s*'
    r'(?:un\s+(?:tr[èe]s\s+)?(?:bel|beau|vaste|grand|agr[ée]able)\s+espace\s+de\s+vie\s+(?:compos[ée]\s+d[\'’]|comprenant\s+))?'
    r'|(?:(?:d[èe]s\s+l[\'’]entr[ée]e|[àa]\s+l[\'’]int[ée]rieur|c[ôo]t[ée]\s+(?:nuit|jour|vie|cuisine|ext[ée]rieur|jardin))\s*[,:\-–]?\s*)?(?:vous\s+(?:y\s+)?(?:trouverez|d[ée]couvrirez|profiterez\s+(?:[ée]galement\s+)?d[\'’]un[e]?|profiterez\s+de|disposerez\s+de|appr[ée]cierez|retrouverez|b[ée]n[ée]ficierez\s+(?:[ée]galement\s+)?d[\'’]un[e]?)|on\s+(?:y\s+)?(?:trouve|retrouve))\s+'
    r'|(?:un\s+(?:tr[èe]s\s+)?(?:bel|beau|vaste|grand|agr[ée]able)\s+espace\s+de\s+vie\s+(?:compos[ée]\s+d[\'’]|comprenant\s+))'
    r'|(?:comprenant|compos[ée]e?\s+de|disposant\s+de)\s*:?\s+'
    r')',
    re.IGNORECASE
)

# Pivot interne en milieu de phrase ("Appartement F2 situé à... comprenant : une cuisine...", "Bas de villa F2 avec jardin composé d'un coin cuisine...")
INTERNAL_COMPOSITION_PIVOT_REGEX = re.compile(
    r'^(?P<intro_head>[^:;]{10,220}?)'
    r'(?:,\s*|\s+)'
    r'(?:(?:il|elle|ce\s+bien|cet\s+appartement|cette\s+villa|cette\s+maison|le\s+logement)\s+)?'
    r'(?:se\s+compose\s+(?:comme\s+suit\s*:?\s*|d[\'’]|de\s+)|est\s+compos[ée]e?\s+(?:comme\s+suit\s*:?\s*|d[\'’]|de\s+)|'
    r'compos[ée]e?\s+(?:comme\s+suit\s*:?\s*|d[\'’]|de\s+)|'
    r'comprenant\s*(?:au\s+rdc\s*:?|à\s+l[\'’][ée]tage\s*:?|en\s+son\s+rez[- ]de[- ]chauss[ée]e\s*:?)?\s*:?\s*|'
    r'comprend\s+(?:en\s+son\s+rez[- ]de[- ]chauss[ée]e\s+|au\s+rdc\s+|à\s+l[\'’][ée]tage\s+)?)'
    r'(?P<comp_tail>.+)$',
    re.IGNORECASE
)

HALLWAY_DISTRIBUTOR_REGEX = re.compile(
    r'^\s*(?:un\s+|une\s+|le\s+|la\s+|l[\'’])?(?:d[ée]gagement|couloir|hall(?:\s+d[\'’]entr[ée]e)?|entr[ée]e(?:\s+accueillante)?|palier)\s+'
    r'(?:distribuant|desservant|menant\s+[àa]|donnant\s+(?:acc[èe]s\s+[àa]|sur)|qui\s+distribue|qui\s+dessert)\s+',
    re.IGNORECASE
)

TRAILING_COMPLETION_REGEX = re.compile(
    r'\s+(?:(?:viennent|vient)\s+compl[ée]ter|compl[èe]te(?:nt)?|parach[èe]ve(?:nt)?)\s+'
    r'(?:ce\s+niveau|le\s+niveau|cet\s+[ée]tage|cet\s+ensemble|cette\s+villa|cette\s+maison|cette\s+propri[ée]t[ée]|la\s+propri[ée]t[ée]|cet\s+appartement|ce\s+bien|le\s+tout|l[\'’]ensemble)\.?$',
    re.IGNORECASE
)

# Queues participiales purement marketing en fin de puce (", idéale pour profiter de l'extérieur en toute saison")
MARKETING_TAIL_REGEX = re.compile(
    r',?\s*(?:id[ée]al[e]?\s+pour\s+(?:profiter|recevoir|vos\s+moments|partager)|parfait[e]?\s+pour\s+(?:profiter|vos\s+moments|recevoir)|permettant\s+de\s+profiter\s+pleinement|n[\'’]attend\s+plus\s+que\s+vous)[^.]*\.?$',
    re.IGNORECASE
)

COMMA_FEATURE_SPLIT_REGEX = re.compile(
    r'(?:(?<!\d),(?!\d)|,\s+)(?:,\s*)?\s*(?=(?:(?:et|ainsi\s+que)\s+)?(?:'
    # Cas A : (d')un / (d')une suivi d'un nom (hors "d'une superficie/surface/capacité")
    r'(?:d[\'’]\s*|de\s+)?(?:un\b|une\b)(?!\s+(?:superficie|surface|total|montant|valeur|capacit[ée]|c[ôo]t[ée]|part)\b)'
    r'|'
    # Cas B : Partitif optionnel + nombre / adjectif / nom de pièce ou prestation
    r'(?:d[\'’]\s*un[e]?\s+|de\s+la\s+|de\s+l[\'’]\s*|du\s+|des\s+|de\s+|d[\'’]\s*)?'
    r'(?:'
    r'\d+(?:[.,]\d+)?(?![0-9.,])\s*(?!(?:m²|m2|ares?|centiares?|ha\b|%|f\b|xpf\b|cfp\b|mf\b|m\b|millions?\b|€|km\b|h\b|min\b|ans?\b|000\b))|'
    r'(?:deux|trois|quatre|cinq|six|plusieurs|nombreux|nombreuses|seconde?|deuxi[èe]me|troisi[èe]me|quatri[èe]me)\b|'
    r'(?:grand[es]?|beau[x]?|belle[s]?|joli[es]?|vaste[s]?|large[s]?|spacieux|spacieuse[s]?|agr[ée]able[s]?|lumineux|lumineuse[s]?|petit[es]?|double|triple)\b|'
    r'(?:' + ROOM_AND_FEATURE_NOUNS_PATTERN + r')\b'
    r')'
    r'))',
    re.IGNORECASE
)

NUMBER_WORDS_MAP = [
    (re.compile(r'\bdeux\b', re.IGNORECASE), '2'),
    (re.compile(r'\btrois\b', re.IGNORECASE), '3'),
    (re.compile(r'\bquatre\b', re.IGNORECASE), '4'),
    (re.compile(r'\bcinq\b', re.IGNORECASE), '5'),
    (re.compile(r'\bsix\b', re.IGNORECASE), '6'),
    (re.compile(r'\bpremi[èe]re\b', re.IGNORECASE), '1ère'),
    (re.compile(r'\btroisi[èe]me\b', re.IGNORECASE), '3ème'),
    (re.compile(r'\bquatri[èe]me\b', re.IGNORECASE), '4ème'),
]


def parse_level_prefix(text: str, current_level: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Détecte un marqueur de niveau en début de phrase/paragraphe, normalise le tag de niveau
    ('RDC', 'à l\'étage', 'en mezzanine', 'en sous-sol', 'au niveau inférieur')
    et renvoie (texte_sans_marqueur, nouveau_level_ctx).
    """
    if LEVEL_RESET_REGEX.match(text):
        return text, None

    m = LEVEL_HEADER_REGEX.match(text)
    if not m:
        return text, current_level

    raw_marker = m.group("marker").lower()
    rest = m.group("rest").strip()

    if any(k in raw_marker for k in ("rez-de-jardin", "rez de jardin")):
        level_tag = "en rez-de-jardin"
    elif any(k in raw_marker for k in ("rez-de-chauss", "rez de chauss", "rdc", "niveau principal", "premier niveau", "1er niveau")):
        level_tag = "RDC"
    elif "mezzanine" in raw_marker:
        level_tag = "en mezzanine"
    elif any(k in raw_marker for k in ("sous-sol", "sous sol", "r-1", "r - 1", "inférieur", "inferieur")):
        level_tag = "au niveau inférieur" if "inf" in raw_marker else "en sous-sol"
    else:
        level_tag = "à l'étage"

    return rest, level_tag


def _split_land_and_exterior_tail(land_str: str) -> List[str]:
    """Découpe 'terrain clos de 4 ares avec piscine' en ['Terrain clos de 4 ares', 'Piscine']."""
    clean_land = land_str.strip().rstrip('.')
    parts = re.split(
        r'\s+(?:avec|comprenant|disposant\s+d[\'’]un[e]?|dot[ée]e?\s+d[\'’]un[e]?|agr[ée]ment[ée]e?\s+d[\'’]un[e]?)\s+',
        clean_land,
        maxsplit=1,
        flags=re.IGNORECASE
    )
    out = [parts[0].strip()]
    if len(parts) > 1:
        ext_items = re.split(r',\s*|\s+(?:et|ainsi\s+que)\s+', parts[1].strip(), flags=re.IGNORECASE)
        for item in ext_items:
            item_clean = re.sub(r'^(?:un|une|des|le|la|les|d[\'’]un[e]?|de\s+la|du|de)\s+', '', item.strip(), flags=re.IGNORECASE)
            if item_clean:
                out.append(item_clean)
    return out


def _synthesize_intro_head(head_str: str) -> List[str]:
    """
    Sépare et synthétise la partie situation/commodités et la partie bien/surface habitable.
    """
    bullets = []
    m_piv = INTRO_PIVOT_REGEX.match(head_str.strip())
    if m_piv:
        loc_part = m_piv.group("loc_part").strip()
        prop_part = m_piv.group("prop_part").strip()

        loc_clean = re.sub(
            r'^(?:id[ée]alement\s+)?(?:situ[ée]e?|nich[ée]e?|implant[ée]e?|localis[ée]e?)\s+(?:dans\s+un\s+|dans\s+une\s+|au\s+sein\s+d[\'’]un[e]?\s+)?',
            '',
            loc_part,
            flags=re.IGNORECASE
        ).strip()

        loc_sub = re.split(
            r'\s+et\s+(?=[àa]\s+proximit[ée]|proche\s+de|proche\s+des)|,\s*(?=[àa]\s+proximit[ée]\b)',
            loc_clean,
            flags=re.IGNORECASE
        )
        for ls in loc_sub:
            if ls.strip():
                bullets.append(ls.strip())

        prop_clean = re.sub(r',\s*en\s+copropri[ée]t[ée]\s*,?', ' en copropriété ', prop_part, flags=re.IGNORECASE)
        prop_clean = re.sub(
            r',?\s*(?:offrant|d[ée]veloppant|proposant|avec|d[\'’])\s*une\s+(?:superficie|surface)\s+habitable\s+(?:d[\'’]environ\s+|de\s+)(\d+(?:[.,]\d+)?\s*(?:m²|m2))',
            r' de \1 habitables',
            prop_clean,
            flags=re.IGNORECASE
        )
        prop_clean = re.sub(r'\s+', ' ', prop_clean).strip(' ,.')
        if prop_clean:
            bullets.append(prop_clean)
        return bullets

    return [head_str.strip()]


def _synthesize_narrative_hook_sentence(line: str) -> Optional[List[str]]:
    """
    Synthétise les phrases d'accroche narratives en puces purement factuelles et atomiques :
    1. 'Cette villa F4 d'environ 115 m² vous séduira par ses volumes et sa situation idéale en plein centre du village.'
       -> ["Villa F4 d'environ 115 m²", "En plein centre du village"]
    2. 'Située à proximité immédiate de toutes les commodités, elle offre un cadre de vie pratique et agréable.'
       -> ["À proximité immédiate de toutes les commodités"]
    """
    clean = line.strip().rstrip('.')

    m_sed = SEDUCTION_HOOK_REGEX.match(clean)
    if m_sed:
        subj = format_bullet_uniform(m_sed.group("subj").strip())
        qual = m_sed.group("qualities").strip()
        qual_concrete = re.sub(
            r'^(?:(?:ses\s+(?:beaux\s+|grands\s+)?volumes|son\s+charme|son\s+confort|son\s+agencement|sa\s+luminosit[ée]|sa\s+fonctionnalit[ée]|son\s+potentiel)\s*(?:,\s*|\s+et\s+))+',
            '',
            qual,
            flags=re.IGNORECASE
        ).strip()
        qual_concrete = re.sub(r'^(?:sa|son|ses|leur)\s+', '', qual_concrete, flags=re.IGNORECASE).strip()
        qual_concrete = re.sub(
            r'^(?:situation\s+id[ée]ale|emplacement\s+id[ée]al|cadre\s+id[ée]al|emplacement\s+privil[ée]gi[ée])\s*',
            '',
            qual_concrete,
            flags=re.IGNORECASE
        ).strip(' ,-')
        out_hooks = [subj] if subj else []
        if qual_concrete:
            out_hooks.append(format_bullet_uniform(qual_concrete))
        return out_hooks

    if GENERIC_LIFESTYLE_TAIL_REGEX.search(clean):
        stripped = GENERIC_LIFESTYLE_TAIL_REGEX.sub('', clean).strip(' ,.')
        stripped = re.sub(
            r'^(?:id[ée]alement\s+)?(?:situ[ée]e?|nich[ée]e?|implant[ée]e?|localis[ée]e?)\s+(?:dans\s+un\s+|dans\s+une\s+|au\s+sein\s+d[\'’]un[e]?\s+)?',
            '',
            stripped,
            flags=re.IGNORECASE
        ).strip()
        return [format_bullet_uniform(stripped)] if stripped else []

    return None


def _decompose_equipment_sentence(line: str) -> Optional[List[str]]:
    """
    Découpe une phrase d'équipements combinée en puces atomiques homogènes :
    Ex: 'Pour un confort optimal, la villa est entièrement climatisée et dispose d'un système d'alarme, d'un chauffe-eau solaire ainsi que de panneaux photovoltaïques.'
    """
    clean = line.strip().rstrip('.')
    m = EQUIPMENT_SENTENCE_REGEX.match(clean)
    if not m:
        return None

    equip_list_str = m.group("equip_list").strip()
    equip_check_terms = (
        "alarme", "chauffe-eau", "chauffe eau", "solaire", "photovolta", "panneau",
        "volet", "fosse", "groupe", "citerne", "fibre", "portail", "moustiquaire",
        "climatisation", "climatis", "caméra", "vidéo", "visiophone", "onduleur", "double vitrage", "pompe à chaleur"
    )
    if not any(t in clean.lower() for t in equip_check_terms):
        return None

    out: List[str] = []
    state_feat = m.group("state_feat")
    subj = m.group("subj") or m.group("subj2") or ""
    if state_feat:
        subj_noun = re.sub(r'^(?:la|le|l[\'’]|cette|cet)\s+', '', subj.strip(), flags=re.IGNORECASE)
        if subj_noun.lower() in ("elle", "il", ""):
            subj_noun = "Propriété"
        out.append(f"{subj_noun.capitalize()} {state_feat.strip()}")

    raw_items = re.split(
        r',\s*(?:et\s+)?|\s+(?:ainsi\s+que|et|mais\s+aussi|de\s+m[êe]me\s+que)\s+',
        equip_list_str,
        flags=re.IGNORECASE
    )
    for item in raw_items:
        item_c = re.sub(
            r'^(?:d[\'’]un[e]?|de\s+la|du|des|de|un|une|le|la|les)\s+',
            '',
            item.strip().rstrip('.'),
            flags=re.IGNORECASE
        ).strip()
        if item_c and len(item_c) >= 3:
            out.append(item_c)

    return out if len(out) >= 1 else None


def _split_clause_connectors(clause: str) -> List[str]:
    """
    Sous-découpe une clause sur :
    - 'ainsi que (d'un/d'une/de...)'
    - 'ouvrant sur / donnant sur / ouvert(e) sur / prolongé(e) par'
    - 'un dégagement distribuant...'
    - 'et (d'un/d'une/un/une/deux...)' introduisant une nouvelle pièce distincte
    """
    s = TRAILING_COMPLETION_REGEX.sub('', clause.strip()).strip(' .')
    s = LEADING_NARRATIVE_WRAPPER_REGEX.sub('', s).strip()
    s = HALLWAY_DISTRIBUTOR_REGEX.sub('', s).strip()
    if not s:
        return []

    parts_ainsi = re.split(
        r'\s*,?\s*(?:ainsi\s+qu(?:e\s+d[\'’]\s*|e\s+de\s+la\s+|e\s+de\s+l[\'’]\s*|e\s+du\s+|e\s+des\s+|e\s+de\s+|e\s+|[\'’]\s*)|de\s+m[êe]me\s+qu(?:e\s+d[\'’]\s*|e\s+|[\'’]\s*))'
        r'(?=(?:un\b|une\b|des\b|du\b|de\b|le\b|la\b|les\b|l[\'’]|\d+|deux\b|trois\b|quatre\b|' + ROOM_AND_FEATURE_NOUNS_PATTERN + r'))',
        s,
        flags=re.IGNORECASE
    )

    out: List[str] = []
    for pa in parts_ainsi:
        pa_clean = HALLWAY_DISTRIBUTOR_REGEX.sub('', pa.strip()).strip()
        parts_ouvrant = re.split(
            r'\s*,?\s*(?:(?:s[\'’])?(?:ouvrant|donnant|ouvert[es]?)\s+sur|prolong[ée]e?s?\s+(?:par|d[\'’]))\s+'
            r'(?=(?:un[e]?\s+|la\s+|le\s+|sa\s+|son\s+)(?:tr[èe]s\s+)?(?:belle\s+|beau\s+|grande?\s+|vaste\s+|joli[e]?\s+|g[ée]n[ée]reuse\s+|magnifique\s+|agr[ée]able\s+|spacieux\s+|spacieuse\s+)?'
            r'(?:terrasse|varangue|jardin|piscine|balcon|deck|v[ée]randa|loggia|patio|pergola|far[ée]|fare|cour|ext[ée]rieur|salon|s[ée]jour|buanderie|cellier))',
            pa_clean,
            flags=re.IGNORECASE
        )
        for po in parts_ouvrant:
            if re.search(r'\b(?:accompagn[ée]e?|disposant|dot[ée]e?|munie?)\s+d[\'’]', po, flags=re.IGNORECASE):
                out.append(po.strip())
                continue

            parts_et = re.split(
                r'\s+et\s+(?='
                r'(?:d[\'’]\s*un[e]?\s+|de\s+la\s+|de\s+l[\'’]\s*|du\s+|des\s+|de\s+|un\s+|une\s+|le\s+|la\s+|les\s+|sa\s+|son\s+)?'
                r'(?:(?:deux|trois|quatre|cinq|\d+|second[e]?|deuxi[èe]me|troisi[èe]me|autre[s]?)\s+)?'
                r'(?:(?:tr[èe]s\s+)?(?:grand[es]?|beau[x]?|belle[s]?|joli[es]?|vaste[s]?|spacieux|spacieuse[s]?|petit[es]?|agr[ée]able[s]?|lumineux|lumineuse[s]?)\s+)?'
                r'(?:bureau[x]?|chambres?|salles?\s+d|sdb\b|sde\b|wc\b|unk|suites?|garages?|parkings?|places?\s+de\s+parking|carports?|celliers?|buanderie|cuisine|s[ée]jour|salon|terrasses?|varangues?|jardin|piscine|v[ée]randa|mezzanine)\b'
                r')',
                po.strip(),
                flags=re.IGNORECASE
            )
            for pe in parts_et:
                if pe.strip():
                    out.append(pe.strip())

    return out


def synthesize_atomic_bullet(segment: str, level_ctx: Optional[str] = None) -> str:
    """
    Transforme un segment brut en une puce synthétique, nominale, courte et standardisée :
    - Supprime les wrappers narratifs, les prépositions partitives ('d'un', 'd'une', 'de') et les queues marketing.
    - Convertit les nombres en chiffres ('deux' -> '2', 'trois' -> '3').
    - Ajoute le suffixe de niveau ('(RDC)', '(À l'étage)') si applicable.
    """
    if not segment:
        return ""

    s = TRAILING_COMPLETION_REGEX.sub('', segment.strip()).strip(' .,;')
    s = LEADING_NARRATIVE_WRAPPER_REGEX.sub('', s).strip()
    s = HALLWAY_DISTRIBUTOR_REGEX.sub('', s).strip()
    s = MARKETING_TAIL_REGEX.sub('', s).strip(' .,;')

    def _repl_dont_une(m: re.Match) -> str:
        feat = m.group("feat").strip().rstrip('.')
        feat = re.sub(r'\bsalle\s+de\s+bains?\s+avec\s+wc\b', 'salle de bains et WC', feat, flags=re.IGNORECASE)
        feat = re.sub(r'\bsalle\s+d[\'’]eau\s+avec\s+wc\b', "salle d'eau et WC", feat, flags=re.IGNORECASE)
        return f" (dont 1 avec {feat})"

    s = re.sub(
        r',\s*dont\s+(?:une|l[\'’]une|1)\s+(?:b[ée]n[ée]ficiant\s+de|disposant\s+de|dot[ée]e?\s+de|[ée]quip[ée]e?\s+de|avec|poss[ée]dant)?\s*(?:sa\s+propre\s+|son\s+propre\s+|une\s+|un\s+)?(?P<feat>[^.]+)$',
        _repl_dont_une,
        s,
        flags=re.IGNORECASE
    )

    if re.search(r',?\s*\b(?:accompagn[ée]e?s?|disposant|dot[ée]e?s?|b[ée]n[ée]ficiant|munie?s?)\s+(?:d[\'’]|de\s+)', s, flags=re.IGNORECASE):
        s = re.sub(
            r',?\s*\b(?:accompagn[ée]e?s?|disposant|dot[ée]e?s?|b[ée]n[ée]ficiant|munie?s?)\s+(?:d[\'’]un[e]?\s+|de\s+sa\s+propre\s+|de\s+son\s+propre\s+|de\s+sa\s+|de\s+son\s+|de\s+)',
            ' avec ',
            s,
            flags=re.IGNORECASE
        )
        s = re.sub(r'\s+(?:et|ainsi\s+que)\s+(?:d[\'’]un[e]?\s+|de\s+sa\s+|de\s+)', ' et ', s, flags=re.IGNORECASE)
        s = re.sub(r'\bsalle\s+d[\'’]eau\s+avec\s+wc\b', "salle d'eau/WC", s, flags=re.IGNORECASE)
        s = re.sub(r'\bsalle\s+de\s+bains?\s+avec\s+wc\b', 'salle de bains/WC', s, flags=re.IGNORECASE)
        s = re.sub(r'\bavec\s+dressing\s+avec\s+', 'avec dressing et ', s, flags=re.IGNORECASE)

    s = re.sub(r'\benti[èe]rement\s+(?=am[ée]nag[ée]e?|[ée]quip[ée]e?)', '', s, flags=re.IGNORECASE)

    # Retrait des prépositions partitives initiales ("d'un ", "d'une ", "de la ", "de l'", "du ", "des ", "de ", "d'")
    s = re.sub(
        r'^(?:(?:d[\'’]\s*(?:un|une)\b|de\s+la\b|de\s+l[\'’]|du\b|des\b|de(?!\s+plain[- ]pied)\b|d[\'’](?!environ\b))\s*)+',
        '',
        s,
        flags=re.IGNORECASE
    ).strip()

    for pat, repl in NUMBER_WORDS_MAP:
        s = pat.sub(repl, s)

    s = re.sub(r'^(?:un|une|le|la|les|l[\'’])\s+', '', s, flags=re.IGNORECASE).strip()

    if level_ctx:
        s_low = s.lower()
        if not any(k in s_low for k in ("étage", "etage", "rdc", "rez-de-", "mezzanine", "sous-sol", "niveau")):
            if any(k in s_low for k in ("chambre", "suite", "bureau", "dressing", "salle", "wc", "toilette", "terrasse", "varangue", "balcon", "salon", "séjour", "cuisine", "buanderie", "garage", "cellier", "rangement")):
                lvl_label = level_ctx if level_ctx == "RDC" else (level_ctx[0].upper() + level_ctx[1:])
                s = f"{s} ({lvl_label})"

    return format_bullet_uniform(s)


def _split_comma_feature_clauses(clause_str: str) -> List[str]:
    """
    Découpe une énumération de caractéristiques séparées par des virgules
    (incluant les prépositions partitives 'd'un', 'd'une', 'de trois', 'des', 'du')
    tout en préservant les compléments liés ('dont 1 privative', 'de 15 m²', etc.).
    """
    parts = COMMA_FEATURE_SPLIT_REGEX.split(clause_str)
    out = []
    for p in parts:
        sub_et = re.split(
            r'\s+et\s+(?=(?:d[\'’]\s*un[e]?\s+|de\s+|un\b|une\b|\d+|deux\b|trois\b)\s*(?:garage|parking|place|carport|terrasse|varangue|chambre|salle|cellier|piscine|jardin|wc|buanderie|bureau|espace))',
            p.strip(),
            flags=re.IGNORECASE
        )
        for sp in sub_et:
            clean_p = sp.strip().rstrip('.')
            if clean_p:
                out.append(clean_p)
    return out


def split_compound_line(line: str, level_ctx: Optional[str] = None) -> List[str]:
    """
    Décompose une ligne ou phrase complexe multi-attributs en segments sémantiques atomiques :
    1. Séparateurs explicites (•, |, ·, ;, // hors URLs, + entre pièces).
    2. Phrases d'accroche narratives ('vous séduira par...', 'elle offre un cadre de vie...').
    3. Phrases condensées '<Bien> sur un <terrain> avec/comprenant <énumération>'.
    4. Phrases d'introduction combinées (Situation/Bien + Terrain/Piscine).
    5. Phrases d'équipements et confort (climatisation, alarme, solaire, photovoltaïque, volets).
    6. Pivots internes de composition ('... comprenant : ...', '... se compose de ...').
    7. Paragraphes narratifs de distribution et énumérations à virgules/connecteurs.
    """
    if not line:
        return []

    clean_line = strip_technical_noise(line).strip()
    if not clean_line:
        return []

    if RHETORICAL_QUESTION_REGEX.match(clean_line) or ORPHAN_COMPOSITION_HEADER_REGEX.match(clean_line):
        return []

    # 0. Si la chaîne contient plusieurs phrases séparées par ? ou ! (ex: question rhétorique suivie d'une accroche)
    if re.search(r'[!?]\s+[A-ZÀ-Ÿ0-9]', clean_line):
        sent_parts = [sp.strip() for sp in re.split(r'(?<=[!?])\s+(?=[A-ZÀ-Ÿ0-9])', clean_line) if sp.strip()]
        if len(sent_parts) > 1:
            expanded_sents: List[str] = []
            for sp in sent_parts:
                if RHETORICAL_QUESTION_REGEX.match(sp) or ORPHAN_COMPOSITION_HEADER_REGEX.match(sp):
                    continue
                expanded_sents.extend(split_compound_line(sp, level_ctx=level_ctx))
            return expanded_sents

    # 1. Séparateurs explicites (•, |, ·, ;, ou // non précédé de ':' pour protéger https://)
    if re.search(r'\s+[•|·]\s+|(?<!:)\s+//\s+|\s*;\s+(?=[A-Za-zÀ-ÿ0-9])|\s+/\s+(?=(?:\d+|un\b|une\b|cuisine\b|salon\b|s[ée]jour\b|chambre|bureau|buanderie|v[ée]randa|sanitaire|salle\b|wc\b))|\s*,?\s*\+\s+(?=(?:\d+|un\b|une\b)\s*(?:chambre|bureau|carport|garage|parking|terrasse|container|villa|studio|salle|wc|cellier))', clean_line, flags=re.IGNORECASE):
        parts = [
            p.strip()
            for p in re.split(
                r'\s+[•|·]\s+|(?<!:)\s+//\s+|\s*;\s+(?=[A-Za-zÀ-ÿ0-9])|\s+/\s+(?=(?:\d+|un\b|une\b|cuisine\b|salon\b|s[ée]jour\b|chambre|bureau|buanderie|v[ée]randa|sanitaire|salle\b|wc\b))|\s*,?\s*\+\s+(?=(?:\d+|un\b|une\b)\s*(?:chambre|bureau|carport|garage|parking|terrasse|container|villa|studio|salle|wc|cellier))',
                clean_line,
                flags=re.IGNORECASE
            )
            if p.strip()
        ]
        if len(parts) > 1:
            expanded_explicit: List[str] = []
            for p in parts:
                expanded_explicit.extend(split_compound_line(p, level_ctx=level_ctx))
            return expanded_explicit

    # 2. Synthèse des phrases d'accroche narratives ('Cette villa F4... vous séduira par...', 'Située à proximité..., elle offre un cadre de vie...')
    hook_synth = _synthesize_narrative_hook_sentence(clean_line)
    if hook_synth is not None:
        return [h for h in hook_synth if h]

    # 3. Cas des phrases condensées "<Bien> sur un <terrain> avec/comprenant : <énumération de pièces>"
    m_cond = re.match(
        r'^(?P<bien>(?:(?:tr[èe]s\s+)?(?:charmante?|belle|beau|magnifique|superbe|joli[e]?|agr[ée]able|vaste|grande?)\s+)?(?:maison|villa|appartement|studio|duplex|propri[ée]t[ée]|f\d)\b[^.;]*?)\s+sur\s+(?P<terrain>(?:un\s+|une\s+)?(?:tr[èe]s\s+)?(?:beau\s+|belle\s+|grand\s+|grande\s+|joli\s+)?(?:\d+(?:[.,]\d+)?\s*ares?\s+de\s+)?(?:terrain|parcelle)\b[^.;]*?)\s+(?:avec|comprenant|compos[ée]e?\s+de)\s*:?\s+(?P<enum>.+)$',
        clean_line,
        flags=re.IGNORECASE
    )
    if m_cond:
        bien_part = m_cond.group("bien").strip()
        terrain_part = re.sub(r'^(?:un|une)\s+', '', m_cond.group("terrain").strip(), flags=re.IGNORECASE)
        enum_parts = _split_comma_feature_clauses(m_cond.group("enum"))
        if len(enum_parts) >= 2:
            return [bien_part, terrain_part] + [synthesize_atomic_bullet(ep, level_ctx) for ep in enum_parts]

    # 4. Cas des phrases d'introduction combinées (Situation + Bien/Surface + sur un terrain... avec piscine)
    m_intro_land = INTRO_LAND_TAIL_REGEX.match(clean_line)
    if m_intro_land and not re.search(r'\b(?:comprenant|se\s+compose|compos[ée]e?\s+d)\b', clean_line, re.IGNORECASE):
        head_bullets = _synthesize_intro_head(m_intro_land.group("head"))
        tail_bullets = _split_land_and_exterior_tail(m_intro_land.group("land"))
        return [synthesize_atomic_bullet(b, level_ctx) for b in (head_bullets + tail_bullets) if b]

    # 4b. Cas d'une accroche de bien suivie de 'avec terrasse de XX m² / jardin / piscine'
    m_intro_ext = re.match(
        r'^(?P<bien>(?:d[ée]couvrez\s+)?(?:ce|cet|cette|un|une)?\s*(?:tr[èe]s\s+)?(?:bel|beau|belle|charmant[e]?|spacieux|spacieuse|magnifique|superbe|agr[ée]able)?\s*'
        r'(?:appartement|villa|maison|duplex|studio|bas\s+de\s+villa|haut\s+de\s+villa|propri[ée]t[ée])\b[^.;]{3,95}?)'
        r'\s+avec\s+(?P<ext>(?:une?\s+|bel\s+|beau\s+|grand[e]?\s+)?(?:terrasse|varangue|jardin(?:et)?|piscine|balcon|deck|vue\s+mer|espace\s+ext[ée]rieur)\b[^.;]*)$',
        clean_line,
        flags=re.IGNORECASE
    )
    if m_intro_ext and not re.search(r'\b(?:comprenant|se\s+compose|compos[ée]e?\s+d)\b', clean_line, re.IGNORECASE):
        bien_part = format_bullet_uniform(m_intro_ext.group("bien"))
        ext_parts = _split_comma_feature_clauses(m_intro_ext.group("ext"))
        out_ie = [bien_part] if bien_part else []
        for ep in ext_parts:
            for sc in _split_clause_connectors(ep):
                sb = synthesize_atomic_bullet(sc, None)
                if sb:
                    out_ie.append(sb)
        if len(out_ie) >= 2:
            return out_ie

    # 5. Cas des phrases d'équipements globaux (climatisation, alarme, solaire, photovoltaïque, volets)
    equip_bullets = _decompose_equipment_sentence(clean_line)
    if equip_bullets:
        return [synthesize_atomic_bullet(b, None) for b in equip_bullets if b]

    # 6. Cas des pivots internes de composition en milieu de phrase ("Appartement F2 situé à... comprenant : une cuisine...")
    if not LEADING_NARRATIVE_WRAPPER_REGEX.match(clean_line):
        m_int_piv = INTERNAL_COMPOSITION_PIVOT_REGEX.match(clean_line)
        if m_int_piv:
            intro_h = m_int_piv.group("intro_head").strip(' ,;:')
            comp_t = m_int_piv.group("comp_tail").strip()
            piv_out: List[str] = []
            if intro_h and len(intro_h) > 5:
                # Si l'intro contient elle-même situation + bien ou bien + jardin, la découper proprement
                for ih_sub in _synthesize_intro_head(intro_h):
                    ih_fmt = format_bullet_uniform(ih_sub)
                    if ih_fmt:
                        piv_out.append(ih_fmt)
            comma_clauses = _split_comma_feature_clauses(comp_t)
            for cc in comma_clauses:
                for sub_c in _split_clause_connectors(cc):
                    synth = synthesize_atomic_bullet(sub_c, level_ctx=level_ctx)
                    if synth:
                        piv_out.append(synth)
            if len(piv_out) >= 2:
                return piv_out

    # 7. Cas des phrases narratives de distribution (RDC / Étage / 'la maison offre...' / 'Elle se compose d'un...' / 'ainsi qu\'un...')
    stripped_narrative = LEADING_NARRATIVE_WRAPPER_REGEX.sub('', clean_line).strip()
    has_distribution_structure = (
        stripped_narrative != clean_line
        or bool(TRAILING_COMPLETION_REGEX.search(clean_line))
        or bool(re.search(r'\b(?:ainsi\s+qu|de\s+m[êe]me\s+qu|un\s+d[ée]gagement\s+distribuant|entr[ée]e\s+desservant|accompagn[ée]e?\s+de)\b', clean_line, flags=re.IGNORECASE))
    )

    if has_distribution_structure:
        comma_clauses = _split_comma_feature_clauses(stripped_narrative)
        atomic_out: List[str] = []
        for cc in comma_clauses:
            for sub_c in _split_clause_connectors(cc):
                synth = synthesize_atomic_bullet(sub_c, level_ctx=level_ctx)
                if synth:
                    atomic_out.append(synth)
        if len(atomic_out) >= 1:
            return atomic_out

    # 8. Cas d'une énumération de pièces/prestations séparées par des virgules ou connecteurs inter-pièces
    literary_verbs = re.search(
        r'\b(?:invite|existe|distingue|conjugue|rejoindre|s[\'\’]ouvrent|repr[ée]sentant|choisit)\b',
        clean_line,
        flags=re.IGNORECASE
    )
    has_multi_room_connector = bool(re.search(
        r',|\b(?:ainsi\s+qu|de\s+m[êe]me\s+qu|(?:ouvrant|donnant|ouvert[es]?)\s+sur|prolong[ée]e?s?\s+par|et\s+(?:d[\'’]\s*un|de\s+|un\b|une\b|deux\b|trois\b|\d+))',
        clean_line,
        flags=re.IGNORECASE
    ))
    if not literary_verbs and has_multi_room_connector:
        enum_candidates = _split_comma_feature_clauses(clean_line)
        expanded_candidates: List[str] = []
        for ec in enum_candidates:
            expanded_candidates.extend(_split_clause_connectors(ec))
        if len(expanded_candidates) >= 2:
            return [synthesize_atomic_bullet(c, level_ctx=level_ctx) for c in expanded_candidates if c.strip()]

    if level_ctx:
        return [synthesize_atomic_bullet(clean_line, level_ctx=level_ctx)]

    return [clean_line]


def is_valid_person_name(candidate: str) -> bool:
    """
    Valide rigoureusement qu'une chaîne candidate est un vrai prénom/nom de négociateur
    et non un pronom ('Nous', 'Au', 'Le'), un libellé ('Réf', 'Loyer Hc', 'Notre Commercial')
    ou un terme immobilier ('Vue Mer', 'Volets Roulants').
    """
    if not candidate:
        return False
    clean = re.sub(r'[^A-Za-zÀ-ÿ\s\-]', '', candidate).strip()
    if len(clean) < 2 or len(clean) > 32:
        return False
    low = clean.lower()
    if low in STOP_NAMES:
        return False
    words = [w for w in re.split(r'[\s\-]+', low) if w]
    if not words or len(words) > 3:
        return False
    if any(w in STOP_NAMES or w in INVALID_NAME_TOKENS or len(w) < 2 for w in words):
        return False
    return True


def is_likely_person_name(line: str) -> bool:
    """
    Détermine si une ligne isolée dans un footer est un nom de négociateur (ex: "Laurent NGUYEN").
    """
    clean = line.strip()
    if len(clean) > 35 or len(clean) < 3:
        return False
    if re.search(r'(?:contact|conseill|n[ée]gociat|agent)\s*:', clean, re.IGNORECASE):
        after_colon = clean.split(':', 1)[-1].strip()
        return is_valid_person_name(after_colon)
    words = clean.split()
    if 2 <= len(words) <= 3 and all(w[0].isupper() for w in words if w):
        return is_valid_person_name(clean)
    return False


def pre_segment_raw_lines(raw_text: str) -> List[str]:
    """
    Découpe et normalise le texte brut en lignes exploitables :
    - Supprime les balises [IMG: ...], les bannières immonc parasites et les balises HTML.
    - Sépare les listes à tirets collées sur une même ligne (':- ', ',- ').
    - Isole les annonces monolignes compactes et les appels au contact en fin de paragraphe.
    """
    if not raw_text:
        return []

    clean_text = strip_technical_noise(raw_text).replace('\x96', '–')
    clean_breaks = re.sub(r'</?(?:ul|ol|li|p|div|br|h[1-6]|tr)[^>]*>', '\n', clean_text, flags=re.IGNORECASE)
    clean_breaks = re.sub(r'</?[a-zA-Z][^>]*>', ' ', clean_breaks)
    # Découpage des listes à tirets collées sur une seule ligne (ex: ':- Une chambre... ,- Une chambre...')
    clean_breaks = re.sub(r'(?:[:;,]|(?<=[a-zà-ÿ0-9\)]))-\s+(?=[A-ZÀ-Ÿ0-9])', '\n- ', clean_breaks)
    lines = [l.strip() for l in clean_breaks.split('\n') if l.strip()]

    expanded = []
    for l in lines:
        m = re.search(r'^(.*?)\.\s+(Contact\s*:.*)$', l, re.IGNORECASE)
        if m and len(m.group(1).strip()) > 5:
            expanded.append(m.group(1).strip() + '.')
            expanded.append(m.group(2).strip())
            continue

        m_dash = re.search(r'^(.*?)\s+[-–—]\s+(\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b.*)$', l, re.IGNORECASE)
        if m_dash and len(m_dash.group(1).strip()) > 15:
            expanded.append(m_dash.group(1).strip())
            expanded.append(m_dash.group(2).strip())
            continue

        if len(l) > 35:
            m_contact = re.search(
                r'^(.*?)(?:\.|\s|!)\s*(\b(?:contactez[- ]nous|contactez|contacter|pour\s+(?:tout(?:e?s?)?\s+visite|tout(?:e?s?)?\s+renseignement|les\s+visites|organiser\s+une\s+visite|en\s+savoir\s+plus)|visites?\s+(?:et\s+renseignements?\s+)?avec)\b.*)$',
                l,
                re.IGNORECASE
            )
            if m_contact and len(m_contact.group(1).strip()) > 15:
                body_part = m_contact.group(1).strip()
                contact_part = m_contact.group(2).strip()
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', body_part) if s.strip()]
                if len(sentences) <= 1 and len(body_part) > 150:
                    clauses = [c.strip() for c in re.split(r'(?=\b(?:1|2|3|4)\s+(?:habitation|bungalow|grand|appartement|maison|villa|studio|f\d)\b|\b(?:nombreux|très\s+gros|situé\s+[aà])\b)', body_part, flags=re.IGNORECASE) if c.strip()]
                    expanded.extend(clauses)
                else:
                    expanded.extend(sentences)
                expanded.append(contact_part)
                continue
            elif len(lines) == 1:
                sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', l) if s.strip()]
                expanded.extend(sentences)
                continue

        expanded.append(l)

    return expanded


def split_body_and_footer(lines: List[str]) -> Tuple[List[str], List[str]]:
    """
    Sépare étanchément le corps descriptif du bien du bloc commercial de signature (Footer Boundary).
    """
    if not lines:
        return [], []

    footer_start_idx = None
    for i, line in enumerate(lines):
        clean = line.strip()
        if FOOTER_TRIGGER_REGEX.search(clean) and (len(clean) < 130 or FOOTER_TRIGGER_REGEX.match(clean)):
            footer_start_idx = i
            break
        if i >= len(lines) - 8 or i >= len(lines) // 2:
            if EMAIL_REGEX.search(clean) or (('tél' in clean.lower() or 'contact' in clean.lower()) and PHONE_NC_REGEX.search(clean)):
                if i > 0 and is_likely_person_name(lines[i - 1]):
                    footer_start_idx = i - 1
                else:
                    footer_start_idx = i
                break

    if footer_start_idx is not None:
        return lines[:footer_start_idx], lines[footer_start_idx:]

    return lines, []


def _clean_candidate_person_name(raw_name: str) -> str:
    """Nettoie les mots outils ou pronoms collés autour d'un prénom ('Edvina nous' -> 'Edvina')."""
    if not raw_name:
        return ""
    s = re.sub(
        r'^(?:contactez|contacter|contact|appelez|appeler|joindre|avec|aupr[èe]s\s+de|notre\s+commercial[e]?|et\s+visites?|visites?|renseignements?|par\s+t[ée]l(?:[ée]phone)?|direct(?:ement)?|ez|er|au|le|la|les|de|du|un|une)\b[\s:\-]*',
        '',
        raw_name.strip(),
        flags=re.IGNORECASE
    ).strip()
    s = re.sub(
        r'[\s\-]+(?:nous|moi|vite|direct(?:ement)?|au|le|ai|pour|par\s+t[ée]l)\b.*$',
        '',
        s,
        flags=re.IGNORECASE
    ).strip()
    return s


def extract_direct_contacts_and_agency_metadata(
    raw_text: str,
    footer_lines: Optional[List[str]] = None
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Extrait universellement les contacts négociateurs et les métadonnées de l'agence
    avec filtrage strict anti-faux-positifs ('Réf', 'Nous', 'Loyer Hc', etc.).
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

    clean_raw_text = strip_technical_noise(raw_text)
    seen_phones = set()

    footer_str = "\n".join(footer_lines) if footer_lines else ""
    search_scope = footer_str if footer_str else clean_raw_text

    all_emails = EMAIL_REGEX.findall(clean_raw_text)
    if all_emails:
        agency_metadata["email"] = all_emails[0]

    if footer_lines:
        for fl in footer_lines:
            fl_clean = fl.strip()
            if any(d in fl_clean.lower() for d in ['lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'horaires']) and ('h' in fl_clean.lower() or ':' in fl_clean):
                if fl_clean not in agency_metadata["hours"]:
                    agency_metadata["hours"].append(fl_clean)
            if any(k in fl_clean.lower() for k in ['cp n', 'carte pro', 'garantie bancaire', 'ridet', 'rcs']):
                if fl_clean not in agency_metadata["legal"]:
                    agency_metadata["legal"].append(fl_clean)

    negotiator_from_footer = None
    if footer_lines:
        for fl in footer_lines:
            fl_clean = fl.strip()
            if FOOTER_TRIGGER_REGEX.search(fl_clean) or EMAIL_REGEX.search(fl_clean):
                continue
            if any(w in fl_clean.lower() for w in ['horaires', 'lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'garantie', 'cp n', 'bci', 'bnc', 'prix', 'loyer']):
                continue
            m_nom = re.search(r'\b(?:contact\s*:?|n[ée]gociat(?:eur|rice)\s*:?|conseill[eè]re?\s*:?|avec)\s+([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)', fl_clean, re.IGNORECASE)
            if m_nom:
                cand = _clean_candidate_person_name(m_nom.group(1))
                if is_valid_person_name(cand):
                    negotiator_from_footer = cand.title()
                    break
            if re.match(r'^[A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ0-9\-]+)+$', fl_clean) and len(fl_clean) < 35:
                cand = _clean_candidate_person_name(fl_clean)
                if is_valid_person_name(cand):
                    negotiator_from_footer = cand
                    break

    # 2. Motifs syntaxiques intra-texte de contacts directs (avec garde-fou anti-montants F/XPF/CFP)
    PAT_PHONE = r'(?:(?:\+687|00687)\s*)?(?:0\s*)?([02-9]\d{2}[\s.-]\d{3}|[02-9]\d(?:[\s.-]?\d{2}){2}|\b[2-9]\d{5}\b)(?!\s*(?:F\b|XPF\b|CFP\b|XFP\b|€|/\s*mois|HC\b|CC\b|\d))'
    patterns = [
        re.compile(rf'\b(?:visites?|visiter)?\s*(?:et\s*n[ée]gociations?)?\s*(?:avec|contactez|contacter|joindre|appelez|appeler|demander)\s+([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s+(?:au|t[ée]l\s*:?|mobile\s*:?|le)\s*{PAT_PHONE}', re.IGNORECASE),
        re.compile(rf'\b([A-ZÀ-Ÿ][a-zà-ÿ\-]+)\s+au\s+{PAT_PHONE}'),
        re.compile(rf'\b(?:contact|conseill[eè]re?|n[ée]gociat(?:eur|rice)|r[ée]f[ée]rent|agent)\s*(?:et\s+visites?\s*)?:?\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s*(?:au|:|\(|\-|[–—\x96])?\s*{PAT_PHONE}', re.IGNORECASE),
        re.compile(rf'\bContact\s*:\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s*\(\s*T[ée]l\s*:\s*{PAT_PHONE}', re.IGNORECASE),
        re.compile(rf'(?:^|\n)\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+(?:\s+[A-ZÀ-Ÿ][A-Za-zÀ-Ÿ\-]+)?)\s*(?::|[–—\x96])\s*{PAT_PHONE}'),
        re.compile(rf'{PAT_PHONE}\s*\(\s*([A-ZÀ-Ÿ][a-zà-ÿ\-]+)\s*\)', re.IGNORECASE),
    ]

    for pat in patterns:
        for m in pat.finditer(clean_raw_text):
            g1, g2 = m.group(1).strip(), m.group(2).strip()
            if re.match(r'^[02-9]\d', g1):
                phone_raw, name_raw = g1, g2
            else:
                name_raw, phone_raw = g1, g2

            clean_name = _clean_candidate_person_name(name_raw)
            if not is_valid_person_name(clean_name):
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

    footer_phones = []
    for m in PHONE_NC_REGEX.finditer(search_scope):
        p_str = m.group(1) or m.group(2) or m.group(3)
        after_ctx = search_scope[m.end():m.end() + 12]
        if re.match(r'^\s*(?:F\b|XPF\b|CFP\b|€|/\s*mois|HC\b|CC\b)', after_ctx, re.IGNORECASE):
            continue
        p_info = normalize_nc_phone(p_str)
        if p_info and p_info["digits"] not in [p["digits"] for p in footer_phones]:
            match_start = m.start()
            prefix_ctx = search_scope[max(0, match_start - 18):match_start].lower()
            if any(k in prefix_ctx for k in ['cp n', 'cp ', 'carte pro', 'ridet', 'rcs', 'loyer', 'prix', 'charges']):
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
    """Détecte si une ligne est un slogan publicitaire, une question rhétorique, une amorce orpheline ou un contact déjà routé."""
    if not line:
        return True

    clean = line.strip()
    clean_no_emoji = re.sub(r'[^\w\s]', ' ', clean).strip().lower()
    clean_no_emoji = re.sub(r'\s+', ' ', clean_no_emoji)

    slogans = {
        'a saisir', 'a voir absolument', 'coup de coeur', 'coup de coeur assure',
        'exclusivite', 'exclusivité', 'urgent', 'nouveaute', 'a visiter rapidement',
        'a ne pas manquer', 'rare sur le secteur', 'opportunite a saisir',
        'il comprend', 'elle comprend', 'il se compose', 'elle se compose',
        'elle se compose de', 'il se compose de', 'la villa comprend', 'la maison comprend',
        'l appartement comprend', 'le logement comprend', 'la partie nuit comprend',
        'description', 'composition', 'a visiter absolument',
        'a vendre', 'à vendre', 'a louer', 'à louer', 'prix de vente', 'descriptif du bien',
        'vendu', 'sous compromis', 'sous offre'
    }
    if clean_no_emoji in slogans:
        return True

    # Questions rhétoriques courtes sans montant ni surface (ex: "Vous cherchez de l'espace... ?")
    if RHETORICAL_QUESTION_REGEX.match(clean):
        return True

    for pat in BOILERPLATE_PATTERNS:
        if pat.match(clean) or pat.match(clean_no_emoji):
            return True

    for contact in direct_contacts:
        if contact["raw_phone"] in clean or contact["name"].lower() in clean.lower():
            if re.search(r'(?:visite|visiter|négociation|contact|joindre|appeler|tél|tel|mobile|:|[–—])', clean, re.IGNORECASE):
                return True

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
    Classe une phrase ou puce atomique de façon 100 % déterministe et invariante dans l'une des 8 rubriques canoniques :
    0. conditions_modalites
    1. espace_nuit
    2. stationnement_annexes
    3. pieces_de_vie
    4. services_residence
    5. prestations_complementaires
    6. cadre_vie (présentation globale du bien & localisation — priorisé avant exterieurs)
    7. exterieurs
    """
    norm = text.lower().replace("’", "'")

    # Priorité 0 : Conditions & Modalités financières / bail / disponibilité explicites
    if any(contains_keyword(norm, k) for k in [
        "loyer :", "loyer:", "loyer mensuel", "loyer charges comprises", "loyer hors charges",
        "dépôt de garantie", "depot de garantie", "caution", "charges comprises", "hors charges",
        "bail", "location possible", "charges :", "charges de copropriété", "charges de copro",
        "charges mensuelles", "provision pour", "colocation accept", "colocation autoris",
        "colocation possible", "disponible immédiatement", "disponible de suite", "disponible tout de suite",
        "disponible à partir", "disponible le ", "libre de suite", "libre immédiatement",
        "frais d'agence", "honoraires"
    ]):
        return "conditions_modalites"
    if re.search(r'\b(?:prix(?:\s+de\s+vente)?|seulement|loyer|charges?)\s*(?:\([^)]*\))?\s*(?::|de|d\'environ)?\s*\d[\d\s.]*\s*(?:f\s*cfp|xpf|frs?|f\b|mf|millions?)\b', norm):
        return "conditions_modalites"
    if re.search(r'^\s*\d+(?:[.,]\d+)?\s*(?:mf|millions?\s*f?)\s*$', norm):
        return "conditions_modalites"

    # Priorité 1 : Espace Nuit & Sanitaires (ou Bureaux & Espaces pros pour bien commercial)
    if any(contains_keyword(norm, k) for k in [
        "chambre", "chambres", "dressing", "parentale", "salle d'eau", "salles d'eau",
        "salle de bain", "salle de bains", "salles de bain", "salles de bains",
        "sde", "sdb", "douche", "baignoire", "wc", "toilette", "toilettes", "sanitaire", "sanitaires",
        "bureau", "bureaux", "open space", "open-space", "plateau de bureaux",
        "salle de réunion", "surface principale", "pièce principale", "surface utile"
    ]):
        if not any(w in norm for w in ["local commercial", "à vendre", "a vendre", "activité tertiaire"]):
            return "espace_nuit"

    # Priorité 2 : Stationnement, Dock, Rangement & Annexes
    if any(contains_keyword(norm, k) for k in [
        "parking", "parkings", "garage", "garages", "carport", "box", "cellier",
        "abri voiture", "abri pour véhicule", "abri deux roues", "deux roues", "deux-roues", "2 roues",
        "garer une voiture", "garer un véhicule", "place de parking", "places de parking",
        "place couverte", "places couvertes", "place sécurisée", "places sécurisées",
        "stationnement", "stationnements", "dock", "réserve", "reserve", "entrepôt",
        "espace de rangement", "débarras", "debarras", "container"
    ]):
        if "cellier intérieur" not in norm and "loyer" not in norm and "route en terre" not in norm:
            return "stationnement_annexes"

    # Priorité 3 : Pièces de vie (Séjour & Cuisine)
    if any(contains_keyword(norm, k) for k in [
        "cuisine", "séjour", "sejour", "salon", "salle à manger", "buanderie",
        "baie vitrée", "baies vitrées", "îlot", "ilot", "cafétéria", "cafeteria", "pièce de vie", "espace de vie"
    ]):
        return "pieces_de_vie"

    # Priorité 4 : Services & Équipements de la résidence / copropriété
    if any(contains_keyword(norm, k) for k in [
        "fitness", "salle de fitness", "salle de sport", "gym", "restaurant", "restaurants",
        "coiffeur", "coiffeurs", "boutique", "boutiques", "conciergerie", "concierge",
        "gardien", "gardiennage", "vidéosurveillance", "videosurveillance", "ascenseur",
        "court de tennis", "sauna", "rooftop", "piscine commune", "piscine de la résidence",
        "digicode", "interphone"
    ]):
        return "services_residence"

    # Priorité 5 : Équipements & Prestations complémentaires (alarme, solaire, photovoltaïque, volets, climatisation globale)
    if any(contains_keyword(norm, term) for term in [
        "chauffe-eau", "chauffe eau", "solaire", "panneaux", "photovoltaïque", "photovoltaique",
        "photovoltaïques", "photovoltaiques", "volet", "volets", "persienne", "persiennes",
        "fibre", "alarme", "carrelage", "moustiquaire", "moustiquaires", "fosse septique",
        "groupe électrogène", "citerne", "surpresseur", "portail électrique", "portail motorisé",
        "visiophone", "onduleur", "double vitrage", "climatisation", "pompe à chaleur"
    ]) or re.search(r'\b(?:enti[èe]rement|totalement|compl[èe]tement)\s+climatis[ée]e?\b', norm):
        return "prestations_complementaires"

    # Priorité 6 : Cadre de vie & Présentation générale du bien (exécuté AVANT exterieurs pour ne pas détourner les intros)
    is_pure_exterior = bool(re.match(
        r'^\s*(?:\d+\s+)?(?:tr[èe]s\s+)?(?:bel(?:le)?|beau|grand[e]?|vaste|joli[e]?|agr[ée]able|petit[e]?)?\s*'
        r'(?:terrasse|varangue|jardin(?:et)?|balcon|deck|piscine|cour|v[ée]randa|loggia|patio|pergola|carbet|far[ée]|'
        r'vue\s+(?:mer|lagon|d[ée]gag[ée]e|montagne|panoramique)|espace\s+ext[ée]rieur|entretien\s+jardin|terrain|parcelle)\b',
        norm
    ))
    is_property_intro_or_loc = bool(re.search(
        r'\b(?:appartement\s+(?:de\s+type\s+)?[ft]\d|villa\s+(?:de\s+type\s+)?[ft]\d|maison\s+(?:de\s+type\s+)?[ft]\d|'
        r'^[ft]\d\s+duplex|bas\s+de\s+villa|haut\s+de\s+villa|rez[\s-]de[\s-]jardin\s+de\s+\d+|'
        r'dans\s+une\s+r[ée]sidence|au\s+c[oœ]ur\s+d|situ[ée]e?\s+(?:dans|sur|[àa])|quartier\s+r[ée]sidentiel|'
        r'environnement\s+calme|secteur\s+de|[àa]\s+proximit[ée]|proche\s+de\s+toutes|situation\s+id[ée]ale|'
        r'bel\s+appartement|belle\s+villa|joli\s+studio|emplacement\s+(?:central|strat[ée]gique)|'
        r'la\s+mer\s+pour\s+horizon|art\s+de\s+vivre|habitable[s]?|village\s+de|en\s+copropri[ée]t[ée]|en\s+impasse|'
        r'r[ée]sidence\s+s[ée]curis[ée]e|petite\s+copropri[ée]t[ée]|rue\s+calme|au\s+calme|id[ée]al[e]?\s+pour\s+(?:la\s+famille|une\s+famille|un\s+couple|1er\s+achat))\b',
        norm
    ))
    if is_property_intro_or_loc and not is_pure_exterior:
        return "cadre_vie"

    # Priorité 7 : Extérieurs, Terrasse & Vue
    if any(contains_keyword(norm, k) for k in [
        "terrasse", "varangue", "jardin", "jardinet", "balcon", "deck", "piscine", "cour", "véranda",
        "vue mer", "vue lagon", "vue dégagée", "vue montagne", "extérieur", "extérieurs", "espace extérieur",
        "terrain", "parcelle", "ares", "carbet", "faré", "fare", "pergola", "barbecue"
    ]):
        return "exterieurs"

    scores: Dict[str, int] = {}
    for cat in CATEGORY_DEFINITIONS:
        score = sum(1 for kw in cat["keywords"] if contains_keyword(norm, kw))
        if score > 0:
            scores[cat["key"]] = score

    if scores:
        return max(scores, key=scores.get)

    return "cadre_vie"


LEVEL_TAG_SUFFIX_RE = re.compile(
    r'\s*\((?:rdc|au\s+rez[- ]de[- ]chauss[ée]e|[àa]\s+l[\'’][ée]tage|en\s+rez[- ]de[- ]jardin|en\s+mezzanine|en\s+sous[- ]sol|au\s+niveau\s+inf[ée]rieur)\)\s*$',
    re.IGNORECASE
)

SECONDARY_DISCRIMINATOR_RE = re.compile(
    r'\b(?:second[e]?|deuxi[èe]me|2[èe]me|3[èe]me|4[èe]me|autre|ind[ée]pendant[e]?|suppl[ée]mentaire|privati(?:f|ve)|parentale|invit[ée]s?)\b',
    re.IGNORECASE
)


def _normalize_dedup_core(text: str) -> str:
    s = LEVEL_TAG_SUFFIX_RE.sub('', text.lower()).strip()
    s = re.sub(r'^(?:un|une|le|la|les|l[\'’]|d[\'’]|de|du|des)\s+', '', s)
    s = re.sub(r'[^\w\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def sanitize_section_bullets(cat_key: str, items: List[str]) -> List[str]:
    """
    Applique l'hygiène stricte des tags de niveau et la déduplication intelligente
    à l'intérieur d'une rubrique canonique :
    - Retire les tags '(À l'étage)'/'(RDC)'/'(En rez-de-jardin)' des rubriques où ils n'ont aucun sens.
    - Élimine les doublons exacts et les libellés génériques strictement inclus dans une puce plus riche
      (ex: 'Séjour' supprimé si 'Grand séjour' est présent), tout en conservant les pièces distinctes
      ('Seconde salle d'eau', 'Chambre indépendante...').
    """
    cleaned: List[str] = []
    for raw in items:
        s = raw.strip()
        if not s:
            continue
        # Hygiène des tags de niveau par rubrique
        if cat_key in ("cadre_vie", "services_residence", "prestations_complementaires", "conditions_modalites"):
            s = LEVEL_TAG_SUFFIX_RE.sub('', s).strip()
        elif cat_key == "exterieurs":
            s = re.sub(r'\s*\(en\s+rez[- ]de[- ]jardin\)\s*$', '', s, flags=re.I).strip()
            if not any(w in s.lower() for w in ("balcon", "terrasse", "varangue", "loggia", "deck")):
                s = LEVEL_TAG_SUFFIX_RE.sub('', s).strip()
        elif cat_key == "stationnement_annexes":
            s = re.sub(r'\s*\(en\s+rez[- ]de[- ]jardin\)\s*$', '', s, flags=re.I).strip()
            if not any(w in s.lower() for w in ("garage", "cellier", "buanderie", "parking", "box", "remise")):
                s = LEVEL_TAG_SUFFIX_RE.sub('', s).strip()

        if not s or len(s) < 2:
            continue
        cleaned.append(s)

    # Déduplication intelligente en préservant l'ordre
    final_items: List[str] = []
    for cand in cleaned:
        cand_core = _normalize_dedup_core(cand)
        cand_low = cand.lower()
        cand_lvl = LEVEL_TAG_SUFFIX_RE.search(cand_low)
        cand_lvl_str = cand_lvl.group(0).strip() if cand_lvl else ""

        skip = False
        replace_idx = None
        for idx, existing in enumerate(final_items):
            ex_core = _normalize_dedup_core(existing)
            ex_low = existing.lower()
            ex_lvl = LEVEL_TAG_SUFFIX_RE.search(ex_low)
            ex_lvl_str = ex_lvl.group(0).strip() if ex_lvl else ""

            # Exact match (sans casse)
            if cand_low == ex_low or (cand_core == ex_core and cand_lvl_str == ex_lvl_str):
                skip = True
                break

            # Même pièce à deux niveaux différents (ex: WC (RDC) vs WC (À l'étage)) -> conserver les deux !
            if cand_lvl_str and ex_lvl_str and cand_lvl_str != ex_lvl_str:
                continue

            # Si l'un des deux est explicitement une seconde/autre pièce distincte -> conserver les deux !
            if SECONDARY_DISCRIMINATOR_RE.search(cand) != SECONDARY_DISCRIMINATOR_RE.search(existing):
                # Exception : si l'existant est un stub ultra-générique de 1-2 mots ('Salle d'eau', 'Chambre') sans détail et cand est 'Suite parentale' -> garder les deux si le texte en parle séparément
                continue

            # Stub générique court inclus dans une puce plus riche (ex: 'Séjour' dans 'Grand séjour', 'Cuisine' dans 'Cuisine équipée')
            if len(cand_core) >= 4 and len(ex_core) > len(cand_core) + 2 and re.search(rf'\b{re.escape(cand_core)}\b', ex_core):
                if len(cand.split()) <= 3:
                    skip = True
                    break
            if len(ex_core) >= 4 and len(cand_core) > len(ex_core) + 2 and re.search(rf'\b{re.escape(ex_core)}\b', cand_core):
                if len(existing.split()) <= 3 and len(cand) <= 130:
                    replace_idx = idx
                    break

        if skip:
            continue
        if replace_idx is not None:
            final_items[replace_idx] = cand
        else:
            final_items.append(cand)

    return final_items


def extract_technical_specs(raw_description: str, raw_specs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Extrait et ancre 100 % des spécifications techniques chiffrées depuis la description brute
    (avec validation optionnelle des champs retournés par l'IA pour zéro hallucination).
    """
    if not isinstance(raw_specs, dict):
        raw_specs = {}

    desc_clean = strip_technical_noise(raw_description or "")
    desc_lower = desc_clean.lower().replace("’", "'")
    desc_digits = re.sub(r"[^\d]", "", desc_clean)

    # 1. Charges de copropriété / locatives (XPF) — garde-fou anti-.00 et anti-'loyer hors charges'
    charges_raw = raw_specs.get("charges_mensuelles_xpf")
    charges_val: Optional[int] = None
    has_valid_charge_context = bool(
        re.search(r"(?<!hors\s)(?<!sans\s)(?<!toutes\s)(?<!aucune\s)\bcharges?\b(?!\s*comprises|\s*incluses)", desc_lower)
    )
    if charges_raw is not None and has_valid_charge_context:
        try:
            raw_chg_str = re.sub(r"[.,]\d{1,2}\s*$", "", str(charges_raw).strip())
            c_digits = re.sub(r"[^\d]", "", raw_chg_str)
            if c_digits:
                val = int(c_digits)
                if 500 <= val <= 150_000 and c_digits in desc_digits:
                    charges_val = val
        except Exception:
            charges_val = None

    if charges_val is None and has_valid_charge_context:
        charge_patterns = [
            r"(?<!hors\s)(?<!sans\s)\bcharges?\s*(?:mensuelles?|de\s+copropri[eé]t[eé]|copro|locatives?)?\s*(?:\([^)]{0,40}\))?\s*[:=]?\s*(?:seulement\s+|environ\s+|de\s+|d['’]environ\s+)?(\d{1,3}(?:[\s.]\d{3})+|\d{3,6})(?:[.,]\d{1,2})?\s*(?:f\s*cfp|xpf|frs?|cfp|f\b|/mois)",
            r"(\d{1,3}(?:[\s.]\d{3})+|\d{3,6})(?:[.,]\d{1,2})?\s*(?:f\s*cfp|xpf|frs?|f)\s*(?:/\s*mois\s*)?de\s+charges?\b",
            r"(?<!hors\s)(?<!sans\s)\bprovision\s+(?:pour\s+|sur\s+)?charges?\s*[:=]?\s*(?:de\s+)?(\d{1,3}(?:[\s.]\d{3})+|\d{3,6})\s*(?:f\s*cfp|xpf|frs?|f\b)",
        ]
        for pat in charge_patterns:
            m_chg = re.search(pat, desc_clean, re.I)
            if m_chg:
                c_digs = re.sub(r"[^\d]", "", m_chg.group(1))
                if c_digs and 500 <= int(c_digs) <= 150_000:
                    charges_val = int(c_digs)
                    break

    # 2. Étage
    etage_raw = raw_specs.get("etage")
    etage_val: Optional[str] = None
    if etage_raw and isinstance(etage_raw, str):
        etage_clean = etage_raw.strip()
        if any(k in etage_clean.lower() for k in ("étage", "etage", "rdc", "rez", "dernier", "duplex")):
            if any(k in desc_lower for k in ("étage", "etage", "rdc", "rez", "dernier", "duplex")):
                etage_val = etage_clean
    if not etage_val:
        m_et = re.search(
            r"\b(dernier\s+[eé]tage|\d+(?:[eè]me|er|e)\s+[eé]tage|rez[- ]de[- ]jardin|rez[- ]de[- ]chauss[eé]e|rdc)\b",
            desc_clean,
            re.I
        )
        if m_et:
            raw_et = m_et.group(1).strip().lower()
            if raw_et == "rdc":
                etage_val = "Rez-de-chaussée"
            elif "rez" in raw_et and "jardin" in raw_et:
                etage_val = "Rez-de-jardin"
            elif "rez" in raw_et:
                etage_val = "Rez-de-chaussée"
            else:
                etage_val = raw_et[0].upper() + raw_et[1:]

    # 3. Ascenseur
    has_elevator: Optional[bool] = None
    elev_raw = raw_specs.get("has_elevator")
    if elev_raw is True and "ascenseur" in desc_lower:
        has_elevator = True
    elif elev_raw is False and "sans ascenseur" in desc_lower:
        has_elevator = False
    elif "ascenseur" in desc_lower and "sans ascenseur" not in desc_lower:
        has_elevator = True
    elif "sans ascenseur" in desc_lower:
        has_elevator = False

    # 4. Stationnement / Parking
    parking_raw = raw_specs.get("parking_detail")
    parking_val: Optional[str] = None
    if parking_raw and isinstance(parking_raw, str):
        p_clean = parking_raw.strip()
        if any(k in p_clean.lower() for k in ("parking", "stationnement", "garage", "box", "carport", "place", "abri")):
            if any(k in desc_lower for k in ("parking", "stationnement", "garage", "box", "carport", "place", "abri", "garer")):
                parking_val = p_clean
    if not parking_val:
        m_park_rich = re.search(
            r"\b(\d+|deux|trois|quatre|cinq)\s*(?:places?\s*(?:de\s*)?(?:parking|stationnement)s?(?:\s+(?:couvertes?|s[ée]curis[ée]es?|privatives?|ext[ée]rieures?)){0,2}|garages?(?:\s+ferm[ée]s?)?|carports?)\b",
            desc_lower
        )
        if m_park_rich:
            p_str = m_park_rich.group(0).strip()
            for pat_n, rep_n in NUMBER_WORDS_MAP:
                p_str = pat_n.sub(rep_n, p_str)
            parking_val = p_str[0].upper() + p_str[1:]
        elif "carport" in desc_lower:
            parking_val = "Carport"
        elif "garage" in desc_lower or re.search(r"\bbox\b", desc_lower):
            parking_val = "Garage fermé" if "ferm" in desc_lower else "Garage"
        elif re.search(r"place\s+de\s+parking\s+(?:couverte|s[ée]curis[ée]e)|parking\s+(?:couvert|s[ée]curis[ée])", desc_lower):
            is_covered = bool(re.search(r"(?<!non\s)(?<!non-)\bcouvert[es]?\b", desc_lower))
            parking_val = "Place couverte sécurisée" if is_covered else "Place de parking sécurisée"
        elif "parking" in desc_lower or "stationnement" in desc_lower:
            parking_val = "Parking"
        elif "garer une voiture" in desc_lower or "abri voiture" in desc_lower:
            parking_val = "Stationnement privatif"

    # 5. Surface terrasse / varangue / deck / balcon (m²)
    ter_raw = raw_specs.get("surface_terrasse_m2")
    ter_val: Optional[float] = None
    if ter_raw is not None:
        try:
            val_t = float(str(ter_raw).replace(",", "."))
            int_str = str(int(val_t))
            if 1.0 <= val_t <= 500.0 and int_str in desc_digits and any(k in desc_lower for k in ("terrasse", "varangue", "deck", "balcon")):
                ter_val = val_t
        except Exception:
            ter_val = None
    if ter_val is None:
        m_ter = re.search(
            r"\b(?:terrasse|varangue|deck|balcon)\s*(?:couverte|privative|am[ée]nag[ée]e|spacieux|spacieuse|d'angle)?\s*(?:de|d['’]|d['’]environ|:)?\s*(?:environ\s*)?(\d+(?:[.,]\d+)?)\s*(?:m²|m2)\b",
            desc_clean,
            re.I
        )
        if m_ter:
            try:
                v_t = float(m_ter.group(1).replace(",", "."))
                if 1.0 <= v_t <= 500.0:
                    ter_val = v_t
            except ValueError:
                pass

    # 6. Surface habitable (m²) et Surface terrain (m²)
    surf_hab_val: Optional[float] = None
    m_hab = re.search(
        r"(?:surface|superficie)\s+habitable\s*(?:de|d['’]|d['’]environ|:)?\s*(?:environ\s*)?(\d+(?:[.,]\d+)?)\s*(?:m²|m2)|"
        r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2)\s*habitables?\b",
        desc_clean,
        re.I
    )
    if m_hab:
        try:
            v_h = float((m_hab.group(1) or m_hab.group(2)).replace(",", "."))
            if 10.0 <= v_h <= 2000.0:
                surf_hab_val = v_h
        except ValueError:
            pass

    surf_ter_val: Optional[float] = None
    m_land_ares = re.search(
        r"(?:terrain|parcelle)\s*(?:clos|cl[ôo]tur[ée]|arbor[ée]|plat|paysager)?\s*(?:de|d['’]|d['’]environ)?\s*(?:environ\s*)?(\d+(?:[.,]\d+)?)\s*ares?\b",
        desc_clean,
        re.I
    )
    if m_land_ares:
        try:
            surf_ter_val = round(float(m_land_ares.group(1).replace(",", ".")) * 100.0, 1)
        except ValueError:
            pass
    else:
        m_land_m2 = re.search(
            r"(?:terrain|parcelle)\s*(?:clos|cl[ôo]tur[ée]|arbor[ée]|plat)?\s*(?:de|d['’]|d['’]environ)?\s*(?:environ\s*)?(\d+(?:[.,]\d+)?)\s*(?:m²|m2)\b",
            desc_clean,
            re.I
        )
        if m_land_m2:
            try:
                v_l = float(m_land_m2.group(1).replace(",", "."))
                if 50.0 <= v_l <= 500_000.0:
                    surf_ter_val = v_l
            except ValueError:
                pass

    # 7. Standing & Équipements booléens
    std_raw = raw_specs.get("standing")
    std_val: Optional[str] = None
    if std_raw and isinstance(std_raw, str):
        if any(k in std_raw.lower() for k in ("standing", "prestige", "luxe", "récent")):
            if any(k in desc_lower for k in ("standing", "prestige", "luxe", "récent")):
                std_val = std_raw.strip()
    if not std_val:
        m_std = re.search(r"\b(bon\s+standing|haut\s+standing|grand\s+standing|prestige)\b", desc_clean, re.I)
        if m_std:
            std_val = m_std.group(1).capitalize()

    has_ac = bool(re.search(r"\b(?:climatis[ée]e?s?|climatisation|clim\b|splits?)\b", desc_lower))
    has_pool = bool(re.search(r"\bpiscine\b", desc_lower) and not re.search(r"sans\s+piscine", desc_lower))
    has_sea_view = bool(re.search(r"\bvue\s+(?:sur\s+la\s+)?(?:mer|lagon|baie|oc[ée]an)\b", desc_lower))

    return {
        "charges_mensuelles_xpf": charges_val,
        "etage": etage_val,
        "has_elevator": has_elevator,
        "parking_detail": parking_val,
        "surface_terrasse_m2": ter_val,
        "surface_habitable_m2": surf_hab_val,
        "surface_terrain_m2": surf_ter_val,
        "has_air_conditioning": has_ac,
        "has_pool": has_pool,
        "has_sea_view": has_sea_view,
        "standing": std_val
    }


def structure_description(raw_description: Optional[str], property_type: Optional[str] = None) -> Dict[str, Any]:
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

    raw_text = strip_technical_noise(raw_description).strip()

    # 1. Découpage et pré-segmentation des lignes du texte
    raw_lines = pre_segment_raw_lines(raw_text)

    # 2. Segmentation Footer Boundary (isolation étanche de la signature)
    body_lines, footer_lines = split_body_and_footer(raw_lines)

    # 3. Extraction des contacts directs et métadonnées d'agence
    direct_contacts, agency_metadata = extract_direct_contacts_and_agency_metadata(raw_text, footer_lines)

    # 4. Décomposition des paragraphes en phrases avec propagation du contexte de niveau (RDC / À l'étage)
    categorized_items: Dict[str, List[str]] = {cat["key"]: [] for cat in CATEGORY_DEFINITIONS}
    cleaned_lines_all: List[str] = []
    current_level: Optional[str] = None

    for para_line in body_lines:
        clean_para = clean_line_typography(para_line)
        if not clean_para or len(clean_para) < 3:
            continue

        clean_para = normalize_header_banner(clean_para)
        if not clean_para or len(clean_para) < 3:
            continue

        if is_contact_or_boilerplate_line(clean_para, direct_contacts):
            continue

        # Détection d'un marqueur de niveau en tête de paragraphe
        para_body, current_level = parse_level_prefix(clean_para, current_level)
        if not para_body:
            continue

        # Découpage systématique en phrases (supportant ?, ! et . suivi d'une majuscule, même sans espace)
        sentences = [
            s.strip()
            for s in re.split(
                r'(?<=[!?])\s*|(?<!\b[Mm]\.)(?<!\b[Mm]me\.)(?<!\b[Dd]r\.)(?<!\b[Tt][ée]l\.)(?<!\b[Rr][ée]f\.)(?<!\b[Ee]nv\.)(?<=\.)\s*(?=[A-ZÀ-Ÿ"«])',
                para_body
            )
            if s.strip()
        ]

        for sent in sentences:
            sent_clean = clean_line_typography(sent)
            if not sent_clean or len(sent_clean) < 2:
                continue

            sent_clean = normalize_header_banner(sent_clean)
            if not sent_clean or len(sent_clean) < 2:
                continue

            if is_contact_or_boilerplate_line(sent_clean, direct_contacts):
                continue

            # Vérifier si la phrase interne change ou réinitialise le niveau
            sent_body, current_level = parse_level_prefix(sent_clean, current_level)
            if not sent_body:
                continue

            sub_segments = split_compound_line(sent_body, level_ctx=current_level)

            for seg in sub_segments:
                seg_clean = format_bullet_uniform(seg)
                if not seg_clean or len(seg_clean) < 2:
                    continue

                if is_contact_or_boilerplate_line(seg_clean, direct_contacts):
                    continue

                # Fusion des fragments numériques orphelins (ex: ligne séparée "300 m²" ou "16 ares")
                if re.match(r'^(?:environ\s*|~)?\d+(?:[.,]\d+)?\s*m²$', seg_clean, flags=re.IGNORECASE):
                    if cleaned_lines_all and not re.search(r'\d+\s*m²', cleaned_lines_all[-1]):
                        prev = cleaned_lines_all[-1]
                        prev_cat = classify_bullet_or_sentence(prev)
                        merged = f"{prev} de {seg_clean}"
                        cleaned_lines_all[-1] = merged
                        t_list = categorized_items.get(prev_cat, categorized_items["prestations_complementaires"])
                        if t_list and t_list[-1] == prev:
                            t_list[-1] = merged
                        continue
                if re.match(r'^(?:environ\s*|~)?\d+(?:[.,]\d+)?\s*ares?$', seg_clean, flags=re.IGNORECASE):
                    seg_clean = f"Terrain d'environ {seg_clean}"

                cat_key = classify_bullet_or_sentence(seg_clean)
                target_list = categorized_items.get(cat_key, categorized_items["prestations_complementaires"])
                if seg_clean.lower() not in [x.lower() for x in target_list]:
                    target_list.append(seg_clean)

                cleaned_lines_all.append(seg_clean)

    # 5. Assembler les sections finales dans l'ordre strict
    prop_upper = str(property_type or "").upper()
    is_residential_or_land = any(t in prop_upper for t in ['MAISON', 'VILLA', 'APPARTEMENT', 'STUDIO', 'DUPLEX', 'TERRAIN'])
    is_commercial = False
    if any(t in prop_upper for t in ['BUREAU', 'COMMERCIAL', 'DOCK', 'LOCAL']):
        is_commercial = True
    elif not is_residential_or_land and any(w in raw_text.lower() for w in ['local commercial', 'plateau de bureaux', 'dock artisanal', 'dock industriel', 'activité tertiaire', 'bail commercial']):
        is_commercial = True

    sections = []
    category_titles = {c["key"]: (c["title"], c["icon"]) for c in CATEGORY_DEFINITIONS}

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

    for k in ordered_keys:
        raw_items = categorized_items.get(k, [])
        items = sanitize_section_bullets(k, raw_items)
        if items:
            title, icon = category_titles[k]
            sections.append({
                "key": k,
                "title": title,
                "icon": icon,
                "items": items
            })

    # 6. Extraire les spécifications techniques chiffrées ancrées + badges atouts flash vérifiés
    tech_specs = extract_technical_specs(raw_text)

    highlights = []
    full_str = " ".join(cleaned_lines_all).lower()

    # Garde-fou anti faux-positif : 'rez-de-jardin de 80 m²' ne doit JAMAIS devenir 'Jardin ~80 m²'
    m_jardin = re.search(
        r'(?<!rez-de-)(?<!rez\sde\s)(?:\bjardin|\bespace\s+ext[ée]rieur\s+privatif|\bterrain)\s*(?:clos\s*|cl[ôo]tur[ée]\s*|arbor[ée]\s*|privatif\s*)?(?:d[\'’]\s*|de\s+)?(?:environ\s*)?(\d+(?:[.,]\d+)?)\s*(?:m²|m2|ares?)',
        full_str
    )
    if m_jardin:
        val = m_jardin.group(1).replace(" ", "")
        unit = "ares" if "are" in m_jardin.group(0) else "m²"
        highlights.append(f"🌿 Jardin/Terrain {val} {unit}")

    if tech_specs.get("surface_terrasse_m2"):
        t_m2 = tech_specs["surface_terrasse_m2"]
        t_str = str(int(t_m2)) if float(t_m2).is_integer() else str(t_m2).replace(".", ",")
        highlights.append(f"☕ Terrasse {t_str} m²")

    if tech_specs.get("has_sea_view"):
        highlights.append("🌊 Vue mer")

    if tech_specs.get("has_pool"):
        highlights.append("🏊 Piscine")

    if tech_specs.get("parking_detail"):
        p_det = tech_specs["parking_detail"]
        highlights.append(f"🚗 {p_det}")
    elif "garage" in full_str:
        highlights.append("🚗 Garage privatif")

    if "cellier" in full_str:
        highlights.append("📦 Cellier privatif")

    if tech_specs.get("has_air_conditioning"):
        highlights.append("❄️ Climatisé")

    if any(w in full_str for w in ("photovoltaïque", "photovoltaique", "panneaux solaires", "chauffe-eau solaire")):
        highlights.append("☀️ Solaire / Photovoltaïque")

    if any(w in full_str for w in ("volets roulants électriques", "volet roulant électrique", "volets électriques")):
        highlights.append("⚡ Volets électriques")

    if "suite parentale" in full_str:
        highlights.append("🛏️ Suite parentale")

    if "fitness" in full_str or "salle de sport" in full_str:
        highlights.append("🏋️ Fitness")

    return {
        "has_structured": len(sections) > 0,
        "direct_contacts": direct_contacts,
        "agency_metadata": agency_metadata,
        "technical_specs": tech_specs,
        "sections": sections,
        "key_highlights": highlights[:7],
        "cleaned_full_text": "\n\n".join(cleaned_lines_all)
    }


def extract_direct_contacts(text: str) -> List[Dict[str, Any]]:
    """Rétro-compatibilité : extrait les contacts directs à partir d'un texte brut."""
    contacts, _ = extract_direct_contacts_and_agency_metadata(text)
    return contacts
