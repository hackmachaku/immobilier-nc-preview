import argparse
import sys
from pathlib import Path
from typing import Optional

# Assurer l'encodage UTF-8 sur Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.ingestion.collector import IngestionPipeline
from src.ingestion.sample_generator import get_pilot_dataset
from src.storage.database import PropertyDatabase
from src.models.listing import Commune, PropertyType
from src.valuation.avm import HedonicValuationEngine, PropertyValuationRequest
from src.valuation.explainer import ValuationExplainer
from src.analysis.market_stats import MarketAnalytics
from src.analysis.geo_map import GrandNoumeaMapGenerator
from src.investment.models import InvestmentRequest
from src.investment.calculator import InvestmentCalculator
from src.investment.report import InvestmentReporter
from src.utils.logger import setup_logger, get_logger
from src.utils.log_analyzer import PipelineAuditor

logger = setup_logger("pipeline_main")



def display_market_report(db: PropertyDatabase):
    console = Console()
    console.print("\n[bold green]=== ANALYSE DU MARCHE IMMOBILIER DU GRAND NOUMEA ===[/bold green]\n")

    summary_df = db.get_market_summary()
    if summary_df.empty:
        console.print("[yellow]Aucune donnée d'annonce disponible en base.[/yellow]")
        return

    table = Table(title="Indicateurs de Prix au m2 et Stocks par Commune & Quartier")
    table.add_column("Commune", style="cyan", justify="left")
    table.add_column("Quartier", style="magenta", justify="left")
    table.add_column("Operation", style="yellow", justify="center")
    table.add_column("Type", style="blue", justify="center")
    table.add_column("Nb Annonces", justify="right")
    table.add_column("Prix Median (XPF)", justify="right")
    table.add_column("Prix/m2 Median (XPF)", justify="right", style="bold green")
    table.add_column("Prix/m2 Moyen (EUR)", justify="right")
    table.add_column("Vue Mer", justify="center")
    table.add_column("Piscine", justify="center")

    for _, row in summary_df.iterrows():
        table.add_row(
            str(row["commune"]),
            str(row["quartier"]) if row["quartier"] else "-",
            str(row["transaction_type"]),
            str(row["property_type"]),
            str(row["total_annonces"]),
            f"{int(row['prix_median_xpf']):,} F".replace(",", " "),
            f"{int(row['prix_m2_median_xpf']):,} F/m2".replace(",", " ") if row["prix_m2_median_xpf"] else "-",
            f"{int(row['prix_m2_moyen_eur'])} EUR/m2" if row["prix_m2_moyen_eur"] else "-",
            "Oui" if row["nb_vue_mer"] > 0 else "Non",
            "Oui" if row["nb_piscine"] > 0 else "Non",
        )

    console.print(table)


def display_market_statistics(db: PropertyDatabase):
    console = Console()
    analytics = MarketAnalytics(db)

    console.print("\n[bold green]=== ANALYSE STATISTIQUE DE DISTRIBUTION (PERCENTILES) ===[/bold green]\n")
    dist_df = analytics.get_price_distributions()

    if not dist_df.empty:
        t_dist = Table(title="Distribution des Prix/m2 (P25 - Mediane - P75 - Ecart Interquartile)")
        t_dist.add_column("Commune", style="cyan")
        t_dist.add_column("Operation", style="yellow")
        t_dist.add_column("Type", style="blue")
        t_dist.add_column("Biens", justify="right")
        t_dist.add_column("P25 (Bas de marche)", justify="right")
        t_dist.add_column("Mediane", justify="right", style="bold green")
        t_dist.add_column("P75 (Haut de marche)", justify="right")
        t_dist.add_column("Dispersion (IQR)", justify="right", style="magenta")

        for _, row in dist_df.iterrows():
            t_dist.add_row(
                str(row["commune"]),
                str(row["transaction_type"]),
                str(row["property_type"]),
                str(row["nb_biens"]),
                f"{int(row['p25_m2_xpf']):,} F".replace(",", " "),
                f"{int(row['median_m2_xpf']):,} F".replace(",", " "),
                f"{int(row['p75_m2_xpf']):,} F".replace(",", " "),
                f"{int(row['iqr_m2_xpf']):,} F".replace(",", " "),
            )
        console.print(t_dist)

    console.print("\n[bold green]=== DETECTION DES OPPORTUNITES DECOTEES (< VALEUR AVM) ===[/bold green]\n")
    opps = analytics.detect_market_opportunities(min_discount_pct=5.0)

    if opps:
        t_opp = Table(title="Biens présentant une décote significative d'acquisition")
        t_opp.add_column("Titre / Localisation", style="cyan")
        t_opp.add_column("Type", style="blue")
        t_opp.add_column("Prix Affiche (XPF)", justify="right")
        t_opp.add_column("Valeur AVM Estimée", justify="right", style="green")
        t_opp.add_column("Decote (%)", justify="right", style="bold red")
        t_opp.add_column("Gain Potentiel", justify="right", style="bold green")
        t_opp.add_column("Rendement Est.", justify="right", style="yellow")

        for op in opps:
            t_opp.add_row(
                f"{op['titre'][:40]}... ({op['commune']} - {op['quartier']})",
                str(op["type"]),
                f"{op['prix_affiche_xpf']:,} F".replace(",", " "),
                f"{op['valeur_estimee_xpf']:,} F".replace(",", " "),
                f"-{op['decote_pct']:.1f} %",
                f"+{op['gain_potentiel_xpf']:,} F".replace(",", " "),
                f"{op['rendement_locatif_estime']:.1f} %",
            )
        console.print(t_opp)
    else:
        console.print("[dim]Aucune anomalie de sous-évaluation détectée au seuil de 5%.[/dim]")


def generate_and_open_map(db: PropertyDatabase):
    console = Console()
    map_gen = GrandNoumeaMapGenerator(db)
    html_file = map_gen.generate_map()
    console.print(f"\n[bold green]OK: Carte interactive générée avec succès ![/bold green]")
    console.print(f"[bold cyan]Fichier HTML :[/bold cyan] [underline]{html_file}[/underline]")
    console.print("[dim]Vous pouvez ouvrir ce fichier directement dans votre navigateur web pour explorer les quartiers et les rendements.[/dim]\n")


def run_demo_estimations():
    engine = HedonicValuationEngine()
    explainer = ValuationExplainer()
    console = Console()

    console.print("\n[bold green]=== DEMONSTRATION DU MOTEUR D'EVALUATION AVM GRAND NOUMEA ===[/bold green]")

    # Cas 1 : Appartement F3 de standing à l'Anse Vata
    req1 = PropertyValuationRequest(
        commune=Commune.NOUMEA,
        quartier="Anse Vata",
        property_type=PropertyType.APPARTEMENT,
        surface_habitable_m2=82.0,
        surface_varangue_m2=22.0,
        vue_mer="BELLE_VUE",
        etage="DERNIER_ETAGE_ATTIQUE",
        etat_bien="RENOVE_HAUT_STANDING",
        parking_type="GARAGE_FERME_BOX",
        nb_parkings=2,
    )
    res1 = engine.evaluate(req1)
    explainer.print_console_report(res1)


def run_investment_simulation(prix: Optional[int] = None, loyer: Optional[int] = None, apport: Optional[int] = None):
    calc = InvestmentCalculator()
    reporter = InvestmentReporter()

    # Cas d'exemple ou paramètres personnalisés
    # Exemple investisseur : F2 à Dumbéa Koutio (13,5M F CFP, loyer 85 000 F/mois)
    prix_achat = prix or 13_500_000
    loyer_mens = loyer or 85_000
    apport_perso = apport if apport is not None else 1_500_000

    req = InvestmentRequest(
        nom_projet="Appartement F2 Koutio Dumbéa (Cas type)",
        prix_acquisition_xpf=prix_achat,
        frais_notaire_taux=0.08,
        apport_personnel_xpf=apport_perso,
        taux_emprunt_annuel_pct=4.2,
        duree_emprunt_annees=20,
        loyer_mensuel_xpf=loyer_mens,
        taux_vacance_locative_pct=5.0,
        charges_copro_mensuelles_non_recup_xpf=5_000,
        taxe_fonciere_annuelle_xpf=60_000,
        assurance_pno_annuelle_xpf=35_000,
        frais_gestion_agence_pct=7.0,
        inclure_tgc_gestion=True,
        provision_travaux_annuelle_xpf=40_000,
    )

    result = calc.simulate(req)
    reporter.print_investment_report(result)


def main():
    parser = argparse.ArgumentParser(description="Plateforme Data & Decision Immobiliere Grand Noumea")
    parser.add_argument("--ingest-sample", action="store_true", help="Ingerer le jeu d'annonces pilotes du Grand Noumea")
    parser.add_argument("--sync-live", action="store_true", help="Synchroniser les 4 portails en direct avec controle qualite automatique")
    parser.add_argument("--reconcile", action="store_true", help="Activer la reconciliation live 404/supprimees lors du --sync-live")
    parser.add_argument("--report", action="store_true", help="Afficher le rapport de synthese du marche")
    parser.add_argument("--stats", action="store_true", help="Afficher les percentiles et opportunités decotees")
    parser.add_argument("--map", action="store_true", help="Generer la carte interactive HTML Folium")
    parser.add_argument("--estimate", action="store_true", help="Lancer une evaluation immobiliere AVM")
    parser.add_argument("--simulate-invest", action="store_true", help="Lancer une simulation financiere d'investissement locatif")
    parser.add_argument("--audit", action="store_true", help="Auditer les logs et verifier l'integrite complete des donnees DuckDB")

    # Arguments spécifiques pour estimation
    parser.add_argument("--commune", choices=["NOUMEA", "DUMBEA", "MONT_DORE", "PAITA"], default=None)
    parser.add_argument("--quartier", type=str, default=None)
    parser.add_argument("--type", choices=["APPARTEMENT", "MAISON_VILLA"], default="APPARTEMENT")
    parser.add_argument("--surface", type=float, default=None)
    parser.add_argument("--varangue", type=float, default=0.0)
    parser.add_argument("--terrain", type=float, default=0.0)
    parser.add_argument("--vue-mer", choices=["AUCUNE", "APERÇU", "BELLE_VUE", "EXCEPTIONNELLE_FRONT_MER"], default="AUCUNE")
    parser.add_argument("--piscine", choices=["AUCUNE", "HORS_SOL_SEMI_ENTERREE", "COQUE_POLYESTER", "MACONNEE_LAGON"], default="AUCUNE")

    # Arguments spécifiques pour investissement
    parser.add_argument("--prix", type=int, default=None, help="Prix d'achat en F CFP")
    parser.add_argument("--loyer", type=int, default=None, help="Loyer mensuel prevu en F CFP")
    parser.add_argument("--apport", type=int, default=None, help="Apport personnel en F CFP")

    args = parser.parse_args()

    # Si aucun argument n'est fourni, on exécute une démonstration complète avec audit
    if not any([args.ingest_sample, args.sync_live, args.report, args.stats, args.map, args.estimate, args.simulate_invest, args.audit]):
        args.ingest_sample = True
        args.report = True
        args.stats = True
        args.map = True
        args.estimate = True
        args.simulate_invest = True
        args.audit = True

    logger.info("=== DEMARRAGE DU PIPELINE IMMOBILIER DU GRAND NOUMEA ===")
    db = PropertyDatabase()
    pipeline = IngestionPipeline(db)

    if args.sync_live:
        from src.ingestion.live_scraper import LiveNCScraper
        console = Console()
        console.print("[bold blue]>> Synchronisation live multi-portails + Controle Qualite Automatique...[/bold blue]")
        scraper = LiveNCScraper(db=db)
        res = scraper.run_live_sync(
            max_pages=10,
            include_rentals=True,
            include_yatoo=True,
            include_immonc=True,
            run_quality_control=True,
            run_canary=True,
            reconcile_404=args.reconcile,
        )
        qc = res.get("quality_control") or {}
        console.print(
            f"[green]OK: Sync terminee ({res['stored_cleaned']} traitees, total actif {res['total_in_db']}) | "
            f"Quality Gate: {res.get('quality_gate_status')} | "
            f"Score: {(qc.get('summary') or {}).get('avgScore')}/100 | "
            f"Canari Live: {qc.get('canary_freshness_pct')}%[/green]"
        )

    if args.ingest_sample:
        console = Console()
        console.print("[bold blue]>> Ingestion pilote Grand Noumea...[/bold blue]")
        pilot_data = get_pilot_dataset()
        stats = pipeline.process_and_store(pilot_data, batch_tag="pilot")
        qc = stats.get("quality_control") or {}
        console.print(
            f"[green]OK: Ingestion reussie : {stats['stored_cleaned']} annonces en base DuckDB "
            f"(Quality Gate: {qc.get('gate_status', 'PASS')}, Score: {(qc.get('summary') or {}).get('avgScore')}/100).[/green]"
        )
        logger.info(f"Ingestion pilote terminée : {stats['stored_cleaned']} annonces insérées/mises à jour.")

    if args.report:
        display_market_report(db)
        logger.info("Rapport de synthèse du marché affiché.")

    if args.stats:
        display_market_statistics(db)
        logger.info("Analyse statistique et détection d'opportunités terminées.")

    if args.map:
        generate_and_open_map(db)
        logger.info("Carte interactive HTML du Grand Nouméa générée.")

    if args.estimate:
        if args.commune and args.surface:
            engine = HedonicValuationEngine()
            explainer = ValuationExplainer()
            req = PropertyValuationRequest(
                commune=Commune[args.commune],
                quartier=args.quartier,
                property_type=PropertyType[args.type],
                surface_habitable_m2=args.surface,
                surface_varangue_m2=args.varangue,
                surface_terrain_m2=args.terrain,
                vue_mer=args.vue_mer,
                piscine=args.piscine,
            )
            res = engine.evaluate(req)
            explainer.print_console_report(res)
        else:
            run_demo_estimations()
        logger.info("Démonstration du moteur AVM terminée.")

    if args.simulate_invest:
        run_investment_simulation(args.prix, args.loyer, args.apport)
        logger.info("Simulation financière d'investissement terminée.")

    if args.audit:
        auditor = PipelineAuditor(db=db)
        audit_res = auditor.run_full_audit()
        auditor.print_audit_report(audit_res)
        logger.info(f"Audit complet réalisé : Statut={audit_res['status']}, Erreurs={audit_res['logs_summary']['error_count']}.")

    logger.info("=== EXECUTION DU PIPELINE TERMINEE AVEC SUCCES ===")


if __name__ == "__main__":
    main()

