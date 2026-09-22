#!/usr/bin/env python3
"""
Outil CLI Sentinel - Analyse & Structuration IA par Lot (Batch Enrichment).
Permet d'analyser automatiquement les annonces non enrichies avec l'IA locale (Ollama / Qwen 2.5),
d'extraire les contacts de négociateurs et de structurer les rubriques dans DuckDB.

Usage:
    python run_ai_batch.py --limit 10
    python run_ai_batch.py --limit 50 --model qwen2.5:1.5b
    python run_ai_batch.py --all
    python run_ai_batch.py --status
"""

import sys
import time
import argparse
from pathlib import Path

# Fix Windows console encoding
sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from src.processing.ai_enricher import (
    batch_enrich_listings,
    get_ai_cache_stats,
    check_ai_status,
    get_ai_config
)


def print_banner():
    print("=" * 70)
    print("🤖 SENTINEL IMMO NC — ANALYSE & ENRICHISSEMENT SÉMANTIQUE PAR LOT")
    print("=" * 70)


def progress_tracker(current: int, total: int, title: str):
    pct = int((current / total) * 100) if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * current // total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    clean_title = (title[:35] + '...') if len(title) > 35 else title
    print(f"\r[{bar}] {current}/{total} ({pct}%) | {clean_title:<38}", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Enrichissement IA par lot des annonces immobilières NC.")
    parser.add_argument("--limit", type=int, default=10, help="Nombre maximal d'annonces à analyser (défaut: 10).")
    parser.add_argument("--all", action="store_true", help="Traiter l'ensemble des annonces non encore analysées.")
    parser.add_argument("--force-all", action="store_true", help="Forcer le recalcul même si l'annonce est déjà en cache.")
    parser.add_argument("--model", type=str, default=None, help="Modèle Ollama à utiliser (ex: qwen2.5:1.5b ou qwen2.5:3b).")
    parser.add_argument("--status", action="store_true", help="Affiche uniquement l'état de la connexion IA et du cache.")

    args = parser.parse_args()
    print_banner()

    status = check_ai_status()
    cfg = get_ai_config()
    active_model = args.model or cfg.get("model", "qwen2.5:1.5b")

    print(f"• Statut Moteur IA : {status.get('status').upper()} ({status.get('ping_ms')} ms)")
    print(f"• Modèle Sélectionné : {active_model}")
    print(f"• Modèles Installés : {', '.join(status.get('installed_models', []))}")
    print(f"• Cache Actuel : {status.get('cache', {}).get('cached_count', 0)} annonces déjà enrichies en DuckDB")

    if args.status:
        print("\nMode statut terminé.")
        return

    target_limit = 999999 if args.all else args.limit
    only_missing = not args.force_all

    print(f"\n🚀 Démarrage du lot : {target_limit if not args.all else 'TOUT LE CATALOGUE'} annonces")
    print(f"• Filtre : {'Uniquement les annonces non enrichies' if only_missing else 'Toutes les annonces'}")
    print("-" * 70)

    t0 = time.time()
    res = batch_enrich_listings(
        limit=target_limit,
        only_missing=only_missing,
        custom_model=active_model,
        progress_callback=progress_tracker
    )
    total_time = time.time() - t0

    print("\n" + "=" * 70)
    print("✅ TRAITEMENT PAR LOT TERMINÉ AVEC SUCCÈS")
    print("=" * 70)
    processed = res.get("processed_count", 0)
    print(f"• Annonces traitées : {processed}")
    print(f"• Temps total : {total_time:.1f} secondes ({total_time / processed:.2f} s/annonce)" if processed > 0 else "• Aucune annonce à traiter.")

    results = res.get("results", [])
    if results:
        contacts_total = sum(r.get("contacts_found", 0) for r in results)
        sections_total = sum(r.get("sections_found", 0) for r in results)
        print(f"• Contacts négociateurs isolés : {contacts_total}")
        print(f"• Rubriques structurées générées : {sections_total}")
        print("\nExemples récents traités :")
        for r in results[:5]:
            print(f"  - [{r.get('id')}] {r.get('title')[:45]} → {r.get('contacts_found')} contact(s), {r.get('sections_found')} rubriques ({r.get('latency_ms')} ms)")

    print("\n💡 Les données sont immédiatement actives dans la base DuckDB et visibles sur le tableau de bord.")


if __name__ == "__main__":
    main()
