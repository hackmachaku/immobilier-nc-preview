"""
Garde-Fou Qualité Production Automatisé (Quality Gate) — Sentinel ImmoNC.
Évalue 100 % des fiches sur 5 piliers (0-100 pts, Grade A/B/C/D), isole en quarantaine
les anomalies P0 bloquantes avant publication & calcul AVM, et produit le rapport de santé
exposé par GET /api/quality et data_quality.json.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

NC_LAT_MIN, NC_LAT_MAX = -23.10, -19.50
NC_LON_MIN, NC_LON_MAX = 163.50, 168.50

_SEASONAL_RE = re.compile(r"\b(?:/\s*nuit|par\s+nuit|la\s+nuit|/\s*semaine|par\s+semaine|location\s+saisonni[eè]re)\b", re.IGNORECASE)
_OUT_OF_SCOPE_TITLE_RE = re.compile(r"^\s*(?:recherche|cherche)\b|\btable\s+de\s+s[eé]jour\b", re.IGNORECASE)
_OUT_OF_NC_RE = re.compile(r"\b(?:montr[eé]al|qu[eé]bec|canada|paris\s+\d+e|m[eé]tropole)\b", re.IGNORECASE)
_CP1252_RE = re.compile(r"[\x80-\x9f]")


def evaluate_listing_quality(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calcule le score déterministe de fiabilité & complétude (0-100), le Grade (A/B/C/D),
    les drapeaux d'audit et le statut de quarantaine P0 pour une annonce individuelle.
    """
    score = 100.0
    flags: List[str] = []
    quarantine_reasons: List[str] = []

    title = str(item.get("title") or "")
    desc = str(item.get("description") or "")
    cat = str(item.get("category") or "")
    tx = str(item.get("transactionType") or "VENTE").upper()
    price = int(item.get("currentPrice") or 0)
    surf_h = float(item.get("surfaceHabitable") or 0.0)
    surf_t = float(item.get("surfaceTerrain") or 0.0)
    rooms = item.get("rooms")
    bedrooms = item.get("bedrooms")
    charges = item.get("chargesMensuelles")
    lat = item.get("lat")
    lon = item.get("lon")
    prec_score = int(item.get("precisionScore") or 40)
    sd = item.get("structuredDescription") or []
    quartier = str(item.get("quartier") or "")
    agency_phone = str(item.get("agencyPhone") or "")
    contacts = item.get("directContacts") or []

    # --- CONTROLES P0 QUARANTAINE ---
    if _OUT_OF_SCOPE_TITLE_RE.search(title):
        quarantine_reasons.append("HORS_PERIMETRE_RECHERCHE_OU_OBJET")
    if _OUT_OF_NC_RE.search(f"{title} {desc[:250]}") and not re.search(r"\b(?:noum[eé]a|dumb[eé]a|pa[iï]ta|mont[- ]dore|cal[eé]donie|bourail|kon[eé])\b", f"{title} {desc[:250]}", re.I):
        quarantine_reasons.append("HORS_NOUVELLE_CALEDONIE")
    if lat is not None and lon is not None:
        try:
            if not (NC_LAT_MIN <= float(lat) <= NC_LAT_MAX and NC_LON_MIN <= float(lon) <= NC_LON_MAX):
                quarantine_reasons.append("GPS_HORS_NC")
        except (ValueError, TypeError):
            quarantine_reasons.append("GPS_INVALIDE")
    if price <= 0:
        quarantine_reasons.append("PRIX_NUL")
    elif tx == "VENTE" and price < 1_000_000:
        quarantine_reasons.append("PRIX_VENTE_ABERRANT_BAS")
    elif tx == "LOCATION" and price < 35_000 and _SEASONAL_RE.search(f"{title} {desc[:300]}"):
        quarantine_reasons.append("LOCATION_SAISONNIERE_NUIT_SEMAINE")

    # Aberrations prix/m² critiques (surface partielle ou erreur unité)
    if tx == "VENTE" and cat == "terrain" and surf_t > 0:
        pm2_t = price / surf_t
        if pm2_t > 250_000:
            quarantine_reasons.append("PRIX_M2_TERRAIN_ABERRANT")
    elif tx == "VENTE" and cat != "terrain" and surf_h > 0:
        pm2_h = price / surf_h
        if pm2_h > 1_500_000:
            quarantine_reasons.append("PRIX_M2_VENTE_ABERRANT")
    elif tx == "LOCATION" and surf_h > 0:
        pm2_l = price / surf_h
        if pm2_l > 6_000:
            quarantine_reasons.append("LOYER_M2_ABERRANT")

    if rooms is not None and bedrooms is not None and int(bedrooms) > int(rooms):
        quarantine_reasons.append("CHAMBRES_SUPERIEUR_PIECES")
    if tx == "LOCATION" and charges and price > 0 and int(charges) >= price:
        quarantine_reasons.append("CHARGES_SUPERIEUR_LOYER")

    # --- PILIER 1 : Complétude cœur métier (30 pts) ---
    if price <= 0:
        score -= 15.0
        flags.append("Prix manquant")
    if cat == "terrain":
        if surf_t <= 0:
            score -= 10.0
            flags.append("Surface terrain manquante")
    else:
        if surf_h <= 0:
            score -= 10.0
            flags.append("Surface habitable manquante")
    if cat in ("appartement", "maison_villa", "maison_riviere_mer"):
        if not rooms:
            score -= 4.0
            flags.append("Nb pièces manquant")
        if bedrooms is None:
            score -= 3.0
    if not quartier or quartier.lower() in ("secteur calédonien", "secteur", "none"):
        score -= 3.0
        flags.append("Quartier imprécis")
    if item.get("isDefaultImage"):
        score -= 5.0
        flags.append("Photo réelle absente")

    # --- PILIER 2 : Fidélité Zéro-Perte NLP (25 pts) ---
    if len(desc) >= 40 and not sd:
        score -= 12.0
        flags.append("Fiche structurée vide")
    elif len(desc) < 50:
        score -= 8.0
        flags.append("Descriptif très court")

    # --- PILIER 3 : Cohérence & Zéro Faux Positif (20 pts) ---
    if quarantine_reasons:
        score -= min(25.0, 12.0 * len(quarantine_reasons))
        flags.extend(quarantine_reasons[:2])

    # --- PILIER 4 : Qualité textuelle & Contacts (15 pts) ---
    if _CP1252_RE.search(desc):
        score -= 4.0
        flags.append("Encodage CP1252")
    if "<br" in desc.lower() or "<strong>" in desc.lower():
        score -= 3.0
        flags.append("HTML brut résiduel")
    has_valid_phone = bool(
        (agency_phone and agency_phone != "Non renseigné")
        or any(isinstance(c, dict) and c.get("phone") for c in contacts)
    )
    if not has_valid_phone:
        score -= 4.0
        flags.append("Contact téléphonique absent")

    # --- PILIER 5 : Précision Géo / Cadastre (10 pts) ---
    if lat is None or lon is None:
        score -= 5.0
    elif prec_score < 50:
        score -= 3.0
    elif prec_score < 70:
        score -= 1.0

    # Bonus léger (+2 max, plafonné à 100) si enrichi par fusion multi-sources
    if item.get("enrichedFromPortals"):
        score = min(100.0, score + 2.0)

    final_score = round(max(0.0, min(100.0, score)), 1)
    if quarantine_reasons or final_score < 55.0:
        grade = "D"
    elif final_score >= 85.0:
        grade = "A"
    elif final_score >= 70.0:
        grade = "B"
    else:
        grade = "C"

    return {
        "qualityScore": final_score,
        "qualityGrade": grade,
        "qualityFlags": flags[:5],
        "isQuarantined": bool(quarantine_reasons),
        "quarantineReasons": quarantine_reasons,
    }


def run_quality_gate(
    listings: List[Dict[str, Any]],
    prev_snapshot_path: Optional[Path] = None,
    canary_report: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    Exécute le Quality Gate complet sur le lot d'annonces :
    1. Note 100 % des fiches (`qualityScore`, `qualityGrade`, `qualityFlags`).
    2. Sépare le flux public sain (`public_listings`) de la quarantaine P0 (`quarantined_listings`).
    3. Construit le rapport complet de santé par portail + statut PASS / WARN / BLOCK.
    """
    public_listings: List[Dict[str, Any]] = []
    quarantined_listings: List[Dict[str, Any]] = []

    by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    grade_counts: Counter = Counter()
    reason_counts: Counter = Counter()
    cp1252_count = 0
    html_raw_count = 0
    empty_sd_count = 0
    fusion_enriched_count = 0
    total_fused_fields = 0

    for item in listings:
        q = evaluate_listing_quality(item)
        item["qualityScore"] = q["qualityScore"]
        item["qualityGrade"] = q["qualityGrade"]
        item["qualityFlags"] = q["qualityFlags"]
        item["isQuarantined"] = q["isQuarantined"]

        grade_counts[q["qualityGrade"]] += 1
        by_source[str(item.get("source") or "autre")].append(item)

        desc = str(item.get("description") or "")
        if _CP1252_RE.search(desc):
            cp1252_count += 1
        if "<br" in desc.lower() or "<strong>" in desc.lower():
            html_raw_count += 1
        if len(desc) >= 40 and not (item.get("structuredDescription") or []):
            empty_sd_count += 1
        if item.get("enrichedFromPortals"):
            fusion_enriched_count += 1
            total_fused_fields += len(item.get("enrichedFields") or [])

        if q["isQuarantined"]:
            for r in q["quarantineReasons"]:
                reason_counts[r] += 1
            quarantined_listings.append({
                "id": item.get("id"),
                "source": item.get("source"),
                "title": item.get("title"),
                "currentPrice": item.get("currentPrice"),
                "transactionType": item.get("transactionType"),
                "category": item.get("category"),
                "sourceUrl": item.get("sourceUrl"),
                "qualityScore": q["qualityScore"],
                "quarantineReasons": q["quarantineReasons"],
            })
        else:
            public_listings.append(item)

    total_n = len(listings)
    pub_n = len(public_listings)
    avg_score = round(sum(float(x["qualityScore"]) for x in public_listings) / pub_n, 1) if pub_n else 0.0
    scores_sorted = sorted(float(x["qualityScore"]) for x in public_listings)
    median_score = round(scores_sorted[pub_n // 2], 1) if pub_n else 0.0

    # Matrice de santé par portail
    portal_health = []
    for src, items in sorted(by_source.items(), key=lambda kv: -len(kv[1])):
        sn = len(items)
        s_pub = [x for x in items if not x.get("isQuarantined")]
        s_avg = round(sum(float(x["qualityScore"]) for x in items) / sn, 1) if sn else 0.0
        s_grade_a = sum(1 for x in items if x["qualityGrade"] == "A")
        s_surf = sum(1 for x in items if (float(x.get("surfaceTerrain") or 0) > 0 if x.get("category") == "terrain" else float(x.get("surfaceHabitable") or 0) > 0))
        s_phone = sum(1 for x in items if (x.get("agencyPhone") and x.get("agencyPhone") != "Non renseigné"))
        s_real_photo = sum(1 for x in items if not x.get("isDefaultImage"))
        s_fused = sum(1 for x in items if x.get("enrichedFromPortals"))
        portal_health.append({
            "source": src,
            "total": sn,
            "published": len(s_pub),
            "quarantined": sn - len(s_pub),
            "avgScore": s_avg,
            "gradeAPct": round(s_grade_a / sn * 100, 1) if sn else 0.0,
            "surfaceCompletenessPct": round(s_surf / sn * 100, 1) if sn else 0.0,
            "phoneCompletenessPct": round(s_phone / sn * 100, 1) if sn else 0.0,
            "realPhotoPct": round(s_real_photo / sn * 100, 1) if sn else 0.0,
            "fusionEnrichedCount": s_fused,
        })

    # Vérification des règles de blocage (Publish Guard)
    block_reasons: List[str] = []
    warn_reasons: List[str] = []

    if total_n == 0:
        block_reasons.append("CATALOGUE_VIDE")
    if prev_snapshot_path and prev_snapshot_path.exists():
        try:
            prev_data = json.loads(prev_snapshot_path.read_text(encoding="utf-8"))
            prev_n = int(prev_data.get("count") or len(prev_data.get("data") or []))
            if prev_n > 100 and pub_n < prev_n * 0.50:
                warn_reasons.append(f"VARIATION_VOLUME ({prev_n} -> {pub_n})")
        except Exception:
            pass

    if total_n > 0:
        empty_sd_pct = empty_sd_count / total_n * 100.0
        cp1252_pct = cp1252_count / total_n * 100.0
        if empty_sd_pct > 1.0:
            block_reasons.append(f"SD_VIDE_ELEVE ({empty_sd_pct:.1f}%)")
        elif empty_sd_pct > 0.3:
            warn_reasons.append(f"SD_VIDE ({empty_sd_pct:.1f}%)")
        if cp1252_pct > 1.5:
            block_reasons.append(f"ENCODAGE_CP1252 ({cp1252_pct:.1f}%)")
        if avg_score < 78.0:
            block_reasons.append(f"SCORE_MOYEN_INSUFFISANT ({avg_score})")
        elif avg_score < 85.0:
            warn_reasons.append(f"SCORE_MOYEN_A_AMELIORER ({avg_score})")

    if canary_report and isinstance(canary_report, dict):
        if canary_report.get("selectorDriftPortals"):
            warn_reasons.append(f"CANARY_DRIFT ({', '.join(canary_report['selectorDriftPortals'])})")

    gate_status = "BLOCK" if block_reasons else ("WARN" if warn_reasons else "PASS")

    # Statistiques globales utiles au tableau de bord UI
    dedup_clusters = len({x["duplicateGroupId"] for x in public_listings if x.get("duplicateGroupId")})
    cadastre_cert = sum(1 for x in public_listings if x.get("isCadastreCertifie"))

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "gateStatus": gate_status,
        "blockReasons": block_reasons,
        "warnReasons": warn_reasons,
        "summary": {
            "totalEvaluated": total_n,
            "publishedCount": pub_n,
            "quarantinedCount": len(quarantined_listings),
            "avgScore": avg_score,
            "medianScore": median_score,
            "gradeCounts": {
                "A": grade_counts.get("A", 0),
                "B": grade_counts.get("B", 0),
                "C": grade_counts.get("C", 0),
                "D": grade_counts.get("D", 0),
            },
            "gradeAPct": round(grade_counts.get("A", 0) / total_n * 100, 1) if total_n else 0.0,
            "zeroLossSdPct": round((1 - empty_sd_count / total_n) * 100, 2) if total_n else 100.0,
            "cp1252Count": cp1252_count,
            "htmlRawCount": html_raw_count,
            "dedupClustersCount": dedup_clusters,
            "fusionEnrichedCount": fusion_enriched_count,
            "totalFusedFields": total_fused_fields,
            "cadastreCertifiedCount": cadastre_cert,
        },
        "portalHealth": portal_health,
        "quarantineBreakdown": dict(reason_counts.most_common()),
        "quarantinedSamples": quarantined_listings[:50],
        "canary": canary_report,
    }

    return public_listings, quarantined_listings, report
