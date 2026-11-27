import re
from typing import Optional
from src.models.listing import CleanedListing
from src.reference.geo import GeoReferential, get_geo_referential, normalize_text


class ListingEnricher:
    def __init__(self, geo_ref: Optional[GeoReferential] = None):
        self.geo_ref = geo_ref or get_geo_referential()

    def enrich(self, listing: CleanedListing) -> CleanedListing:
        """
        Enrichit l'annonce avec des attributs prédictifs (vue mer, piscine, climatisation, standing)
        sans jamais écraser les attributs déjà validés lors du nettoyage/scraping.
        """
        text = normalize_text(f"{listing.title} {listing.description}")

        # 1. Vue mer / lagon (prime de valeur majeure dans le Grand Nouméa)
        sea_view_regex = r"\b(vue\s*mer|vue\s*lagon|vue\s*baie|vue\s*imprenable\s*sur\s*la\s*mer|face\s*a\s*la\s*mer|front\s*de\s*mer|bord\s*de\s*mer|apercu\s*mer|acces\s*direct\s*plage)\b"
        listing.has_sea_view = bool(listing.has_sea_view) or bool(re.search(sea_view_regex, text))

        # 2. Piscine (exclut 'piscinable' et 'sans piscine')
        pool_regex = r"\b(piscine|bassin\s+de\s+nage|spa\s*pool|couloir\s*de\s*nage|jacuzzi)\b"
        has_pool_neg = bool(re.search(r"\b(sans\s+piscine|pas\s+de\s+piscine|piscine\s*:\s*non)\b", text))
        listing.has_pool = bool(listing.has_pool) or (bool(re.search(pool_regex, text)) and not has_pool_neg)

        # 3. Climatisation (confort thermique essentiel en climat tropical calédonien)
        ac_regex = r"\b(climatis\w*|climatiseur\w*|clims?|air\s+conditionne)\b"
        listing.has_air_conditioning = bool(listing.has_air_conditioning) or bool(re.search(ac_regex, text))

        # 4. Sécurité (critère de plus en plus surveillé post-2024)
        security_regex = r"\b(securis\w*|digicode|interphone|visiophone|gardien|alarme|volets\s*roulants|portail\s*electrique|portail\s*motorise|clotur\w*)\b"
        listing.is_secured = bool(listing.is_secured) or bool(re.search(security_regex, text))

        # 5. Standing estimé
        prestige_regex = r"\b(prestige|haut\s*standing|luxe|renove\w*\s*avec\s*gout|materiaux\s*nobles|kohu|deck\s*en\s*kohu|villa\s*d\s*architecte)\b"
        if re.search(prestige_regex, text):
            listing.standing_estime = "PREMIUM"
        elif listing.quartier:
            q_info = self.geo_ref.get_quartier_info(listing.commune, listing.quartier)
            if q_info:
                listing.standing_estime = q_info.get("standing", "INTERMEDIAIRE").upper()
            else:
                listing.standing_estime = listing.standing_estime or "STANDARD"
        else:
            listing.standing_estime = listing.standing_estime or "STANDARD"

        return listing
