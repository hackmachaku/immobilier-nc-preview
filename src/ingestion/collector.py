from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path

from src.config.settings import RAW_DATA_DIR
from src.models.listing import RawListing, CleanedListing
from src.processing.cleaner import ListingCleaner
from src.processing.enricher import ListingEnricher
from src.storage.database import PropertyDatabase
from src.utils.logger import get_logger

logger = get_logger("ingestion")


class IngestionPipeline:
    def __init__(self, db: Optional[PropertyDatabase] = None):
        self.db = db or PropertyDatabase()
        self.cleaner = ListingCleaner()
        self.enricher = ListingEnricher()
        RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

    def save_raw_batch(self, raw_listings: List[RawListing], batch_tag: str = "batch") -> Path:
        """Sauvegarde les annonces brutes dans un fichier JSONL d'archive (Staging / Bronze)."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = RAW_DATA_DIR / f"raw_listings_{batch_tag}_{timestamp}.jsonl"
        with open(filename, "w", encoding="utf-8") as f:
            for item in raw_listings:
                f.write(item.model_dump_json() + "\n")
        logger.info(f"Archive brute sauvegardée ({len(raw_listings)} annonces) : {filename.name}")
        return filename

    def process_and_store(
        self,
        raw_listings: List[RawListing],
        batch_tag: str = "batch",
        auto_quality_control: bool = True,
    ) -> Dict[str, Any]:
        """
        Exécute le cycle complet :
        1. Sauvegarde Staging (Raw JSONL)
        2. Nettoyage & Normalisation (Pydantic / Regex)
        3. Enrichissement (Vue mer, piscine, clim, standing)
        4. Ingestion / Upsert dans DuckDB
        5. Contrôle Qualité Automatique Post-Import (assainissement, cache v4.1, Quality Gate)
        """
        logger.info(f"Démarrage du traitement par lot '{batch_tag}' : {len(raw_listings)} annonces brutes reçues.")
        # 1. Sauvegarde Raw
        raw_file = self.save_raw_batch(raw_listings, batch_tag)

        cleaned_list: List[CleanedListing] = []
        rejected_count = 0

        # 2. Nettoyage et 3. Enrichissement
        for raw in raw_listings:
            cleaned = self.cleaner.clean(raw)
            if cleaned:
                enriched = self.enricher.enrich(cleaned)
                cleaned_list.append(enriched)
            else:
                rejected_count += 1
                logger.warning(f"Annonce rejetée (données insuffisantes) : ID={raw.source_id}, source={raw.source}")

        # 4. Stockage analytique
        self.db.upsert_listings(cleaned_list)
        logger.info(
            f"Traitement terminé avec succès : {len(cleaned_list)} annonces nettoyées et enrichies, "
            f"{rejected_count} rejetées, upsert DuckDB validé."
        )

        # 5. Contrôle Qualité Automatique Systématique sur les annonces importées
        quality_control = None
        if auto_quality_control and cleaned_list:
            try:
                from src.quality.post_import_controller import run_post_import_control
                stored_ids = [c.id for c in cleaned_list]
                quality_control = run_post_import_control(
                    db=self.db,
                    listing_ids=stored_ids,
                    batch_tag=batch_tag,
                    run_geo=False,
                    rebuild_ai_cache=True,
                    run_canary=False,
                    save_report=True,
                )
            except Exception as exc:
                logger.warning(f"Contrôle qualité automatique post-import ignoré : {exc}")

        return {
            "total_raw": len(raw_listings),
            "stored_cleaned": len(cleaned_list),
            "rejected": rejected_count,
            "raw_archive_file": str(raw_file),
            "quality_control": quality_control,
        }
