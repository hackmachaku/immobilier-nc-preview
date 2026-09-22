"""
Utilitaires de nettoyage de texte et resolution d'encodage (Mojibake)
pour les annonces et agences de Nouvelle-Caledonie.
"""

import re
from typing import Optional

MOJIBAKE_MAP = {
    '\u00c3\u00a9': 'é',
    '\u00c3\u00a8': 'è',
    '\u00c3\u00aa': 'ê',
    '\u00c3\u00ab': 'ë',
    '\u00c3\u00a0': 'à',
    '\u00c3\u00a2': 'â',
    '\u00c3\u00ae': 'î',
    '\u00c3\u00af': 'ï',
    '\u00c3\u00b4': 'ô',
    '\u00c3\u00bb': 'û',
    '\u00c3\u00b9': 'ù',
    '\u00c3\u00bc': 'ü',
    '\u00c3\u00a7': 'ç',
    '\u00c3\u0089': 'É',
    '\u00c3\u0088': 'È',
    '\u00c3\u0080': 'À',
    '\u00c2\u00a0': ' ',
    '\u00c2 ': ' ',
    '\u00c2': '',
    '\u00a0': ' ',
    '\u00e2\u0080\u0099': "'",
    '\u00e2\u0080\u0093': '–',
    '\u00e2\u0080\u0094': '—',
    '\u00e2\u0080\u009c': '“',
    '\u00e2\u0080\u009d': '”',
    '\u00e2\u0080\u00a2': '•',
    '\u00e2\u0080\u00a6': '…',
}


def fix_mojibake(text: Optional[str]) -> str:
    """
    Répare les altérations d'encodage UTF-8 (mojibake) fréquentes dans
    les flux scrapés (ex: 'CalÃ©donienne' -> 'Calédonienne', 'Â\xa0' -> ' ').
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    # 1. Tentative d'inversion directe si le texte est intégralement du mojibake
    if '\u00c3' in text or '\u00c2' in text:
        try:
            re_decoded = text.encode('latin1').decode('utf-8')
            if '\u00c3' not in re_decoded and '\u00c2' not in re_decoded:
                text = re_decoded
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    # 2. Remplacement par table de correspondances ciblées (pour les chaînes mixtes)
    for bad, good in MOJIBAKE_MAP.items():
        if bad in text:
            text = text.replace(bad, good)

    # 3. Élimination des résidus d'espaces insécables parasites
    text = re.sub(r'[\u00a0\u200b\u202f]', ' ', text)
    # Nettoyage de résidu Â isolé avant ponctuation ou espace
    text = re.sub(r'\u00c2(?=[\s\.,;:]|$)', '', text)
    text = re.sub(r'[ \t]+', ' ', text)

    return text.strip()


def _strip_accents(s: str) -> str:
    import unicodedata
    return unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('utf-8').lower().strip()


def normalize_agency_name(raw_name: Optional[str]) -> str:
    """
    Nettoie et résout le nom d'agence canonique NC à partir d'un nom potentiellement
    tronqué (ex: 'Calédonienne d' -> 'Calédonienne d\'Immobilier') ou altéré.
    """
    if not raw_name:
        return "Professionnel Immo NC"

    cleaned = fix_mojibake(raw_name).strip()
    c_norm = _strip_accents(cleaned)

    try:
        from src.domain.agencies_directory import NC_AGENCIES
        for ag in NC_AGENCIES:
            canonical_name = ag.get("name", "")
            ag_norm = _strip_accents(canonical_name)
            if ag_norm == c_norm:
                return canonical_name
            for alias in ag.get("aliases", []):
                alias_norm = _strip_accents(alias)
                if alias_norm == c_norm or (len(c_norm) >= 6 and alias_norm.startswith(c_norm)) or (len(alias_norm) >= 6 and c_norm.startswith(alias_norm)):
                    return canonical_name
    except Exception:
        pass

    return cleaned

    return cleaned
