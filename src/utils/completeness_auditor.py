"""
Agent d'audit de complétude et d'intégrité des données immobilières.
Détecte la troncature de texte, les corruptions d'encodage (mojibake)
et valide la présence des attributs techniques essentiels.
"""

import re
import json
from typing import Dict, Any, List, Optional
from src.storage.database import PropertyDatabase
from src.utils.logger import get_logger

logger = get_logger("completeness_auditor")

MOJIBAKE_PATTERNS = [
    r"Ã©", r"Ã¨", r"Ã ", r"Ã´", r"Ã®", r"Ã§", r"Ã¹",
    r"â€“", r"â€”", r"â€™", r"â€œ", r"â€", r"âœ…", r"âœ", r"â˜Ž",
    r"Â\s*", r"\ufffd", r"\?{3,}"
]

TRUNCATION_PATTERNS = [
    r"\.\.\.\s*$",
    r"…\s*$",
    r"â€¦\s*$",
    r"\b(?:une|un|le|la|les|de|des|du|au|aux|et|ou|avec|pour|en)\s*\.\.\.\s*$",
]


class CompletenessAuditor:
    """Agent d'audit vérifiant qu'aucune donnée d'annonce n'est perdue ou dégradée."""

    def __init__(self, db: Optional[PropertyDatabase] = None):
        self.db = db or PropertyDatabase()

    def audit_text(self, text: Optional[str]) -> Dict[str, Any]:
        """Analyse un texte pour détecter troncature et corruptions mojibake."""
        if not text or not text.strip():
            return {
                "length": 0,
                "is_truncated": False,
                "has_mojibake": False,
                "mojibake_samples": [],
                "issues": ["Texte vide ou absent"]
            }

        s = text.strip()
        length = len(s)
        issues = []
        is_truncated = False
        mojibake_samples = []

        # Troncature
        for pat in TRUNCATION_PATTERNS:
            if re.search(pat, s, re.IGNORECASE):
                is_truncated = True
                issues.append("Description se termine par une ellipse (...) ou une troncature")
                break

        if length < 50:
            issues.append(f"Description excessivement courte ({length} caractères)")

        # Mojibake
        for pat in MOJIBAKE_PATTERNS:
            found = re.findall(pat, s)
            if found:
                mojibake_samples.extend(found[:3])
                issues.append(f"Corruption d'encodage détectée (motif: {pat})")

        return {
            "length": length,
            "is_truncated": is_truncated,
            "has_mojibake": len(mojibake_samples) > 0,
            "mojibake_samples": list(set(mojibake_samples)),
            "issues": issues
        }

    def audit_listing(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Audite une fiche d'annonce complète pour évaluer son score de complétude."""
        listing_id = str(item.get("id") or item.get("source_id") or "inconnu")
        title = str(item.get("title") or "")
        desc = str(item.get("description") or "")
        prop_type = str(item.get("property_type") or item.get("property_type_declared") or "")

        text_audit = self.audit_text(desc)
        title_audit = self.audit_text(title)

        issues = list(text_audit["issues"])
        if title_audit["has_mojibake"]:
            issues.append("Mojibake détecté dans le titre")

        # Vérification des spécifications techniques
        missing_specs = []
        surface = item.get("surface_habitable_m2") or item.get("raw_surface")
        if surface is None or (isinstance(surface, (int, float)) and surface <= 0):
            # Pour les terrains, c'est surface_terrain_m2
            if "terrain" in prop_type.lower():
                surf_ter = item.get("surface_terrain_m2")
                if surf_ter is None or (isinstance(surf_ter, (int, float)) and surf_ter <= 0):
                    missing_specs.append("Surface terrain manquante")
            else:
                missing_specs.append("Surface habitable manquante")

        # Charges copropriété pour appartements
        charges = item.get("charges_mensuelles_xpf") or item.get("raw_charges")
        if charges is None and "appartement" in prop_type.lower():
            missing_specs.append("Charges de copropriété non renseignées")

        # Contact négociateur direct
        raw_neg = item.get("direct_negotiator")
        direct_neg = str(raw_neg).strip() if raw_neg is not None and str(raw_neg).strip() not in ("", "None", "nan", "0.0", "0") else None
        raw_phone = item.get("direct_phone")
        direct_phone = str(raw_phone).strip() if raw_phone is not None and str(raw_phone).strip() not in ("", "None", "nan", "0.0", "0") else None

        # Calcul du score de complétude (0 à 100)
        score = 100.0
        if text_audit["is_truncated"]:
            score -= 30.0
        if text_audit["has_mojibake"] or title_audit["has_mojibake"]:
            score -= 25.0
        if missing_specs:
            score -= (10.0 * len(missing_specs))
        if text_audit["length"] < 80:
            score -= 15.0

        score = max(0.0, min(100.0, score))

        return {
            "listing_id": listing_id,
            "score": round(score, 1),
            "is_complete": score >= 80.0 and not text_audit["is_truncated"] and not text_audit["has_mojibake"],
            "is_truncated": text_audit["is_truncated"],
            "has_mojibake": text_audit["has_mojibake"] or title_audit["has_mojibake"],
            "desc_length": text_audit["length"],
            "missing_specs": missing_specs,
            "has_negotiator_contact": bool(direct_neg or direct_phone),
            "direct_negotiator": direct_neg,
            "direct_phone": direct_phone,
            "issues": issues,
        }

    def audit_database(self, limit: int = 500, source_filter: Optional[str] = None) -> Dict[str, Any]:
        """Exécute un audit global sur la base DuckDB locale (requête strictement paramétrée)."""
        safe_limit = max(1, min(int(limit), 10000))
        params: List[Any] = []
        query = "SELECT * FROM listings WHERE is_active=TRUE"
        if source_filter:
            query += " AND source = ?"
            params.append(str(source_filter))
        query += " ORDER BY scraped_at DESC LIMIT ?"
        params.append(safe_limit)

        try:
            df = self.db.query(query, params)
        except Exception as e:
            logger.error(f"Erreur requête audit database : {e}")
            return {"error": str(e)}

        if df.empty:
            return {"total_audited": 0, "status": "EMPTY"}

        records = df.to_dict(orient="records")
        results = []
        truncated_count = 0
        mojibake_count = 0
        total_score = 0.0

        for rec in records:
            res = self.audit_listing(rec)
            results.append(res)
            if res["is_truncated"]:
                truncated_count += 1
            if res["has_mojibake"]:
                mojibake_count += 1
            total_score += res["score"]

        total = len(results)
        avg_score = round(total_score / total, 1) if total > 0 else 0.0

        return {
            "total_audited": total,
            "average_completeness_score": avg_score,
            "truncated_count": truncated_count,
            "truncated_percentage": round((truncated_count / total) * 100, 1) if total > 0 else 0.0,
            "mojibake_count": mojibake_count,
            "mojibake_percentage": round((mojibake_count / total) * 100, 1) if total > 0 else 0.0,
            "fully_complete_count": sum(1 for r in results if r["is_complete"]),
            "sample_issues": [r for r in results if not r["is_complete"]][:10],
        }
