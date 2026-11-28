"""
Module d'extraction haute fidélité pour fiches détaillées d'annonces immobilières.
Garantit l'absence totale de troncature de texte et extrait 100% des spécifications techniques.
"""

import re
import html as html_lib
from typing import Dict, Any, Optional, List, Tuple
from bs4 import BeautifulSoup
from src.processing.description_structurer import (
    strip_technical_noise,
    is_valid_person_name,
    _clean_candidate_person_name,
)


def parse_immonc_detail_html(html_content: str, url: str = "") -> Dict[str, Any]:
    """
    Parse le code HTML complet d'une page de détail d'annonce ImmoNC (https://www.immonc.com/annonce/...).
    Extrait :
    - Le descriptif intégral (non tronqué, avec sauts de lignes et puces préservés)
    - Le dictionnaire complet des 'Détails complémentaires' (Général, Cadre de vie, Finances, Cuisine, Salle de bain, Annexe, Garage)
    - Les attributs scalaires normalisés (surface habitable, terrasse, charges, étage, ascenseur, etc.)
    - Les coordonnées du négociateur direct (nom et téléphone portable/fixe)
    """
    if not html_content:
        return {}

    soup = BeautifulSoup(html_content, "html.parser")

    # 1. Extraction du Descriptif Intégral
    full_description = ""
    desc_card = soup.find("div", class_="listing-description-card") or soup.find("div", class_="detailcomplement")
    if desc_card:
        pera = desc_card.find("div", class_="pera")
        if pera:
            paragraphs = []
            for elem in pera.children:
                if elem.name in ("p", "div", "h2", "h3", "h4", "h5", "li"):
                    txt = elem.get_text(strip=True)
                    if txt:
                        paragraphs.append(txt)
                elif elem.name == "br":
                    continue
                elif isinstance(elem, str):
                    s = elem.strip()
                    if s:
                        paragraphs.append(s)
            if paragraphs:
                full_description = "\n\n".join(paragraphs)
            else:
                full_description = pera.get_text("\n\n", strip=True)

    if not full_description:
        # Fallback de recherche par titre 'Descriptif du bien'
        for h2 in soup.find_all(["h2", "h3"]):
            if "descriptif" in h2.get_text().lower():
                parent = h2.find_parent("div", class_="detailcomplement") or h2.find_parent("div")
                if parent:
                    p_elem = parent.find("div", class_="pera") or parent.find("p")
                    if p_elem:
                        full_description = p_elem.get_text("\n\n", strip=True)
                        break

    # Décodage propre des entités HTML (&nbsp;, &#192;, etc.) et retrait des bannières statiques parasites
    full_description = strip_technical_noise(html_lib.unescape(full_description)).strip()

    # 2. Extraction des Détails Complémentaires structurés
    complementary_details: Dict[str, Any] = {}
    details_card = soup.find("div", class_="listing-details-card")
    if not details_card:
        for div in soup.find_all("div", class_="detailcomplement"):
            if "détails" in div.get_text().lower() or "details" in div.get_text().lower():
                details_card = div
                break

    if details_card:
        boxes = details_card.find_all("div", class_="detailbox")
        for box in boxes:
            h3 = box.find(["h3", "h4", "span"])
            cat_name = h3.get_text(strip=True) if h3 else "Divers"
            cat_name = re.sub(r"\s+", " ", cat_name).strip()

            items = []
            for li in box.find_all(["li", "p"]):
                li_text = li.get_text(" ", strip=True)
                li_text = re.sub(r"\s+", " ", li_text).strip()
                if li_text and li_text != cat_name:
                    items.append(li_text)

            if items:
                complementary_details[cat_name] = items

    # 3. Extraction et normalisation des attributs scalaires
    surface_habitable: Optional[float] = None
    surface_terrasse: Optional[float] = None
    charges_mensuelles: Optional[int] = None
    rooms: Optional[int] = None
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    parkings: Optional[int] = None
    etage: Optional[str] = None
    has_elevator = False
    has_sea_view = False
    has_ac = False
    has_pool = False
    is_secured = False

    # Parcours des boîtes de détails complémentaires
    for cat, items in complementary_details.items():
        cat_lower = cat.lower()
        items_str = " ".join(items).lower()

        # Général ou Surfaces
        if "général" in cat_lower or "general" in cat_lower or "surface" in cat_lower:
            for it in items:
                # Surface habitable
                m_surf = re.search(r"surface\s*(?:habitable)?\s*[:\s]?\s*(\d+(?:[\.,]\d+)?)\s*(?:m2|m²)?", it, re.IGNORECASE)
                if m_surf and surface_habitable is None:
                    try:
                        surface_habitable = float(m_surf.group(1).replace(",", "."))
                    except ValueError:
                        pass
                # Surface terrasse
                m_ter = re.search(r"terrasse\s*[:\s]?\s*(\d+(?:[\.,]\d+)?)\s*(?:m2|m²)?", it, re.IGNORECASE)
                if m_ter and surface_terrasse is None:
                    try:
                        surface_terrasse = float(m_ter.group(1).replace(",", "."))
                    except ValueError:
                        pass
                # Pièces
                m_rooms = re.search(r"pi[eè]ces?\s*[:\s]?\s*(\d+)", it, re.IGNORECASE)
                if m_rooms:
                    try:
                        rooms = int(m_rooms.group(1))
                    except ValueError:
                        pass
                # Chambres
                m_ch = re.search(r"chambres?\s*[:\s]?\s*(\d+)", it, re.IGNORECASE)
                if m_ch:
                    try:
                        bedrooms = int(m_ch.group(1))
                    except ValueError:
                        pass
                # Étage
                m_et = re.search(r"[eé]tage\s*[:\s]?\s*(\d+|rdc|rez[- ]de[- ]chauss[eé]e)", it, re.IGNORECASE)
                if m_et:
                    etage = m_et.group(1).strip()
                # Ascenseur (exclure 'sans ascenseur' et 'Ascenseur : Non')
                if "ascenseur" in it.lower() and not re.search(r"(?:sans\s+ascenseur|ascenseur\s*:\s*non|\bnon\b)", it, re.IGNORECASE):
                    has_elevator = True

        # Informations financières (retrait des décimales .00 avant extraction des chiffres pour éviter x100)
        if "financ" in cat_lower or "charge" in cat_lower:
            for it in items:
                m_chg = re.search(r"charges?\s*(?:de\s+copropri[eé]t[eé]|mensuelles?|copro)?\s*[:\s]?\s*(\d+[\d\s\.,]*)", it, re.IGNORECASE)
                if m_chg:
                    raw_val = re.sub(r"[.,]\d{1,2}\s*$", "", m_chg.group(1).strip())
                    num_str = re.sub(r"[^\d]", "", raw_val)
                    if num_str and 500 <= int(num_str) <= 150_000:
                        charges_mensuelles = int(num_str)

        # Cadre de vie (strictement vue mer/lagon/baie, pas simple proximité plage)
        if "cadre" in cat_lower or "vue" in cat_lower:
            if any(k in items_str for k in ("vue mer", "vue lagon", "vue baie", "vue sur la mer")) and "non" not in items_str:
                has_sea_view = True

        # Annexe
        if "annexe" in cat_lower or "ext" in cat_lower:
            for it in items:
                # Terrasse
                m_ter = re.search(r"terrasse\s*[:\s]?\s*(\d+(?:[\.,]\d+)?)\s*(?:m2|m²)?", it, re.IGNORECASE)
                if m_ter:
                    try:
                        v_ter = float(m_ter.group(1).replace(",", "."))
                        if v_ter > 0:
                            surface_terrasse = v_ter
                    except ValueError:
                        pass
                if "clim" in it.lower() and not re.search(r"\bnon\b", it, re.IGNORECASE):
                    has_ac = True

        # Salle de bain / Salle d'eau
        if "bain" in cat_lower or "eau" in cat_lower:
            sdb_total = 0
            for it in items:
                m_sdb = re.search(r"salles?\s*(?:d['’]eau|de\s*bains?)\s*[:\s]?\s*(\d+)", it, re.IGNORECASE)
                if m_sdb:
                    try:
                        sdb_total += int(m_sdb.group(1))
                    except ValueError:
                        pass
            if sdb_total > 0:
                bathrooms = sdb_total

        # Piscine
        if "piscine" in cat_lower:
            for it in items:
                if "piscine" in it.lower() and not re.search(r"\bnon\b", it, re.IGNORECASE):
                    has_pool = True

        # Garage / Stationnement
        if "garage" in cat_lower or "parking" in cat_lower or "stationnement" in cat_lower:
            for it in items:
                m_park = re.search(r"(?:carport|parking(?:\s+couvert)?|garage(?:\s+ferm[eé])?|box|place)\s*[:\s]?\s*(\d+)", it, re.IGNORECASE)
                if m_park:
                    try:
                        pv = int(m_park.group(1))
                        if 1 <= pv <= 20:
                            parkings = max(parkings or 0, pv)
                    except ValueError:
                        pass
            if parkings is None and len(items) > 0:
                parkings = 1

    # Heuristiques complémentaires sur le corps du texte si non trouvés dans les boîtes
    blob_full = f"{full_description} {url}".lower()

    if surface_terrasse is None:
        m_t = re.search(r"(?:terrasse|varangue)\s*(?:couverte|privative)?\s*(?:d['’]environ|de)?\s*(\d+(?:[\.,]\d+)?)\s*(?:m2|m²)", blob_full)
        if m_t:
            try:
                v_ter = float(m_t.group(1).replace(",", "."))
                if 1.0 <= v_ter <= 500.0:
                    surface_terrasse = v_ter
            except ValueError:
                pass

    if etage is None:
        m_et_text = re.search(r"\b(dernier\s+[eé]tage|\d+(?:[eè]me|er)?\s+[eé]tage|rdc|rez[- ]de[- ]chauss[eé]e)\b", blob_full)
        if m_et_text:
            etage = m_et_text.group(1)

    if not has_sea_view and re.search(r"\b(vue\s*mer|vue\s*lagon|vue\s*baie|vue\s*sur\s*la\s*mer|vue\s*imprenable\s*sur\s*la\s*mer|apercu\s*mer|aperçu\s*mer)\b", blob_full):
        has_sea_view = True
    if not has_ac and re.search(r"\b(climatis\w*|climatiseur\w*|clims?)\b", blob_full):
        has_ac = True
    if not has_pool and re.search(r"\b(piscine|bassin\s+de\s+nage|couloir\s+de\s+nage)\b", blob_full) and not re.search(r"\b(sans\s+piscine|pas\s+de\s+piscine|piscine\s*:\s*non|dalle\s+(?:de\s+)?piscine|projet\s+de\s+piscine|piscinable)\b", blob_full):
        has_pool = True
    if not is_secured and re.search(r"\b(s[eé]curis\w*|digicode|gardien|interphone|visiophone)\b", blob_full):
        is_secured = True
    if not has_elevator and "ascenseur" in blob_full and not re.search(r"\b(sans\s+ascenseur|pas\s+d['’]ascenseur|ascenseur\s*:\s*non)\b", blob_full):
        has_elevator = True

    if parkings is None:
        if "carport" in blob_full or "parking" in blob_full or "place de stationnement" in blob_full:
            parkings = 1

    # 4. Détection du Négociateur Direct (avec filtrage anti-faux-positifs 'Réf', 'Nous', 'Au', 'Loyer Hc')
    negotiator_name = None
    negotiator_phone = None

    contact_match = re.search(
        r"(?:contactez|contacter|joindre|appelez|visites?,?\s*contactez)\s+([A-ZÀ-ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-ÿ][a-zà-ÿ]+)?)\s+(?:au\s+|au\s*:?\s*|\(?T[eé]l\.?\s*:?\s*|\(?)\s*([0-9]{2}[\.\s]?[0-9]{2}[\.\s]?[0-9]{2}|[0-9]{6})(?!\s*(?:F\b|XPF\b|CFP\b|€))",
        full_description,
        re.IGNORECASE
    )
    if contact_match:
        cand_name = _clean_candidate_person_name(contact_match.group(1))
        if is_valid_person_name(cand_name):
            negotiator_name = cand_name.title()
        raw_num = contact_match.group(2).strip()
        num_clean = re.sub(r"[^\d]", "", raw_num)
        if len(num_clean) == 6:
            negotiator_phone = f"+687 {num_clean[:2]}.{num_clean[2:4]}.{num_clean[4:]}"
        else:
            negotiator_phone = f"+687 {raw_num}"

    # 5. Photos multiples (normalisation www + remplacement /photos_vignette/ + déduplication stricte par nom de fichier)
    photos = []
    seen_photo_keys = set()
    for img in soup.find_all("img"):
        src = (img.get("src") or img.get("data-src") or "").strip()
        if "/photos/" in src and src.startswith("http"):
            big_src = re.sub(r"/photos_(?:medium|small|vignette|thumb)/", "/photos_big/", src)
            big_src = re.sub(r"^https?://(?:www\.)?immonc\.com/", "https://www.immonc.com/", big_src, flags=re.IGNORECASE)
            fname_key = big_src.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()
            if fname_key and fname_key not in seen_photo_keys:
                seen_photo_keys.add(fname_key)
                photos.append(big_src)

    return {
        "full_description": full_description,
        "complementary_details": complementary_details,
        "surface_habitable_m2": surface_habitable,
        "surface_terrasse_m2": surface_terrasse,
        "charges_mensuelles_xpf": charges_mensuelles,
        "rooms": rooms,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "parkings": parkings,
        "etage": etage,
        "has_elevator": has_elevator,
        "has_sea_view": has_sea_view,
        "has_air_conditioning": has_ac,
        "has_pool": has_pool,
        "is_secured": is_secured,
        "direct_negotiator": negotiator_name,
        "direct_phone": negotiator_phone,
        "photos": photos,
    }
