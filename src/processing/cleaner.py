import re
import json
from typing import Optional, Tuple
from src.models.listing import RawListing, CleanedListing, TransactionType, PropertyType, Commune
from src.reference.geo import GeoReferential, get_geo_referential, normalize_text


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
        1. Surface habitable (m²)
        2. Surface terrain (m²) - gère les 'ares' calédoniens (1 are = 100 m²)
        3. Surface terrasse / varangue (m²)
        """
        surface_hab: Optional[float] = None
        surface_terrain: Optional[float] = None
        surface_terrasse: Optional[float] = None

        blob = f"{raw_surface or ''} {text_fallback}".lower()

        # 1. Surface Habitable
        hab_match = re.search(r"(\d+(?:[,\.]\d+)?)\s*(?:m²|m2|metres\s*carres?)\s*(?:hab(?:itables?)?|int(?:erieurs?)?)?", blob)
        if raw_surface:
            # Si un champ surface dédié est fourni
            num_match = re.search(r"(\d+(?:[,\.]\d+)?)", raw_surface)
            if num_match:
                try:
                    surface_hab = float(num_match.group(1).replace(",", "."))
                except ValueError:
                    pass
        elif hab_match:
            try:
                surface_hab = float(hab_match.group(1).replace(",", "."))
            except ValueError:
                pass

        # 2. Surface Terrain (ex: "terrain de 12 ares", "terrain 850 m²", "parcelle de 6 ares 50")
        # En NC, les surfaces de terrain sont très souvent exprimées en ares
        ares_match = re.search(r"(\d+(?:[,\.]\d+)?)\s*ares?(?:\s*(\d+))?", blob)
        if ares_match:
            try:
                ares_val = float(ares_match.group(1).replace(",", "."))
                centiares = float(ares_match.group(2)) if ares_match.group(2) else 0.0
                surface_terrain = (ares_val * 100.0) + centiares
            except ValueError:
                pass
        else:
            terrain_match = re.search(
                r"(?:terrain|parcelle)\s*(?:de|d['’])?\s*(\d+(?:[\s\.,]\d+)?)\s*(?:m²|m2)",
                blob,
            )
            if terrain_match:
                try:
                    raw_t = terrain_match.group(1).strip()
                    # Gère les décimales ("650,5" / "650.5") vs séparateurs de milliers ("1 200" / "1.200")
                    if re.match(r"^\d+[\.,]\d{1,2}$", raw_t):
                        surface_terrain = float(raw_t.replace(",", "."))
                    else:
                        cleaned_t = re.sub(r"[^\d]", "", raw_t)
                        if cleaned_t:
                            surface_terrain = float(cleaned_t)
                except ValueError:
                    pass

        # 3. Surface Terrasse / Varangue / Deck (terme local calédonien)
        terrasse_match = re.search(r"(?:terrasse|varangue|deck)\s*(?:couverte)?\s*(?:de)?\s*(\d+(?:[,\.]\d+)?)\s*(?:m²|m2)", blob)
        if terrasse_match:
            try:
                surface_terrasse = float(terrasse_match.group(1).replace(",", "."))
            except ValueError:
                pass

        if surface_hab is not None and surface_hab < 0:
            surface_hab = None
        if surface_terrain is not None and surface_terrain < 0:
            surface_terrain = None
        if surface_terrasse is not None and surface_terrasse < 0:
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
        if re.search(r"\b(non\s*meubl[eé]e?s?|non-meubl[eé]e?s?|lou[eé]\s*vide|logement\s*vide|appartement\s*vide|villa\s*vide|maison\s*vide|bail\s*vide|non\s*équip[eé]e?s?)\b", blob):
            return False

        # 3. Détection explicite meublé
        if re.search(r"\b(meubl[eé]e?s?|enti[eè]rement\s*meubl[eé]e?s?|tout\s*équip[eé]e?s?|semi[- ]meubl[eé]e?s?|équipé\s*et\s*meubl[eé]e?s?)\b", blob):
            # Exclusion des faux-positifs de home staging virtuel (ex: "images meublées par IA")
            if re.search(r"(?:images?|photos?|projections?|virtuel(?:le)?s?)\s*meubl[eé]es?", blob) and not re.search(r"\b(bien|logement|appartement|villa|f[1-9]|studio)\s*(?:enti[eè]rement\s*)?meubl[eé]", blob):
                return None
            return True

        return None

    def clean(self, raw: RawListing) -> Optional[CleanedListing]:
        """Transforme une RawListing en CleanedListing validée et exploitable."""
        price = self.parse_price(raw.raw_price, f"{raw.title} {raw.description}")
        if price is None:
            # Une annonce sans prix exploitable est ignorée pour les calculs d'estimation
            return None

        surface_hab, surface_terrain, surface_terrasse = self.parse_surface(
            raw.raw_surface, f"{raw.title} {raw.description}"
        )
        prop_type = self.detect_property_type(raw.property_type_declared, raw.title, raw.description or "")
        trans_type = self.detect_transaction_type(
            raw.transaction_type_declared,
            raw.title,
            raw.description or "",
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
            rooms, bedrooms = self.parse_rooms(raw.raw_rooms, f"{raw.title} {raw.description}")

        # Localisation hiérarchique :
        # 1. Vérifier si le titre déclare un quartier précis (ex: "à Nouméa (P.k. 6)")
        clean_desc = re.sub(r'(?:contact|tél|tel|email|mail|agent|notre agence|retrouvez-nous)\s*:?.*', '', raw.description or '', flags=re.IGNORECASE)
        commune, quartier, _ = self.geo_ref.find_location(raw.title or "")

        # 2. Si le titre n'a pas de quartier précis, analyser la localisation déclarée puis le descriptif
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

        # Attributs qualitatifs (avec protection contre les faux positifs : piscinable, sans ascenseur, freebox...)
        raw_facs = getattr(raw, "facilities", None) or []
        blob_features = f"{raw.title} {raw.description or ''}".lower()
        blob_norm = normalize_text(blob_features)

        has_sea_view = bool(re.search(r"\b(vue\s*mer|vue\s*lagon|vue\s*baie|front\s*de\s*mer|bord\s*de\s*mer|les\s*pieds\s*dans\s*l['’]?eau|apercu\s*mer)\b", blob_norm))
        if "vue_mer" in raw_facs or "bord_de_mer" in raw_facs:
            has_sea_view = True

        has_pool = bool(
            re.search(r"\b(piscine|bassin\s+de\s+nage|couloir\s+de\s+nage|spa\s+pool)\b", blob_norm)
            and not re.search(r"\b(sans\s+piscine|pas\s+de\s+piscine|piscine\s*:\s*non)\b", blob_norm)
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
            description=raw.description or "",
            property_type=prop_type,
            transaction_type=trans_type,
        )

        # Extraction des charges mensuelles (avec retrait des décimales .00 et exclusion de 'hors charges')
        charges: Optional[int] = None
        if getattr(raw, "raw_charges", None):
            raw_c_str = re.sub(r"[.,]\d{1,2}\s*$", "", str(raw.raw_charges).strip())
            clean_c = re.sub(r"[^\d]", "", raw_c_str)
            if clean_c and 500 <= int(clean_c) <= 150_000:
                charges = int(clean_c)
        elif raw.description:
            m_chg = re.search(
                r"(?<!hors\s)(?<!sans\s)(?<!toute\s)(?<!aucune\s)\bcharges?\s*(?:mensuelles?|de\s+copropri[eé]t[eé]|copro)\s*[:=\s]?\s*(\d{1,3}(?:[\s.]?\d{3})*|\d{3,6})(?:[.,]\d{1,2})?\s*(?:f\s*cfp|xpf|f\b|cfp|/mois)?",
                raw.description,
                re.IGNORECASE
            )
            if m_chg:
                clean_c = re.sub(r"[^\d]", "", m_chg.group(1))
                if clean_c and 500 <= int(clean_c) <= 150_000:
                    charges = int(clean_c)

        # Surface terrasse fallback
        if surface_terrasse is None and getattr(raw, "raw_terrace", None):
            m_ter = re.search(r"(\d+(?:[\.,]\d+)?)", str(raw.raw_terrace))
            if m_ter:
                try:
                    surface_terrasse = float(m_ter.group(1).replace(",", "."))
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

        m_park_num = re.search(
            r"\b([1-9]|1[0-9]|20)\s*(?:places?\s*(?:de\s*)?(?:parking|stationnement|voiture|couvertes?|privatives?|exterieures?|interieures?|aeriennes?)|parkings?|carports?|garages?|box\s*(?:fermes?)?|stationnements?)\b",
            blob_park_clean,
        )
        if m_park_num:
            try:
                parkings = int(m_park_num.group(1))
            except ValueError:
                pass

        # Étage et Ascenseur (exclusion stricte de "sans ascenseur")
        etage: Optional[str] = getattr(raw, "raw_floor", None)
        if not etage:
            m_et = re.search(
                r"\b(dernier\s+[eé]tage|\d+(?:[eè]me|er|e)\s+[eé]tage|au\s+\d+(?:[eè]me|er|e)\b|rdc|rdj|rez[- ]de[- ]chauss[eé]e|rez[- ]de[- ]jardin|r\+[1-9])\b",
                blob_features,
            )
            if m_et:
                etage = m_et.group(1).capitalize()

        has_elevator = False
        bathrooms: Optional[int] = None
        comp_dict = getattr(raw, "complementary_details", None)
        if isinstance(comp_dict, dict):
            for cat, items in comp_dict.items():
                if not isinstance(items, list):
                    continue
                cat_norm = normalize_text(str(cat))
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
                    if bathrooms is None and any(w in it_norm for w in ("salle d'eau", "salle d eau", "salle de bain", "salles de bain", "sdb", "sde")):
                        m_b = re.search(r"(\d+)", it_norm)
                        if m_b:
                            b_val = int(m_b.group(1))
                            if 1 <= b_val <= 15:
                                bathrooms = b_val
                    if surface_terrain is None and ("terrain" in it_norm or "parcelle" in it_norm):
                        m_ta = re.search(r"(\d+(?:[\.,]\d+)?)\s*ares?", it_norm)
                        m_tm = re.search(r"(\d+(?:[\s\.,]\d+)?)\s*m2", it_norm)
                        if m_ta:
                            try:
                                surface_terrain = float(m_ta.group(1).replace(",", ".")) * 100.0
                            except ValueError:
                                pass
                        elif m_tm:
                            raw_t = m_tm.group(1).strip()
                            try:
                                surface_terrain = float(raw_t.replace(",", ".")) if re.match(r"^\d+[\.,]\d{1,2}$", raw_t) else float(re.sub(r"[^\d]", "", raw_t))
                            except ValueError:
                                pass
                    if surface_terrasse is None and ("terrasse" in it_norm or "varangue" in it_norm):
                        m_tr = re.search(r"(\d+(?:[\.,]\d+)?)\s*m2", it_norm)
                        if m_tr:
                            try:
                                surface_terrasse = float(m_tr.group(1).replace(",", "."))
                            except ValueError:
                                pass
                    if parkings is None and ("parking" in cat_norm or "garage" in cat_norm or "stationnement" in cat_norm):
                        m_pk = re.search(r"(\d+)", it_norm)
                        if m_pk:
                            p_v = int(m_pk.group(1))
                            if 1 <= p_v <= 20:
                                parkings = p_v
                    if charges is None and ("charge" in it_norm or "financ" in cat_norm):
                        m_ch = re.search(r"charges?[^0-9]{0,20}(\d[\d\s\.,]*)", it_norm)
                        if m_ch:
                            raw_cs = re.sub(r"[.,]\d{1,2}\s*$", "", m_ch.group(1).strip())
                            clean_cs = re.sub(r"[^\d]", "", raw_cs)
                            if clean_cs and 500 <= int(clean_cs) <= 150_000:
                                charges = int(clean_cs)

        if not has_elevator and "ascenseur" in blob_norm and not re.search(r"\b(?:sans\s+ascenseur|pas\s+d['’]?ascenseur|ascenseur\s*:\s*non)\b", blob_norm):
            has_elevator = True
        if "ascenseur" in raw_facs:
            has_elevator = True

        # Extraction salles de bain depuis le texte si non trouvé dans complementary_details
        if bathrooms is None and prop_type not in (PropertyType.TERRAIN, PropertyType.DOCK):
            m_sdb = re.search(r"\b([1-9])\s*(?:salles?\s*d['’]eau|salles?\s*de\s*bains?|sdb|sde)\b", blob_norm)
            if m_sdb:
                bathrooms = int(m_sdb.group(1))

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
        cleaned_description_text = strip_technical_noise(raw.description or "").strip()
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
