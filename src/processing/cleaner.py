import re
import json
import html as html_lib
from typing import Optional, Tuple
from src.models.listing import RawListing, CleanedListing, TransactionType, PropertyType, Commune
from src.reference.geo import GeoReferential, get_geo_referential, normalize_text

_CP1252_CLEAN_TRANS = str.maketrans({
    "\x80": "€", "\x85": "...", "\x91": "'", "\x92": "'",
    "\x93": '"', "\x94": '"', "\x95": "•", "\x96": "–", "\x97": "—",
})


def normalize_etage(raw_etage: Optional[str]) -> Optional[str]:
    """Normalise les 41+ variantes brutes d'étage vers 10 libellés canoniques propres."""
    if not raw_etage:
        return None
    s = normalize_text(str(raw_etage))
    if not s or s in ("none", "nan", "null", "0"):
        return None
    if "jardin" in s or s == "rdj":
        return "Rez-de-jardin"
    if "chaussee" in s or s == "rdc":
        return "Rez-de-chaussée"
    if "dernier" in s:
        return "Dernier étage"
    if "plain pied" in s:
        return "Plain-pied"
    if "duplex" in s or "triplex" in s:
        return "Duplex"
    if re.search(r"\b(?:premier|1er|1e|1eme|1)\b", s) or s == "r 1":
        return "1er étage"
    m_num = re.search(r"\b([2-9]|1[0-5])(?:eme|e)?\b", s)
    if m_num:
        n = int(m_num.group(1))
        return "5ème étage et +" if n >= 5 else f"{n}ème étage"
    if "deuxieme" in s or "second" in s:
        return "2ème étage"
    if "troisieme" in s:
        return "3ème étage"
    if "quatrieme" in s:
        return "4ème étage"
    if "cinquieme" in s:
        return "5ème étage et +"
    return None


def parse_terrain_surface_m2(text: str) -> Optional[float]:
    """
    Parse de façon unifiée et sans erreur les surfaces de terrain calédoniennes :
    - '2ha 20 ares', '5 hectares et 60 ares', '17.5 Hectares', '2ha29' -> 22 000 / 56 000 / 175 000 / 22 900 m²
    - '17a 64ca', '10A96', '4.99 ares', '16,5 ares', '6 ares 50' -> 1 764 / 1 096 / 499 / 1 650 / 650 m²
    - 'terrain de 934 m²', 'parcelle 1 200 m2' -> 934 / 1 200 m²
    """
    if not text:
        return None
    s = text.lower().replace("’", "'")
    # 1. Hectares + optionnel ares (ex: "2ha 20 ares", "5 hectares et 60 ares", "2ha29", "17.5 ha")
    m_ha = re.search(
        r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:hectares?|ha)\b(?:\s*(?:et\s*)?(\d+(?:[.,]\d+)?)\s*(?:ares?|a\b))?",
        s,
    )
    if m_ha:
        try:
            ha_val = float(m_ha.group(1).replace(",", "."))
            a_val = float(m_ha.group(2).replace(",", ".")) if m_ha.group(2) else 0.0
            total = round(ha_val * 10_000.0 + a_val * 100.0, 1)
            if total > 0:
                return total
        except ValueError:
            pass

    # Forme compacte "2ha29" (2 ha 29 ares = 22 900 m²)
    m_ha_c = re.search(r"(?<!\d)(\d+)\s*ha\s*(\d{1,2})(?!\d)", s)
    if m_ha_c:
        try:
            total = float(int(m_ha_c.group(1)) * 10_000 + int(m_ha_c.group(2)) * 100)
            if total > 0:
                return total
        except ValueError:
            pass

    # 2. Forme notariale / cadastrale "17a 64ca", "10a96", "13a86ca"
    m_aca = re.search(r"(?<!\d)(\d{1,4})\s*a(?:res?)?\s*(\d{1,2})\s*(?:ca\b|(?!\d))", s)
    if m_aca:
        # Vérifier qu'on ne capture pas un mot quelconque en "a" (ex: "à 10")
        matched_raw = m_aca.group(0)
        if "ca" in matched_raw or "are" in matched_raw or re.match(r"^\d{1,3}a\d{2}$", matched_raw.strip()):
            try:
                total = float(int(m_aca.group(1)) * 100 + int(m_aca.group(2)))
                if total >= 50.0:
                    return total
            except ValueError:
                pass

    # 3. Ares décimaux ou entiers avec frontière gauche stricte (évite "4.99 ares" -> "99 ares" !)
    m_ares = re.search(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*ares?(?:\s*(\d{1,2})(?:\s*ca)?)?\b", s)
    if m_ares:
        try:
            ares_val = float(m_ares.group(1).replace(",", "."))
            ca_val = float(m_ares.group(2)) if (m_ares.group(2) and "." not in m_ares.group(1) and "," not in m_ares.group(1)) else 0.0
            total = round(ares_val * 100.0 + ca_val, 1)
            if total >= 50.0:
                return total
        except ValueError:
            pass

    # 4. Terrain / parcelle explicite en m² (exclut SHON / constructible / bungalow)
    m_ter = re.search(
        r"\b(?:terrain|parcelle|foncier)\b(?:\s+(?:clos|cl[ôo]tur[ée]|arbor[ée]|plat|viabilis[ée]|divisible|paysager|constructible|privatif|de|d'|et|d'une|surface|superficie|totale|environ)){0,6}\s*[:=]?\s*(\d+(?:[\s.,]\d+)?)\s*(?:m²|m2)\b",
        s,
    )
    if m_ter:
        raw_t = m_ter.group(1).strip()
        try:
            if re.match(r"^\d+[.,]\d{1,2}$", raw_t):
                val = float(raw_t.replace(",", "."))
            else:
                cleaned_t = re.sub(r"[^\d]", "", raw_t)
                val = float(cleaned_t) if cleaned_t else 0.0
            if 50.0 <= val <= 50_000_000.0:
                return val
        except ValueError:
            pass
    return None


def is_out_of_scope_listing(title: str, description: str, url: str = "") -> bool:
    """Détecte les annonces hors périmètre (recherches, mobilier seul, biens hors Nouvelle-Calédonie)."""
    t_norm = normalize_text(title or "")
    d_norm = normalize_text(description or "")
    # 1. Annonces de recherche ("RECHERCHE PETIT LOCAL", "Cherche appartement")
    if re.match(r"^(?:recherche|cherche)\b", t_norm) or re.search(r"\brecherche\s+(?:petit\s+)?(?:local|appartement|maison|villa|terrain|studio|f[1-6]|colocataire|location)\b", d_norm[:120]):
        return True
    # 2. Objets/mobilier non immobiliers ("Vendre table de séjour")
    if re.search(r"\b(?:table\s+de\s+sejour|canape\s+a\s+vendre|vendre\s+(?:une\s+)?table|meuble\s+tv|electromenager)\b", t_norm):
        return True
    # 3. Biens hors Nouvelle-Calédonie (Canada/Québec/France métropolitaine sans mention NC)
    if re.search(r"\b(?:montreal|quebec|canada|paris\s+\d+e|lyon\s+\d+e|marseille|bordeaux|toulouse|metropole)\b", f"{t_norm} {d_norm[:250]}"):
        if not re.search(r"\b(?:noumea|dumbea|paita|mont\s+dore|bourail|kone|koumac|caledonie|nc)\b", f"{t_norm} {d_norm[:250]}"):
            return True
    return False


class ListingCleaner:
    def __init__(self, geo_ref: Optional[GeoReferential] = None):
        self.geo_ref = geo_ref or get_geo_referential()

    def parse_price(self, raw_price: Optional[str], text_fallback: str = "") -> Optional[int]:
        """
        Extrait et normalise un prix en Francs Pacifique (XPF / F CFP).
        Gère les formats courants en NC :
        - '38 500 000 F' / '38.500.000 XPF'
        - '38.5 MF' / '38,5 M' / '42 MF' (Millions de Francs)
        - '125 000 F/mois'
        """
        candidate = raw_price or ""
        if not candidate and text_fallback:
            # Recherche d'un motif de prix dans le texte
            m = re.search(r"(\d+[\d\s\.,]*)\s*(?:f\s*cfp|xpf|f(?:\.|\b)|millions?|mf)", text_fallback, re.IGNORECASE)
            if m:
                candidate = m.group(0)

        if not candidate:
            return None

        clean = candidate.strip().lower()

        # Format "XX,X MF" ou "XX M" (Millions de Francs Pacifique)
        mf_match = re.search(r"(\d+(?:[,\.]\d+)?)\s*(?:mf|millions?)", clean)
        if mf_match:
            try:
                val = float(mf_match.group(1).replace(",", "."))
                return int(val * 1_000_000)
            except ValueError:
                pass

        # Format standard numérique (ex: "38 500 000", "38.500.000")
        digits_only = re.sub(r"[^\d]", "", clean)
        if digits_only:
            try:
                val = int(digits_only)
                # Filtre de cohérence pour éviter les numéros de téléphone ou codes postaux
                if val >= 10_000:  # Minimum réaliste pour un loyer mensuel en F CFP
                    return val
            except ValueError:
                pass

        return None

    def parse_surface(self, raw_surface: Optional[str], text_fallback: str = "") -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Extrait :
        1. Surface habitable (m²) — avec garde-fous anti-surface partielle (terrasse, chambre, SHON, 1 m² placeholder, terrain)
        2. Surface terrain (m²) — gère ha + ares, ares décimaux, 17a 64ca, et m²
        3. Surface terrasse / varangue (m²)
        """
        surface_hab: Optional[float] = None
        surface_terrain: Optional[float] = None
        surface_terrasse: Optional[float] = None

        blob_raw = f"{raw_surface or ''} {text_fallback}"
        blob = blob_raw.lower().replace("’", "'")

        # 2. Surface Terrain d'abord (pour éviter que "terrain de 934 m²" ne soit aspiré comme surface habitable)
        surface_terrain = parse_terrain_surface_m2(blob_raw)

        # 3. Surface Terrasse / Varangue / Deck (somme les terrasses explicites si 2 chalets/villas)
        ter_matches = re.findall(
            r"\b(?:terrasses?|varangues?|decks?)\s*(?:couvertes?|privatives?|am[eé]nag[eé]es?)?\s*(?:d['’]environ|de\s+plus\s+de|de|d['’]|:)?\s*(?:environ\s*)?(\d+(?:[,\.]\d+)?)\s*(?:m²|m2)\b",
            blob,
        )
        if ter_matches:
            try:
                vals = [float(v.replace(",", ".")) for v in ter_matches if 1.0 <= float(v.replace(",", ".")) <= 500.0]
                if vals:
                    surface_terrasse = vals[0]
            except ValueError:
                pass

        # 1. Surface Habitable
        if raw_surface:
            rs_clean = str(raw_surface).strip()
            if "+" in rs_clean:
                parts = re.findall(r"\d+(?:[,\.]\d+)?", rs_clean)
                if parts:
                    tot = sum(float(p.replace(",", ".")) for p in parts)
                    if 9.0 <= tot <= 25_000.0:
                        surface_hab = tot
            else:
                num_match = re.search(r"(\d+(?:[,\.]\d+)?)", rs_clean)
                if num_match:
                    try:
                        v = float(num_match.group(1).replace(",", "."))
                        if 9.0 <= v <= 25_000.0:
                            surface_hab = v
                    except ValueError:
                        pass
        else:
            # Préférence absolue à "surface habitable" ou "X m² habitables"
            m_hab_exp = re.search(
                r"(?:surface|superficie)\s+(?:habitable|utile|totale|commerciale|int[eé]rieure)\s*(?:de|d'|d'environ|:)?\s*(?:environ\s*)?(\d+(?:[,\.]\d+)?)\s*(?:m²|m2)\b|"
                r"(\d+(?:[,\.]\d+)?)\s*(?:m²|m2)\s*(?:hab(?:itables?)?|int(?:[eé]rieurs?)?|utiles?)\b",
                blob,
            )
            if m_hab_exp:
                try:
                    v = float((m_hab_exp.group(1) or m_hab_exp.group(2)).replace(",", "."))
                    if 9.0 <= v <= 25_000.0:
                        surface_hab = v
                except ValueError:
                    pass
            else:
                # Neutraliser les surfaces partielles / annexes / terrain / SHON avant le fallback générique
                cleaned_for_hab = re.sub(
                    r"\b(?:terrasses?|varangues?|decks?|balcons?|jardins?|terrains?|parcelles?|shon|constructibles?|"
                    r"chambres?|s[eé]jours?|salons?|cuisines?|salles?\s+d['e]|garages?|celliers?|appentis|carports?|"
                    r"bureaux?\s+de|bungalows?\s+de|dalles?|piscines?|accueils?)\b[^.,;\n]{0,45}?\d+(?:[,\.]\d+)?\s*(?:m²|m2)\b",
                    " ",
                    blob,
                )
                hab_match = re.search(r"(?<!\d)(\d+(?:[,\.]\d+)?)\s*(?:m²|m2|metres\s*carres?)\b", cleaned_for_hab)
                if hab_match:
                    try:
                        v = float(hab_match.group(1).replace(",", "."))
                        # Rejeter les placeholders (< 9 m²) et les valeurs identiques à surface_terrain >= 400 m²
                        if 9.0 <= v <= 25_000.0 and not (surface_terrain and surface_terrain >= 400.0 and abs(v - surface_terrain) <= 1.0):
                            surface_hab = v
                    except ValueError:
                        pass

        if surface_hab is not None and surface_hab < 9.0:
            surface_hab = None
        if surface_terrain is not None and surface_terrain < 0:
            surface_terrain = None
        if surface_terrasse is not None and surface_terrasse <= 0:
            surface_terrasse = None

        return surface_hab, surface_terrain, surface_terrasse

    def parse_rooms(self, raw_rooms: Optional[str], text_fallback: str = "") -> Tuple[Optional[int], Optional[int]]:
        """
        Extrait le nombre de pièces totales et de chambres (ex: F3 -> 3 pièces, 2 chambres).
        """
        rooms: Optional[int] = None
        bedrooms: Optional[int] = None

        # Nettoyage préalable : suppression des balises [IMG: ...] et URLs pour éviter les faux positifs hexadécimaux/UUIDs
        clean_text = re.sub(r"\[IMG:\s*https?://[^\]]+\]", " ", text_fallback or "")
        clean_text = re.sub(r"https?://\S+", " ", clean_text)
        blob = f"{raw_rooms or ''} {clean_text}".lower()

        # 1. Détection des Studios / Studettes (1 pièce, 0 chambre)
        if re.search(r"\b(studio|studette|chambre d'étudiant)\b", blob):
            rooms = 1
            bedrooms = 0

        # 2. Détection type F1, F2, F3, F4, F5, F6, T1, T2, T3... (limité strictement de 1 à 12)
        f_match = re.search(r"(?:^|[\s,;:(>\-])[ft]([1-9]|1[0-2])(?:\b|bis|ter|[\s,;:)<\-]|$)", blob)
        if f_match:
            try:
                val = int(f_match.group(1))
                if 1 <= val <= 12:
                    rooms = val
                    if rooms > 1 and bedrooms is None:
                        bedrooms = rooms - 1
            except ValueError:
                pass

        # 2b. Détection ">F5" ou "F5+" ou "F5 et +"
        if re.search(r"(?:>|\+)\s*f5|f5\s*(?:\+|et\s*plus)", blob):
            if rooms is None or rooms < 5:
                rooms = 5
                if bedrooms is None:
                    bedrooms = 4

        # 3. Détection explicite "X pièces"
        pieces_match = re.search(r"\b([1-9]|1[0-2])\s*pi[èe]ces?\b", blob)
        if pieces_match and rooms is None:
            try:
                val = int(pieces_match.group(1))
                if 1 <= val <= 12:
                    rooms = val
                    if rooms > 1 and bedrooms is None:
                        bedrooms = rooms - 1
            except ValueError:
                pass

        # 4. Détection explicite "X chambres"
        chambres_match = re.search(r"\b([1-9]|1[0-2])\s*chambres?\b", blob)
        if chambres_match:
            try:
                bed_val = int(chambres_match.group(1))
                if 1 <= bed_val <= 10:
                    bedrooms = bed_val
                    if rooms is None:
                        rooms = bedrooms + 1
            except ValueError:
                pass

        # Validation finale de cohérence (1 à 15 pièces max)
        if rooms is not None and (rooms < 1 or rooms > 15):
            rooms = None
        if bedrooms is not None and (bedrooms < 0 or bedrooms > 15):
            bedrooms = None

        return rooms, bedrooms

    def detect_property_type(self, declared: Optional[str], title: str, description: str) -> PropertyType:
        """Identifie le type de bien immobilier avec priorité stricte au type déclaré puis au titre."""
        dec_norm = normalize_text(declared or "")
        title_norm = normalize_text(title or "")
        desc_norm = normalize_text(description or "")

        # 1. Priorité absolue au type explicitement déclaré par la source
        if dec_norm:
            if re.search(r"\b(parking|garage|box)\b", dec_norm) and not re.search(r"\b(villa|maison|appartement)\b", dec_norm):
                return PropertyType.LOCAL_COMMERCIAL
            if re.search(r"\b(terrain|parcelle|foncier)\b", dec_norm):
                return PropertyType.TERRAIN
            if re.search(r"\bdock\s*habitable\b", dec_norm):
                return PropertyType.MAISON_VILLA
            if re.search(r"\b(dock|entrepot|hangar)\b", dec_norm):
                return PropertyType.DOCK
            if re.search(r"\b(local|locaux|bureau|bureaux|commerce|boutique|commercial|professionnel)\b", dec_norm):
                return PropertyType.LOCAL_COMMERCIAL
            if re.search(r"\b(immeuble|murs|batiment)\b", dec_norm):
                return PropertyType.IMMEUBLE
            if re.search(r"\b(appartement|studio|duplex|triplex|attique|loft)\b", dec_norm):
                return PropertyType.APPARTEMENT
            if re.search(r"\b(villa|maison|propriete|chalet|bungalow)\b", dec_norm):
                return PropertyType.MAISON_VILLA

        # 2. Analyse du titre (sans contamination par les mentions "terrain de 8 ares" dans un titre de villa)
        if title_norm:
            if re.match(r"^(?:a\s+(?:vendre|louer)\s*[-:]?\s*)?(?:place\s+de\s+)?(?:parking|garage|box)\b", title_norm) and not re.search(r"\b(villa|maison|appartement|f[1-6])\b", title_norm):
                return PropertyType.LOCAL_COMMERCIAL
            if re.search(r"\bdock\s*habitable\b", title_norm):
                return PropertyType.MAISON_VILLA
            if re.search(r"\b(dock|entrepot|hangar)\b", title_norm):
                return PropertyType.DOCK
            if re.search(r"\b(local|locaux|bureau|bureaux|commerce|boutique|fonds\s*de\s*commerce|murs\s*commerciaux)\b", title_norm):
                return PropertyType.LOCAL_COMMERCIAL
            if re.search(r"\b(immeuble|batiment)\b", title_norm):
                return PropertyType.IMMEUBLE
            # Un titre qui commence par "terrain" / "parcelle" est un terrain même s'il mentionne "idéal villa"
            if re.search(r"^(?:a\s+vendre\s*[-:]?\s*)?(?:beau\s+|grand\s+|superbe\s+|magnifique\s+)?(?:terrain|parcelle|lot\s+de\s+terrain)\b", title_norm):
                return PropertyType.TERRAIN
            if re.search(r"\b(villa|maison|propriete|chalet|bungalow)\b", title_norm):
                return PropertyType.MAISON_VILLA
            if re.search(r"\b(appartement|studio|duplex|triplex|attique|loft)\b", title_norm):
                return PropertyType.APPARTEMENT
            if re.search(r"\b(terrain|parcelle|lotissement)\b", title_norm):
                return PropertyType.TERRAIN

        # 3. Fallback sur le descriptif (en neutralisant "pour construire une villa/maison" sur un terrain)
        if desc_norm:
            if re.search(r"\bterrain\s+(?:constructible|a\s+batir|viabilise|nu|plat|agricole)\b", desc_norm) and not re.search(r"\b(?:villa|maison|appartement)\s+(?:de\s+type\s+)?f[1-6]\b", desc_norm):
                return PropertyType.TERRAIN
            if re.search(r"\bdock\s*habitable\b", desc_norm):
                return PropertyType.MAISON_VILLA
            if re.search(r"\b(dock|entrepot|hangar)\b", desc_norm):
                return PropertyType.DOCK
            if re.search(r"\b(local\s+commercial|plateau\s+de\s+bureaux|bureaux?\s+a\s+louer|fonds\s+de\s+commerce)\b", desc_norm):
                return PropertyType.LOCAL_COMMERCIAL
            if re.search(r"\b(immeuble\s+de\s+rapport|immeuble)\b", desc_norm):
                return PropertyType.IMMEUBLE
            if re.search(r"\b(appartement|studio|duplex|triplex|attique|loft)\b", desc_norm):
                return PropertyType.APPARTEMENT
            if re.search(r"\b(villa|maison|propriete)\b", desc_norm):
                return PropertyType.MAISON_VILLA
            if re.search(r"\b(terrain|parcelle|lotissement)\b", desc_norm):
                return PropertyType.TERRAIN
            if re.search(r"\b(local|bureau|commerce|boutique)\b", desc_norm):
                return PropertyType.LOCAL_COMMERCIAL

        return PropertyType.AUTRE

    def detect_transaction_type(
        self,
        declared: Optional[str],
        title: str,
        description: str,
        price: Optional[int],
        raw_price_str: Optional[str] = None,
    ) -> TransactionType:
        """
        Détermine avec précision et robustesse s'il s'agit d'une vente ou d'une location.
        Intègre des garde-fous financiers absolus adaptés au marché calédonien :
        - En Nouvelle-Calédonie, aucun bien bâti ou terrain ne se vend à moins de 1 500 000 F CFP.
          Tout montant inférieur à 1.5M F CFP est systématiquement un loyer mensuel (Location).
        - Tout bien à plus de 3 000 000 F CFP est une Vente (sauf déclaration explicite de bail commercial).
        - Les mentions d'investissement locatif ("idéal investisseur", "rentabilité locative", "actuellement loué")
          dans les annonces de vente ne doivent pas contaminer la détection.
        """
        dec_lower = (declared or "").strip().lower()
        is_declared_loc = bool("locat" in dec_lower or "louer" in dec_lower)
        is_declared_vente = bool("vent" in dec_lower or "achat" in dec_lower or "vendre" in dec_lower)

        price_text = f"{raw_price_str or ''} {title} {description}".lower()
        has_mois_in_price = bool(re.search(r"(?:/mois|par\s*mois|f/mois|f\s*cfp/mois)", price_text))

        # 1. RÈGLES FINANCIÈRES ABSOLUES (GARDE-FOUS DU MARCHÉ CALÉDONIEN)
        if price is not None:
            # Sous 1.5M F CFP, il est matériellement impossible qu'il s'agisse d'une vente immobilière
            # (un dock à 360 000 F, un F2 à 65 000 F, ou un local à 164 550 F sont des loyers mensuels)
            if price < 1_500_000:
                return TransactionType.LOCATION

            # Au-dessus de 3 000 000 F CFP, c'est presque toujours une vente.
            # Seuls de rares baux commerciaux de complexes entiers dépassent ce montant avec indication de loyer mensuel.
            if price >= 3_000_000 and not (is_declared_loc and has_mois_in_price):
                return TransactionType.VENTE

        # 2. PRISE EN COMPTE DU STATUT DÉCLARÉ PAR LA SOURCE (SI COHÉRENT AVEC LE PRIX)
        if is_declared_loc and not is_declared_vente:
            if price is None or price < 3_000_000:
                return TransactionType.LOCATION
        if is_declared_vente and not is_declared_loc:
            if price is None or price >= 1_500_000:
                return TransactionType.VENTE

        # 3. ANALYSE SÉMANTIQUE DU TITRE ET DE LA DESCRIPTION
        blob = f"{title} {description}".lower()

        # Neutralisation des expressions d'investissement locatif dans les ventes
        blob_clean = re.sub(
            r"\b(id[ée]al\s*(?:pour\s*)?investiss(?:eur|ement)|rentabilit[ée]\s*locative|rapport\s*locatif|actuellement\s*lou[ée]|vendu\s*lou[ée]|possibilit[ée]\s*de\s*location)\b",
            " ",
            blob
        )

        has_loc = bool(re.search(r"\b(louer|location|loyer|mensuel|charges comprises|f/mois|par mois|bail commercial)\b", blob_clean)) or has_mois_in_price
        has_vente = bool(re.search(r"\b(vente|vendre|acheter|achat|acquerir|prix fai|fai inclus)\b", blob_clean))

        if has_loc and not has_vente:
            return TransactionType.LOCATION
        if has_vente and not has_loc:
            return TransactionType.VENTE

        # 4. ARBITRAGE FINAL PAR LE PRIX SI INDÉTERMINÉ
        if price is not None:
            if price < 1_500_000:
                return TransactionType.LOCATION
            else:
                return TransactionType.VENTE

        return TransactionType.VENTE

    def parse_furnished(
        self,
        facilities: Optional[list] = None,
        title: str = "",
        description: str = "",
        property_type: Optional[PropertyType] = None,
        transaction_type: Optional[TransactionType] = None,
    ) -> Optional[bool]:
        """
        Détermine si le bien est meublé (True), non meublé (False) ou non spécifié (None).
        Donne la priorité aux déclarations explicites dans facilities, puis analyse les motifs textuels.
        """
        # Seuls les biens d'habitation sont pertinents
        if property_type in (PropertyType.TERRAIN, PropertyType.DOCK):
            return None

        # 1. Vérification dans les facilities fournies par l'API
        if facilities and isinstance(facilities, list):
            fac_lower = [str(f).strip().lower() for f in facilities]
            if "meuble" in fac_lower or "meublé" in fac_lower:
                return True

        blob = f"{title} {description}".lower()

        # 2. Détection explicite non-meublé / loué vide (prioritaire pour éviter les faux-positifs)
        if re.search(r"\b(non\s*meubl[eé]e?s?|non-meubl[eé]e?s?|lou[eé]\s*vide|logement\s*vide|appartement\s*vide|villa\s*vide|maison\s*vide|bail\s*vide|vendu\s*vide)\b", blob):
            return False

        # 3. Détection explicite meublé (bornes de mots strictes : exclut 'immeuble', 'tout équipé' seul et home staging IA)
        blob_no_staging = re.sub(
            r"(?:images?|photos?|projections?|visuels?|home\s*staging|virtuel(?:le)?s?|non[\s-])\s*(?:de\s+)?meubl[eé]e?s?",
            " ",
            blob,
        )
        if re.search(r"\b(meubl[eé]e?s?|enti[eè]rement\s*meubl[eé]e?s?|semi[- ]meubl[eé]e?s?|tout\s*meubl[eé]e?s?|[eé]quip[eé]e?\s+et\s+meubl[eé]e?s?|vendu\s+avec\s+tout\s+le\s+mobilier)\b", blob_no_staging):
            return True

        return None

    def clean(self, raw: RawListing) -> Optional[CleanedListing]:
        """Transforme une RawListing en CleanedListing validée et exploitable."""
        # Nettoyage CP1252 & HTML brut dès l'entrée
        raw_desc_in = (raw.description or "").translate(_CP1252_CLEAN_TRANS)
        raw_desc_in = re.sub(r"[\x80-\x9f]", " ", raw_desc_in)
        raw_desc_in = html_lib.unescape(raw_desc_in)
        raw_desc_in = re.sub(r"<\s*br\s*/?\s*>", "\n", raw_desc_in, flags=re.IGNORECASE)
        raw_desc_in = re.sub(r"</?[a-zA-Z][^>]*>", "", raw_desc_in)

        # Filtrage hors périmètre (recherches, mobilier non immo, biens hors Nouvelle-Calédonie)
        if is_out_of_scope_listing(raw.title or "", raw_desc_in, raw.url or ""):
            return None

        price = self.parse_price(raw.raw_price, f"{raw.title} {raw_desc_in}")
        if price is None:
            # Une annonce sans prix exploitable est ignorée pour les calculs d'estimation
            return None

        # Correction défensive des ventes divisées par 1 000 (ex: 39 900 F alors que vente déclarée / prix en MF)
        dec_tx = (raw.transaction_type_declared or "").strip().lower()
        desc_low_chk = f"{raw.title} {raw_desc_in}".lower()
        if 10_000 <= price <= 500_000 and ("vent" in dec_tx or "/vente/" in (raw.url or "").lower()):
            has_million_hint = bool(re.search(r"\b(?:prix\s*(?:de\s+vente|fai)?\s*:?\s*\d{1,3}[\s.,]?\d{0,3}\s*(?:mf|millions?|000\s*000)|actuellement\s+lou[ée]|bail\s+en\s+cours|vendu\s+lou[ée])\b", desc_low_chk))
            is_rent_explicit = bool(re.search(r"/\s*mois|par\s+mois|f\s*cfp\s*/\s*mois", str(raw.raw_price or "").lower()))
            if has_million_hint and not is_rent_explicit:
                price = int(price * 1_000)

        surface_hab, surface_terrain, surface_terrasse = self.parse_surface(
            raw.raw_surface, f"{raw.title} {raw_desc_in}"
        )
        prop_type = self.detect_property_type(raw.property_type_declared, raw.title, raw_desc_in)
        trans_type = self.detect_transaction_type(
            raw.transaction_type_declared,
            raw.title,
            raw_desc_in,
            price,
            raw_price_str=raw.raw_price,
        )

        # Pour un terrain nu, la surface déclarée est une surface terrain, JAMAIS une surface habitable
        if prop_type == PropertyType.TERRAIN:
            if surface_terrain is None and surface_hab is not None:
                surface_terrain = surface_hab
            surface_hab = None

        # Les biens non résidentiels (terrains, docks, locaux professionnels) n'ont pas de pièces d'habitation
        if prop_type in (PropertyType.TERRAIN, PropertyType.DOCK, PropertyType.LOCAL_COMMERCIAL, PropertyType.IMMEUBLE):
            rooms, bedrooms = None, None
        else:
            rooms, bedrooms = self.parse_rooms(raw.raw_rooms, f"{raw.title} {raw_desc_in}")

        # Localisation hiérarchique :
        clean_desc = re.sub(r'(?:contact|tél|tel|email|mail|agent|notre agence|retrouvez-nous)\s*:?.*', '', raw_desc_in, flags=re.IGNORECASE)
        commune, quartier, _ = self.geo_ref.find_location(raw.title or "")

        if not quartier:
            if raw.raw_location:
                c_loc, q_loc, _ = self.geo_ref.find_location(raw.raw_location)
                if q_loc and (commune == Commune.AUTRE or c_loc == commune):
                    quartier = q_loc
                    commune = c_loc
                elif commune == Commune.AUTRE and c_loc != Commune.AUTRE:
                    commune = c_loc

        if not quartier:
            c_blob, q_blob, _ = self.geo_ref.find_location(clean_desc)
            if q_blob and (commune == Commune.AUTRE or c_blob == commune):
                quartier = q_blob
                commune = c_blob
            elif commune == Commune.AUTRE and c_blob != Commune.AUTRE:
                commune = c_blob

        unique_id = f"{raw.source.lower()}_{raw.source_id}"

        # Attributs qualitatifs (stricts : pas de bord de mer -> vue mer, pas de jacuzzi/dalle -> piscine)
        raw_facs = getattr(raw, "facilities", None) or []
        blob_features = f"{raw.title} {raw_desc_in}".lower()
        blob_norm = normalize_text(blob_features)

        has_sea_view = bool(re.search(r"\b(vue\s*(?:sur\s+la\s+)?mer|vue\s*(?:sur\s+le\s+)?lagon|vue\s*(?:sur\s+la\s+)?baie|vue\s*panoramique\s*(?:sur\s+la\s+)?mer|apercu\s*mer)\b", blob_norm))
        if "vue_mer" in raw_facs:
            has_sea_view = True

        has_pool = bool(
            re.search(r"\b(piscine|bassin\s+de\s+nage|couloir\s+de\s+nage)\b", blob_norm)
            and not re.search(r"\b(sans\s+piscine|pas\s+de\s+piscine|piscine\s*:\s*non|dalle\s+(?:de\s+)?piscine|projet\s+de\s+piscine|piscinable)\b", blob_norm)
        )
        if "piscine" in raw_facs or "piscine_commune" in raw_facs:
            has_pool = True

        has_ac = bool(re.search(r"\b(climatis\w*|climatiseur\w*|clims?|air\s+conditionne)\b", blob_norm))
        if "climatisation" in raw_facs:
            has_ac = True

        is_sec = bool(re.search(r"\b(securis\w*|gardien|digicode|interphone|visiophone|alarme|portail\s+electrique|portail\s+motorise)\b", blob_norm))
        if "securite" in raw_facs:
            is_sec = True

        is_furnished = self.parse_furnished(
            facilities=raw_facs,
            title=raw.title,
            description=raw_desc_in,
            property_type=prop_type,
            transaction_type=trans_type,
        )

        # Extraction des charges mensuelles (avec tolérance faute de frappe 'XFP', retrait .00 et exclusion 'hors charges')
        charges: Optional[int] = None
        if getattr(raw, "raw_charges", None):
            raw_c_str = re.sub(r"[.,]\d{1,2}\s*$", "", str(raw.raw_charges).strip())
            clean_c = re.sub(r"[^\d]", "", raw_c_str)
            if clean_c and 500 <= int(clean_c) <= 150_000:
                charges = int(clean_c)
        elif raw_desc_in:
            m_chg = re.search(
                r"(?<!hors\s)(?<!sans\s)(?<!toute\s)(?<!aucune\s)\bcharges?\s*(?:mensuelles?|de\s+copropri[eé]t[eé]|copro|en\s+sus)?\s*[:=\s]?\s*(\d{1,3}(?:[\s.]?\d{3})*|\d{3,6})(?:[.,]\d{1,2})?\s*(?:f\s*cfp|xpf|xfp|f\b|cfp|/mois)",
                raw_desc_in,
                re.IGNORECASE
            )
            if m_chg:
                clean_c = re.sub(r"[^\d]", "", m_chg.group(1))
                if clean_c and 500 <= int(clean_c) <= 150_000:
                    charges = int(clean_c)
        # Garde-fou : en location, les charges ne peuvent pas dépasser le loyer principal
        if charges is not None and trans_type == TransactionType.LOCATION and price > 0 and charges >= price:
            charges = None

        # Surface terrasse fallback
        if surface_terrasse is None and getattr(raw, "raw_terrace", None):
            m_ter = re.search(r"(\d+(?:[\.,]\d+)?)", str(raw.raw_terrace))
            if m_ter:
                try:
                    vt = float(m_ter.group(1).replace(",", "."))
                    if 1.0 <= vt <= 500.0:
                        surface_terrasse = vt
                except ValueError:
                    pass

        # Stationnement & Parking (exclusion de freebox/livebox et "lit 2 places" / "canapé 3 places")
        parkings: Optional[int] = None
        parking_detail: Optional[str] = None
        blob_park_clean = re.sub(r"\b(?:freebox|livebox|bbox|box\s*(?:internet|fibre|tv)|lit\s+\d+\s*places?|canap[eé]\s+\d+\s*places?)\b", " ", blob_norm)
        if re.search(r"\bcarport\b", blob_park_clean):
            parking_detail = "Carport"
            parkings = 1
        elif re.search(r"\b(garage|box)\b", blob_park_clean):
            parking_detail = "Garage fermé"
            parkings = 1
        elif re.search(r"\b(parking|stationnement)\b", blob_park_clean):
            parking_detail = "Parking"
            parkings = 1

        word_to_int = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6}
        m_park_num = re.search(
            r"\b([1-9]|1[0-9]|20|deux|trois|quatre|cinq|six)\s*(?:places?\s*(?:de\s*)?(?:parking|stationnement|voiture|couvertes?|privatives?|exterieures?|interieures?|aeriennes?)|parkings?|carports?|garages?|box\s*(?:fermes?)?|stationnements?|pkg)\b",
            blob_park_clean,
        )
        if m_park_num:
            try:
                tok = m_park_num.group(1).lower()
                parkings = word_to_int.get(tok, int(tok))
            except ValueError:
                pass
        else:
            m_carport_v = re.search(r"\bcarport\s+pour\s+(\d+|deux|trois|quatre)\s+v[eé]hicules?\b", blob_park_clean)
            if m_carport_v:
                tok = m_carport_v.group(1).lower()
                parkings = word_to_int.get(tok, int(tok) if tok.isdigit() else 1)

        # Étage et Ascenseur (normalisé vers 10 valeurs canoniques + exclusion 'commerces au RDC')
        etage: Optional[str] = normalize_etage(getattr(raw, "raw_floor", None))
        if not etage:
            blob_no_shop_rdc = re.sub(r"\b(?:commerces?|locaux|boutiques?|bureaux?|parking|garage)\s+(?:au|en)\s+(?:rdc|rez[- ]de[- ]chauss[eé]e)\b", " ", blob_features)
            m_et = re.search(
                r"\b(dernier\s+[eé]tage|(?:premier|deuxi[eè]me|troisi[eè]me|quatri[eè]me|cinqui[eè]me|\d+(?:[eè]me|er|e))\s+[eé]tage|au\s+\d+(?:[eè]me|er|e)\b|rdc|rdj|rez[- ]de[- ]chauss[eé]e|rez[- ]de[- ]jardin|r\+[1-9])\b",
                blob_no_shop_rdc,
            )
            if m_et:
                etage = normalize_etage(m_et.group(1))

        has_elevator = False
        bathrooms: Optional[int] = None
        comp_dict = getattr(raw, "complementary_details", None)
        if isinstance(comp_dict, dict):
            sdb_from_comp = 0
            for cat, items in comp_dict.items():
                if not isinstance(items, list):
                    continue
                cat_norm = normalize_text(str(cat))
                if "meuble" in cat_norm and len(items) >= 2 and is_furnished is None and prop_type not in (PropertyType.TERRAIN, PropertyType.DOCK):
                    is_furnished = True
                for it in items:
                    it_s = str(it).strip()
                    it_norm = normalize_text(it_s)
                    if "ascenseur" in it_norm and not re.search(r"(?:sans\s+ascenseur|ascenseur\s*:\s*non|\bnon\b)", it_norm):
                        has_elevator = True
                    if "clim" in it_norm and not re.search(r"\bnon\b", it_norm):
                        has_ac = True
                    if "piscine" in it_norm and not re.search(r"\b(non|piscinable)\b", it_norm):
                        has_pool = True
                    if any(w in it_norm for w in ("vue mer", "vue lagon", "vue baie")) and not re.search(r"\bnon\b", it_norm):
                        has_sea_view = True
                    # Surface habitable depuis complementaryDetails (ex: 'Surface 90 m2')
                    if prop_type != PropertyType.TERRAIN and surface_hab is None and ("general" in cat_norm or "surface" in cat_norm):
                        m_sh = re.search(r"surface\s*(?:habitable)?\s*(\d+(?:[.,]\d+)?)\s*(?:m2|m²)?", it_norm)
                        if m_sh:
                            try:
                                v_sh = float(m_sh.group(1).replace(",", "."))
                                if 9.0 <= v_sh <= 25_000.0:
                                    surface_hab = v_sh
                            except ValueError:
                                pass
                    # Pièces & Chambres depuis complementaryDetails
                    if prop_type not in (PropertyType.TERRAIN, PropertyType.DOCK, PropertyType.LOCAL_COMMERCIAL, PropertyType.IMMEUBLE):
                        if rooms is None and re.search(r"\bpieces?\s+(\d+)", it_norm):
                            rv = int(re.search(r"\bpieces?\s+(\d+)", it_norm).group(1))
                            if 1 <= rv <= 15:
                                rooms = rv
                        if bedrooms is None and re.search(r"\bchambres?\s+(\d+)", it_norm):
                            bv = int(re.search(r"\bchambres?\s+(\d+)", it_norm).group(1))
                            if 0 <= bv <= 12:
                                bedrooms = bv
                    if any(w in it_norm for w in ("salle d eau", "salle de bain", "salles de bain", "sdb", "sde")):
                        m_b = re.search(r"(\d+)", it_norm)
                        if m_b:
                            b_val = int(m_b.group(1))
                            if 1 <= b_val <= 15:
                                sdb_from_comp += b_val
                    # Surface terrain depuis complementaryDetails ('Sur terrain 15' -> 15 ares = 1500 m², 'Sur terrain 9450' -> 9450 m²)
                    if "terrain" in cat_norm or "sur terrain" in it_norm or "parcelle" in it_norm:
                        parsed_t = parse_terrain_surface_m2(it_s)
                        if not parsed_t:
                            m_bare_t = re.search(r"sur\s+terrain\s+(\d+(?:[.,]\d+)?)\s*$", it_norm)
                            if m_bare_t:
                                try:
                                    bt = float(m_bare_t.group(1).replace(",", "."))
                                    if 0.5 <= bt < 50.0:
                                        parsed_t = round(bt * 100.0, 1)  # En NC, "Sur terrain 15" = 15 ares
                                    elif 50.0 <= bt <= 50_000_000.0:
                                        parsed_t = bt
                                except ValueError:
                                    pass
                        if parsed_t and (surface_terrain is None or parsed_t > surface_terrain * 2.5):
                            surface_terrain = parsed_t
                    if surface_terrasse is None and ("terrasse" in it_norm or "varangue" in it_norm):
                        m_tr = re.search(r"(\d+(?:[\.,]\d+)?)\s*m2", it_norm)
                        if m_tr:
                            try:
                                vt = float(m_tr.group(1).replace(",", "."))
                                if 1.0 <= vt <= 500.0:
                                    surface_terrasse = vt
                            except ValueError:
                                pass
                    if ("parking" in cat_norm or "garage" in cat_norm or "stationnement" in cat_norm):
                        m_pk = re.search(r"(\d+)", it_norm)
                        if m_pk:
                            p_v = int(m_pk.group(1))
                            if 1 <= p_v <= 20:
                                parkings = max(parkings or 0, p_v)
                    if charges is None and ("charge" in it_norm or "financ" in cat_norm):
                        m_ch = re.search(r"charges?[^0-9]{0,20}(\d[\d\s\.,]*)", it_norm)
                        if m_ch:
                            raw_cs = re.sub(r"[.,]\d{1,2}\s*$", "", m_ch.group(1).strip())
                            clean_cs = re.sub(r"[^\d]", "", raw_cs)
                            if clean_cs and 500 <= int(clean_cs) <= 150_000:
                                charges = int(clean_cs)
            if sdb_from_comp > 0:
                bathrooms = min(sdb_from_comp, 15)

        if not has_elevator and "ascenseur" in blob_norm and not re.search(r"\b(?:sans\s+ascenseur|pas\s+d['’]?ascenseur|ascenseur\s*:\s*non)\b", blob_norm):
            has_elevator = True
        if "ascenseur" in raw_facs:
            has_elevator = True

        # Extraction salles de bain / d'eau depuis le texte (chiffres + mots français : une/deux/trois salles d'eau)
        if bathrooms is None and prop_type not in (PropertyType.TERRAIN, PropertyType.DOCK):
            sdb_words_map = {"une": 1, "un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5}
            m_sdb = re.search(
                r"\b([1-9]|une|deux|trois|quatre|cinq)\s*(?:belles?\s+|grandes?\s+|petites?\s+)?(?:salles?\s*d\s*eau|salles?\s*de\s*bains?|sdb|sde)\b",
                blob_norm,
            )
            if m_sdb:
                tok = m_sdb.group(1)
                bathrooms = sdb_words_map.get(tok, int(tok) if tok.isdigit() else 1)
            elif re.search(r"\b(?:salle\s*d\s*eau|salle\s*de\s*bains?)\b", blob_norm):
                bathrooms = 1

        # Garde-fou cohérence rooms >= bedrooms
        if rooms is not None and bedrooms is not None and bedrooms > rooms:
            rooms = bedrooms + 1

        # Détails complémentaires JSON
        comp_details_json = None
        if getattr(raw, "complementary_details", None):
            try:
                comp_details_json = json.dumps(raw.complementary_details, ensure_ascii=False)
            except Exception:
                comp_details_json = None

        # Négociateur direct et contact téléphonique
        from src.processing.description_structurer import (
            structure_description,
            strip_technical_noise,
            is_valid_person_name,
            _clean_candidate_person_name,
        )
        cleaned_description_text = strip_technical_noise(raw_desc_in).strip()
        direct_negotiator = getattr(raw, "direct_negotiator", None)
        if direct_negotiator and not is_valid_person_name(_clean_candidate_person_name(direct_negotiator)):
            direct_negotiator = None
        direct_phone = getattr(raw, "direct_phone", None)
        if (not direct_negotiator or not direct_phone) and cleaned_description_text:
            # 1. Détection via le moteur universel de contacts NC
            try:
                struct = structure_description(cleaned_description_text)
                contacts = struct.get("direct_contacts", [])
                if contacts:
                    if not direct_negotiator and contacts[0].get("name") and is_valid_person_name(contacts[0]["name"]):
                        direct_negotiator = contacts[0]["name"]
                    if not direct_phone and contacts[0].get("phone"):
                        direct_phone = contacts[0]["phone"]
            except Exception:
                pass

            # 2. Fallback regex ciblée
            if not direct_negotiator or not direct_phone:
                m_neg = re.search(
                    r"(?:contactez|contacter|joindre|appelez|visites?,?\s*contactez)\s+([A-ZÀ-ÿ][a-zà-ÿ]+(?:\s+[A-ZÀ-ÿ][a-zà-ÿ]+)?)\s+(?:au\s+|au\s*:?\s*|\(?T[eé]l\.?\s*:?\s*|\(?)\s*([0-9]{2}[\.\s]?[0-9]{2}[\.\s]?[0-9]{2}|[0-9]{6})(?!\s*(?:F\b|XPF\b|CFP\b|€))",
                    cleaned_description_text,
                    re.IGNORECASE
                )
                if m_neg:
                    cand_name = _clean_candidate_person_name(m_neg.group(1))
                    if is_valid_person_name(cand_name):
                        if not direct_negotiator:
                            direct_negotiator = cand_name.title()
                    if not direct_phone:
                        raw_num = m_neg.group(2).strip()
                        num_clean = re.sub(r"[^\d]", "", raw_num)
                        direct_phone = f"+687 {num_clean[:2]}.{num_clean[2:4]}.{num_clean[4:]}" if len(num_clean) == 6 else f"+687 {raw_num}"

        return CleanedListing(
            id=unique_id,
            source=raw.source,
            source_id=raw.source_id,
            url=raw.url,
            title=raw.title.strip(),
            description=cleaned_description_text,
            transaction_type=trans_type,
            property_type=prop_type,
            commune=commune,
            quartier=quartier,
            price_xpf=price,
            charges_mensuelles_xpf=charges,
            surface_habitable_m2=surface_hab,
            surface_terrain_m2=surface_terrain,
            surface_terrasse_m2=surface_terrasse,
            rooms=rooms,
            bedrooms=bedrooms,
            bathrooms=bathrooms,
            parkings=parkings,
            parking_detail=parking_detail,
            etage=etage,
            has_elevator=has_elevator,
            has_sea_view=has_sea_view,
            has_pool=has_pool,
            has_air_conditioning=has_ac,
            is_secured=is_sec,
            is_furnished=is_furnished,
            agency_name=raw.agency_name,
            image_url=raw.image_url,
            images_json=raw.images_json,
            initial_price_xpf=price,
            published_at=raw.published_at,
            first_seen_at=raw.published_at or raw.extracted_at,
            is_active=True,
            complementary_details_json=comp_details_json,
            direct_negotiator=direct_negotiator,
            direct_phone=direct_phone,
        )
