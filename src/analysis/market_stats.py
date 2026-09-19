"""
Module d'analyses statistiques avancées du marché immobilier du Grand Nouméa.
Calcul des distributions de prix, percentiles et détection des opportunités d'achat.
"""

from typing import Dict, Any, List, Optional
import pandas as pd

from src.storage.database import PropertyDatabase
from src.valuation.avm import HedonicValuationEngine, PropertyValuationRequest
from src.models.listing import Commune, PropertyType


class MarketAnalytics:
    def __init__(self, db: Optional[PropertyDatabase] = None):
        self.db = db or PropertyDatabase()
        self.engine = HedonicValuationEngine()

    def get_price_distributions(self) -> pd.DataFrame:
        """
        Calcule les percentiles (P25, Médiane, P75) et l'écart interquartile (IQR)
        des prix au m² par commune et type de transaction.
        """
        query = """
        SELECT 
            commune,
            transaction_type,
            property_type,
            COUNT(*) as nb_biens,
            ROUND(QUANTILE_CONT(prix_m2_habitable_xpf, 0.25), 0) as p25_m2_xpf,
            ROUND(MEDIAN(prix_m2_habitable_xpf), 0) as median_m2_xpf,
            ROUND(QUANTILE_CONT(prix_m2_habitable_xpf, 0.75), 0) as p75_m2_xpf,
            ROUND(QUANTILE_CONT(prix_m2_habitable_xpf, 0.75) - QUANTILE_CONT(prix_m2_habitable_xpf, 0.25), 0) as iqr_m2_xpf,
            ROUND(AVG(prix_m2_habitable_eur), 0) as avg_m2_eur
        FROM listings
        WHERE is_active = TRUE AND prix_m2_habitable_xpf IS NOT NULL
        GROUP BY commune, transaction_type, property_type
        ORDER BY commune, transaction_type, nb_biens DESC;
        """
        return self.db.query(query)

    def detect_market_opportunities(self, min_discount_pct: float = 8.0) -> List[Dict[str, Any]]:
        """
        Détecte les annonces de vente dont le prix affiché est inférieur
        à la valeur vénale estimée par le modèle AVM (décote d'opportunité).
        """
        query = """
        SELECT * FROM listings 
        WHERE transaction_type = 'VENTE' AND is_active = TRUE AND surface_habitable_m2 > 0;
        """
        df = self.db.query(query)
        opportunities: List[Dict[str, Any]] = []

        for _, row in df.iterrows():
            commune_enum = Commune[row["commune"]] if row["commune"] in Commune.__members__ else Commune.AUTRE
            prop_type_enum = PropertyType[row["property_type"]] if row["property_type"] in PropertyType.__members__ else PropertyType.APPARTEMENT

            # Évaluation AVM du bien
            req = PropertyValuationRequest(
                commune=commune_enum,
                quartier=row["quartier"] if pd.notna(row["quartier"]) else None,
                property_type=prop_type_enum,
                surface_habitable_m2=float(row["surface_habitable_m2"]),
                surface_varangue_m2=float(row["surface_terrasse_m2"]) if pd.notna(row["surface_terrasse_m2"]) else 0.0,
                surface_terrain_m2=float(row["surface_terrain_m2"]) if pd.notna(row["surface_terrain_m2"]) else 0.0,
                vue_mer="BELLE_VUE" if row["has_sea_view"] else "AUCUNE",
                piscine="MACONNEE_LAGON" if row["has_pool"] else "AUCUNE",
                has_air_conditioning=bool(row["has_air_conditioning"]),
                is_secured=bool(row["is_secured"]),
            )
            res = self.engine.evaluate(req)

            prix_affiche = int(row["price_xpf"])
            valeur_estimee = res.valeur_centrale_xpf

            if valeur_estimee > prix_affiche:
                decote_pct = round(((valeur_estimee - prix_affiche) / valeur_estimee) * 100, 1)
                gain_potentiel_xpf = valeur_estimee - prix_affiche

                if decote_pct >= min_discount_pct:
                    opportunities.append({
                        "id": row["id"],
                        "titre": row["title"],
                        "commune": row["commune"],
                        "quartier": row["quartier"] if pd.notna(row["quartier"]) else "Non spécifié",
                        "type": row["property_type"],
                        "surface_m2": row["surface_habitable_m2"],
                        "prix_affiche_xpf": prix_affiche,
                        "valeur_estimee_xpf": valeur_estimee,
                        "decote_pct": decote_pct,
                        "gain_potentiel_xpf": gain_potentiel_xpf,
                        "rendement_locatif_estime": res.rendement_locatif_brut_pct,
                        "url": row["url"],
                    })

        # Trier par décote décroissante
        opportunities.sort(key=lambda x: x["decote_pct"], reverse=True)
        return opportunities
