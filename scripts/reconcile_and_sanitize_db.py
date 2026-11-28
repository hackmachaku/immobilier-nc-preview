"""
Script de réconciliation live complète + contrôle qualité automatique post-import.
Réutilise le contrôleur unifié `src.quality.post_import_controller` (zéro duplication).
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.storage.database import PropertyDatabase
from src.ingestion.live_scraper import LiveNCScraper
from src.quality.post_import_controller import (
    run_post_import_control,
    sanitize_duckdb_active_listings,
    rebuild_ai_v4_cache,
)


def main():
    t0 = time.time()
    db = PropertyDatabase()
    scraper = LiveNCScraper(db=db)

    print("=== ETAPE 1/2 : Réconciliation live sur les 4 portails (purge 404 + hydratation + photos) ===")
    rec_stats = scraper.reconcile_active_listings(max_workers=16, hydrate_immonc_stubs=True)
    print(json.dumps({k: v for k, v in rec_stats.items() if k != "etl"}, ensure_ascii=False, indent=2))

    print("\n=== ETAPE 2/2 : Contrôle Qualité Automatique Post-Import (Assainissement + Géo + v4.1 + Canari + Quality Gate) ===")
    qc = run_post_import_control(
        db=db,
        listing_ids=None,
        batch_tag="full_live_reconciliation",
        run_geo=True,
        rebuild_ai_cache=True,
        run_canary=True,
        canary_per_portal=5,
        save_report=True,
    )
    print(json.dumps(qc, ensure_ascii=False, indent=2))
    print(f"\n=== TERMINÉ EN {time.time() - t0:.1f}s | Quality Gate: {qc['gate_status']} ===")


if __name__ == "__main__":
    main()
