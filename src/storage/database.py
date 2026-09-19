from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import duckdb
import pandas as pd

from src.config.settings import DB_PATH, PROCESSED_DATA_DIR
from src.models.listing import RawListing, CleanedListing


class PropertyDatabase:
    def __init__(self, db_path: Optional[Path] = None, read_only: bool = False):
        self.db_path = db_path or DB_PATH
        self.read_only = read_only
        # Assurer que le dossier parent existe
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.read_only and not self.db_path.exists():
            self._init_schema()
        elif not self.read_only:
            self._init_schema()

    def get_connection(self, read_only: Optional[bool] = None) -> duckdb.DuckDBPyConnection:
        ro = self.read_only if read_only is None else read_only
        return duckdb.connect(str(self.db_path), read_only=ro)

    def _init_schema(self):
        """Initialise les tables et vues analytiques DuckDB."""
        with self.get_connection(read_only=False) as con:
            # Table des annonces nettoyées
            con.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                id VARCHAR PRIMARY KEY,
                source VARCHAR,
                source_id VARCHAR,
                url VARCHAR,
                title VARCHAR,
                description VARCHAR,
                transaction_type VARCHAR,
                property_type VARCHAR,
                commune VARCHAR,
                quartier VARCHAR,
                price_xpf BIGINT,
                price_eur DOUBLE,
                charges_mensuelles_xpf INTEGER,
                surface_habitable_m2 DOUBLE,
                surface_terrain_m2 DOUBLE,
                surface_terrasse_m2 DOUBLE,
                prix_m2_habitable_xpf DOUBLE,
                prix_m2_habitable_eur DOUBLE,
                rooms INTEGER,
                bedrooms INTEGER,
                bathrooms INTEGER,
                parkings INTEGER,
                has_sea_view BOOLEAN,
                has_pool BOOLEAN,
                has_air_conditioning BOOLEAN,
                is_secured BOOLEAN,
                is_furnished BOOLEAN,
                standing_estime VARCHAR,
                agency_name VARCHAR,
                image_url VARCHAR,
                images_json VARCHAR,
                initial_price_xpf BIGINT,
                first_seen_at TIMESTAMP,
                last_price_change_at TIMESTAMP,
                published_at TIMESTAMP,
                scraped_at TIMESTAMP,
                is_active BOOLEAN
            );
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS images_json VARCHAR;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_furnished BOOLEAN;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS initial_price_xpf BIGINT;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS last_price_change_at TIMESTAMP;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS lat_precise DOUBLE;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS lon_precise DOUBLE;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS precision_score INTEGER;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS precision_level VARCHAR;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS precision_detail VARCHAR;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS photo_date_taken TIMESTAMP;
            ALTER TABLE listings ADD COLUMN IF NOT EXISTS photo_age_months INTEGER;

            -- Table dédiée à l'historique complet des variations de prix
            CREATE TABLE IF NOT EXISTS listing_price_history (
                id VARCHAR PRIMARY KEY,
                listing_id VARCHAR,
                price_xpf BIGINT,
                price_eur DOUBLE,
                event_type VARCHAR,
                price_change_xpf BIGINT,
                price_change_pct DOUBLE,
                recorded_at TIMESTAMP
            );
            """)

            # Vues analytiques
            con.execute("""
            CREATE OR REPLACE VIEW v_marche_par_quartier AS
            SELECT 
                commune,
                quartier,
                transaction_type,
                property_type,
                COUNT(*) AS total_annonces,
                ROUND(MEDIAN(price_xpf), 0) AS prix_median_xpf,
                ROUND(AVG(price_xpf), 0) AS prix_moyen_xpf,
                ROUND(MEDIAN(prix_m2_habitable_xpf), 0) AS prix_m2_median_xpf,
                ROUND(AVG(prix_m2_habitable_xpf), 0) AS prix_m2_moyen_xpf,
                ROUND(AVG(prix_m2_habitable_eur), 1) AS prix_m2_moyen_eur,
                ROUND(AVG(surface_habitable_m2), 1) AS surface_hab_moyenne,
                SUM(CASE WHEN has_sea_view THEN 1 ELSE 0 END) AS nb_vue_mer,
                SUM(CASE WHEN has_pool THEN 1 ELSE 0 END) AS nb_piscine
            FROM listings
            WHERE is_active = TRUE AND prix_m2_habitable_xpf IS NOT NULL
            GROUP BY commune, quartier, transaction_type, property_type;
            """)

            # Vue de valorisation par typologie de pièces (F2, F3, F4, etc.)
            con.execute("""
            CREATE OR REPLACE VIEW v_marche_par_typologie AS
            SELECT 
                commune,
                property_type,
                rooms,
                transaction_type,
                COUNT(*) AS total_annonces,
                ROUND(MEDIAN(price_xpf), 0) AS prix_median_xpf,
                ROUND(MEDIAN(prix_m2_habitable_xpf), 0) AS prix_m2_median_xpf,
                ROUND(AVG(surface_habitable_m2), 1) AS surface_moyenne
            FROM listings
            WHERE is_active = TRUE AND rooms IS NOT NULL
            GROUP BY commune, property_type, rooms, transaction_type;
            """)

    def upsert_listings(self, listings: List[CleanedListing]):
        """Insère ou met à jour une liste d'annonces nettoyées."""
        if not listings:
            return

        records = []
        for l in listings:
            records.append({
                "id": l.id,
                "source": l.source,
                "source_id": l.source_id,
                "url": l.url,
                "title": l.title,
                "description": l.description,
                "transaction_type": l.transaction_type.value,
                "property_type": l.property_type.value,
                "commune": l.commune.value,
                "quartier": l.quartier,
                "price_xpf": l.price_xpf,
                "price_eur": l.price_eur,
                "charges_mensuelles_xpf": l.charges_mensuelles_xpf,
                "surface_habitable_m2": l.surface_habitable_m2,
                "surface_terrain_m2": l.surface_terrain_m2,
                "surface_terrasse_m2": l.surface_terrasse_m2,
                "prix_m2_habitable_xpf": l.prix_m2_habitable_xpf,
                "prix_m2_habitable_eur": l.prix_m2_habitable_eur,
                "rooms": l.rooms,
                "bedrooms": l.bedrooms,
                "bathrooms": l.bathrooms,
                "parkings": l.parkings,
                "has_sea_view": l.has_sea_view,
                "has_pool": l.has_pool,
                "has_air_conditioning": l.has_air_conditioning,
                "is_secured": l.is_secured,
                "is_furnished": l.is_furnished,
                "standing_estime": l.standing_estime,
                "agency_name": l.agency_name,
                "image_url": getattr(l, "image_url", None),
                "images_json": getattr(l, "images_json", None),
                "initial_price_xpf": getattr(l, "initial_price_xpf", None) or l.price_xpf,
                "first_seen_at": getattr(l, "first_seen_at", None) or l.published_at or l.scraped_at,
                "last_price_change_at": getattr(l, "last_price_change_at", None),
                "published_at": l.published_at,
                "scraped_at": l.scraped_at,
                "is_active": l.is_active,
                "lat_precise": getattr(l, "lat_precise", None),
                "lon_precise": getattr(l, "lon_precise", None),
                "precision_score": getattr(l, "precision_score", None),
                "precision_level": getattr(l, "precision_level", None),
                "precision_detail": getattr(l, "precision_detail", None),
                "photo_date_taken": getattr(l, "photo_date_taken", None),
                "photo_age_months": getattr(l, "photo_age_months", None),
            })

        df = pd.DataFrame(records)

        with self.get_connection() as con:
            con.register("staging_df", df)

            # 1. Analyse des variations de prix par rapport aux données existantes
            existing_df = con.execute("""
                SELECT id, price_xpf, published_at, first_seen_at, initial_price_xpf 
                FROM listings 
                WHERE id IN (SELECT id FROM staging_df)
            """).df()
            existing_map = {row["id"]: row for _, row in existing_df.iterrows()}

            history_records = []
            for l in listings:
                ex = existing_map.get(l.id)
                rec_time = l.scraped_at or datetime.now(timezone.utc)
                if ex is None:
                    # Nouvelle annonce : événement initial dans l'historique
                    init_time = l.published_at or l.scraped_at or datetime.now(timezone.utc)
                    history_records.append({
                        "id": f"hist_{l.id}_{int(init_time.timestamp()) if init_time else 0}",
                        "listing_id": l.id,
                        "price_xpf": l.price_xpf,
                        "price_eur": l.price_eur,
                        "event_type": "INITIAL",
                        "price_change_xpf": 0,
                        "price_change_pct": 0.0,
                        "recorded_at": init_time,
                    })
                else:
                    old_p = ex.get("price_xpf")
                    if old_p is not None and not pd.isna(old_p) and int(old_p) != l.price_xpf:
                        diff_xpf = l.price_xpf - int(old_p)
                        diff_pct = round((diff_xpf / int(old_p)) * 100, 2)
                        ev_type = "PRICE_DROP" if diff_xpf < 0 else "PRICE_INCREASE"
                        history_records.append({
                            "id": f"hist_{l.id}_{int(rec_time.timestamp()) if rec_time else 0}",
                            "listing_id": l.id,
                            "price_xpf": l.price_xpf,
                            "price_eur": l.price_eur,
                            "event_type": ev_type,
                            "price_change_xpf": diff_xpf,
                            "price_change_pct": diff_pct,
                            "recorded_at": rec_time,
                        })

            if history_records:
                hist_df = pd.DataFrame(history_records)
                con.register("staging_hist_df", hist_df)
                con.execute("""
                INSERT INTO listing_price_history BY NAME
                SELECT * FROM staging_hist_df
                ON CONFLICT (id) DO NOTHING;
                """)

            # 2. Insertion avec dédoublonnage et préservation du prix d'origine
            con.execute("""
            INSERT INTO listings BY NAME
            SELECT * FROM staging_df
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                transaction_type = EXCLUDED.transaction_type,
                property_type = EXCLUDED.property_type,
                commune = EXCLUDED.commune,
                quartier = EXCLUDED.quartier,
                description = EXCLUDED.description,
                price_xpf = EXCLUDED.price_xpf,
                price_eur = EXCLUDED.price_eur,
                initial_price_xpf = COALESCE(listings.initial_price_xpf, EXCLUDED.initial_price_xpf, EXCLUDED.price_xpf),
                first_seen_at = COALESCE(listings.first_seen_at, EXCLUDED.first_seen_at, listings.scraped_at),
                published_at = COALESCE(listings.published_at, EXCLUDED.published_at),
                last_price_change_at = CASE 
                    WHEN listings.price_xpf != EXCLUDED.price_xpf THEN EXCLUDED.scraped_at 
                    ELSE listings.last_price_change_at 
                END,
                charges_mensuelles_xpf = EXCLUDED.charges_mensuelles_xpf,
                surface_habitable_m2 = EXCLUDED.surface_habitable_m2,
                surface_terrain_m2 = EXCLUDED.surface_terrain_m2,
                surface_terrasse_m2 = EXCLUDED.surface_terrasse_m2,
                prix_m2_habitable_xpf = EXCLUDED.prix_m2_habitable_xpf,
                prix_m2_habitable_eur = EXCLUDED.prix_m2_habitable_eur,
                rooms = EXCLUDED.rooms,
                bedrooms = EXCLUDED.bedrooms,
                bathrooms = EXCLUDED.bathrooms,
                parkings = EXCLUDED.parkings,
                has_sea_view = EXCLUDED.has_sea_view,
                has_pool = EXCLUDED.has_pool,
                has_air_conditioning = EXCLUDED.has_air_conditioning,
                is_secured = EXCLUDED.is_secured,
                is_furnished = EXCLUDED.is_furnished,
                standing_estime = EXCLUDED.standing_estime,
                agency_name = EXCLUDED.agency_name,
                image_url = COALESCE(EXCLUDED.image_url, listings.image_url),
                images_json = COALESCE(EXCLUDED.images_json, listings.images_json),
                scraped_at = EXCLUDED.scraped_at,
                is_active = EXCLUDED.is_active,
                lat_precise = COALESCE(EXCLUDED.lat_precise, listings.lat_precise),
                lon_precise = COALESCE(EXCLUDED.lon_precise, listings.lon_precise),
                precision_score = COALESCE(EXCLUDED.precision_score, listings.precision_score),
                precision_level = COALESCE(EXCLUDED.precision_level, listings.precision_level),
                precision_detail = COALESCE(EXCLUDED.precision_detail, listings.precision_detail),
                photo_date_taken = COALESCE(EXCLUDED.photo_date_taken, listings.photo_date_taken),
                photo_age_months = COALESCE(EXCLUDED.photo_age_months, listings.photo_age_months);
            """)

    def query(self, sql: str) -> pd.DataFrame:
        """Exécute une requête SQL personnalisée et renvoie un DataFrame."""
        with self.get_connection(read_only=True) as con:
            return con.execute(sql).df()

    def get_market_summary(self) -> pd.DataFrame:
        """Retourne la synthèse des prix par quartier et type d'opération."""
        with self.get_connection(read_only=True) as con:
            return con.execute("""
            SELECT * FROM v_marche_par_quartier 
            ORDER BY commune, transaction_type, total_annonces DESC
            """).df()
