import re
from typing import Optional, Tuple
from src.models.listing import RawListing, CleanedListing, TransactionType, PropertyType, Commune
from src.reference.geo import GeoReferential


class ListingCleaner:
    def __init__(self, geo_ref: Optional[GeoReferential] = None):
        self.geo_ref = geo_ref or GeoReferential()

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
            terrain_match = re.search(r"terrain\s*(?:de)?\s*(\d+(?:[\s\.,]\d+)?)\s*(?:m²|m2)", blob)
            if terrain_match:
                try:
                    cleaned_t = re.sub(r"[^\d]", "", terrain_match.group(1))
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
        """Identifie le type de bien immobilier."""
        blob = f"{declared or ''} {title} {description}".lower()

        if re.search(r"\b(villa|maison|propriete|dock\s*habitable)\b", blob):
            return PropertyType.MAISON_VILLA
        if re.search(r"\b(appartement|studio|duplex|triplex|attique|loft)\b", blob):
            return PropertyType.APPARTEMENT
        if re.search(r"\b(terrain|parcelle|lotissement)\b", blob):
            return PropertyType.TERRAIN
        if re.search(r"\b(immeuble|murs|batiment)\b", blob):
            return PropertyType.IMMEUBLE
        if re.search(r"\b(dock|entrepot|hangar)\b", blob):
            return PropertyType.DOCK
        if re.search(r"\b(local|bureau|commerce|boutique)\b", blob):
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

        # Les biens non résidentiels (terrains, docks, locaux professionnels) n'ont pas de pièces d'habitation
        if prop_type in (PropertyType.TERRAIN, PropertyType.DOCK, PropertyType.LOCAL_COMMERCIAL, PropertyType.IMMEUBLE):
            rooms, bedrooms = None, None
        else:
            rooms, bedrooms = self.parse_rooms(raw.raw_rooms, f"{raw.title} {raw.description}")

        # Estimation de la surface habitable si non déclarée mais typologie F1/F2/F3... connue (barème standard NC)
        if surface_hab is None and prop_type in (PropertyType.APPARTEMENT, PropertyType.MAISON_VILLA) and rooms:
            standard_surfaces = {1: 30.0, 2: 50.0, 3: 75.0, 4: 100.0, 5: 135.0, 6: 170.0}
            surface_hab = standard_surfaces.get(rooms, float(rooms * 25.0))

        # Localisation hiérarchique :
        # 1. Vérifier si le titre déclare un quartier précis (ex: "à Nouméa (P.k. 6)")
        clean_desc = re.sub(r'(?:contact|tél|tel|email|mail|agent|notre agence|retrouvez-nous)\s*:?.*', '', raw.description or '', flags=re.IGNORECASE)
        commune, quartier, _ = self.geo_ref.find_location(raw.title or "")

        # 2. Si le titre n'a pas de quartier précis (ex: titre générique "Maison F4"),
        # analyser conjointement la localisation déclarée et le descriptif nettoyé
        if not quartier:
            blob_loc = f"{raw.raw_location or ''} {clean_desc}".strip()
            c_blob, q_blob, _ = self.geo_ref.find_location(blob_loc)
            if q_blob:
                quartier = q_blob
                commune = c_blob
            elif commune == Commune.AUTRE and c_blob != Commune.AUTRE:
                commune = c_blob

        unique_id = f"{raw.source.lower()}_{raw.source_id}"

        # Attributs qualitatifs
        raw_facs = getattr(raw, "facilities", None) or []
        blob_features = f"{raw.title} {raw.description or ''}".lower()

        has_sea_view = bool(re.search(r"\b(vue\s*mer|vue\s*lagon|front\s*de\s*mer|bord\s*de\s*mer|les\s*pieds\s*dans\s*l'eau)\b", blob_features))
        if "vue_mer" in raw_facs or "bord_de_mer" in raw_facs:
            has_sea_view = True

        has_pool = bool(re.search(r"\b(piscine|bassin)\b", blob_features))
        if "piscine" in raw_facs or "piscine_commune" in raw_facs:
            has_pool = True

        has_ac = bool(re.search(r"\b(climatis[eé]|clim\b)", blob_features))
        if "climatisation" in raw_facs:
            has_ac = True

        is_sec = bool(re.search(r"\b(s[eé]curis[eé]|gardien|digicode|interphone)\b", blob_features))
        if "securite" in raw_facs:
            is_sec = True

        is_furnished = self.parse_furnished(
            facilities=raw_facs,
            title=raw.title,
            description=raw.description or "",
            property_type=prop_type,
            transaction_type=trans_type,
        )

        return CleanedListing(
            id=unique_id,
            source=raw.source,
            source_id=raw.source_id,
            url=raw.url,
            title=raw.title.strip(),
            description=raw.description.strip() if raw.description else "",
            transaction_type=trans_type,
            property_type=prop_type,
            commune=commune,
            quartier=quartier,
            price_xpf=price,
            surface_habitable_m2=surface_hab,
            surface_terrain_m2=surface_terrain,
            surface_terrasse_m2=surface_terrasse,
            rooms=rooms,
            bedrooms=bedrooms,
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
        )
