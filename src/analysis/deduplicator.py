"""
Moteur de dédoublonnage multi-portails & détection d'écarts de prix entre agences (Sentinel ImmoNC).
Regroupe intelligemment les annonces identiques publiées sur plusieurs portails
(immobilier.nc, bienmeloger.nc, immonc, yatoo.nc) ou par plusieurs agences sans aucune perte de données :
- Conserve 100 % des offres mais marque la fiche canonique la plus riche (`isDuplicateSecondary = False`)
  et rattache toutes les occurrences dans `multiPortals`.
- Expose les écarts de prix entre agences sur un même bien (`priceSpreadXpf`, `minPriceAcrossPortals`, `maxPriceAcrossPortals`).
"""

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
from src.reference.geo import normalize_text

_STOP_TOKENS: Set[str] = {
    "avec", "pour", "dans", "cette", "sont", "bien", "tres", "plus", "situe", "situee",
    "proche", "noumea", "dumbea", "paita", "mont", "dore", "villa", "maison", "appartement",
    "louer", "vendre", "vente", "location", "contact", "agence", "visite", "prix", "loyer",
    "mois", "cfp", "xpf", "terrain", "quartier", "secteur", "calme", "beau", "belle",
    "grand", "grande", "pieces", "chambre", "chambres", "salle", "cuisine", "sejour",
    "salon", "terrasse", "parking", "place", "places", "surface", "environ", "comprenant",
    "compose", "equipee", "amenagee", "nouvelle", "caledonie", "annonce", "offre", "frais",
    "inclus", "charges", "disponible", "suite", "dont", "tout", "tous", "toutes", "vous",
    "nous", "votre", "notre", "leurs", "entre", "sous", "sans", "chez", "hors",
}

_LOT_RE = re.compile(r"\b(?:lot|parcelle)\s*(?:n°|numero|no|#)?\s*(\d+[a-z]?)\b", re.IGNORECASE)
_FLOOR_RE = re.compile(r"\b(rdc|rez[- ]de[- ]chaussee|rdj|rez[- ]de[- ]jardin|dernier\s+etage|\d+(?:er|eme|e)?\s+etage)\b", re.IGNORECASE)


def _extract_tokens(text: str) -> Set[str]:
    norm = normalize_text(text or "")
    return {w for w in re.findall(r"[a-z0-9]{4,}", norm) if w not in _STOP_TOKENS}


def _is_synthetic_description(desc: str) -> bool:
    s = (desc or "").strip()
    if len(s) < 120:
        return True
    if s.lower().startswith("annonce ") and "contact:" in s.lower() and len(s) < 220:
        return True
    return False


def _extract_lot_number(text: str) -> Optional[str]:
    m = _LOT_RE.search(normalize_text(text or ""))
    return m.group(1).lower() if m else None


def _extract_floor_key(item: Dict[str, Any]) -> Optional[str]:
    et = normalize_text(str(item.get("etage") or ""))
    if et:
        return et
    m = _FLOOR_RE.search(normalize_text(str(item.get("description") or "")))
    return m.group(1).lower() if m else None


def _quality_score(item: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """Score de richesse pour choisir la fiche canonique dans un groupe de doublons."""
    has_real_img = 0 if item.get("isDefaultImage") else 1
    img_count = len(item.get("images") or [])
    desc_len = len(str(item.get("description") or ""))
    prec_score = int(item.get("precisionScore") or 40)
    spec_count = sum(
        1 for k in ("surfaceHabitable", "surfaceTerrain", "surfaceVarangue", "chargesMensuelles", "rooms", "bedrooms", "bathrooms", "etage")
        if item.get(k)
    )
    has_phone = 1 if (item.get("agencyPhone") and item.get("agencyPhone") != "Non renseigné") else 0
    # Plus grand score = meilleure fiche canonique ; en cas d'égalité, privilégie le prix le plus bas
    neg_price = -(int(item.get("currentPrice") or 0))
    return (has_real_img, prec_score >= 70, min(img_count, 8) * 10 + spec_count * 5 + has_phone * 5, desc_len, neg_price)


def detect_and_annotate_duplicates(listings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Détecte les annonces doublons (multi-portails et multi-agences) et annote chaque fiche :
    - `isDuplicateSecondary` (bool)
    - `duplicateGroupId` (str | None)
    - `duplicateCount` (int)
    - `multiPortals` (List[Dict])
    - `minPriceAcrossPortals` (int | None)
    - `maxPriceAcrossPortals` (int | None)
    - `priceSpreadXpf` (int)
    """
    n = len(listings)
    if n == 0:
        return listings

    # Initialisation par défaut
    for item in listings:
        item["isDuplicateSecondary"] = False
        item["duplicateGroupId"] = None
        item["duplicateCount"] = 1
        item["multiPortals"] = []
        item["minPriceAcrossPortals"] = item.get("currentPrice")
        item["maxPriceAcrossPortals"] = item.get("currentPrice")
        item["priceSpreadXpf"] = 0

    if n < 2:
        return listings

    # Union-Find
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Pré-calcul léger par annonce et bucketing O(N)
    buckets: Dict[Tuple[Any, ...], List[int]] = defaultdict(list)
    meta: List[Dict[str, Any]] = []

    for idx, item in enumerate(listings):
        desc = str(item.get("description") or "")
        title = str(item.get("title") or "")
        q_norm = normalize_text(str(item.get("quartier") or ""))
        if q_norm in ("secteur caledonien", "secteur", "nc", "none", "nan"):
            q_norm = ""
        price = int(item.get("currentPrice") or 0)
        surf_h = float(item.get("surfaceHabitable") or 0.0)
        surf_t = float(item.get("surfaceTerrain") or 0.0)
        ag_norm = normalize_text(str(item.get("agencyName") or ""))
        img_url = "" if item.get("isDefaultImage") else str(item.get("image") or "").strip()

        meta.append({
            "price": price,
            "surf_h": surf_h,
            "surf_t": surf_t,
            "q": q_norm,
            "tokens": _extract_tokens(desc),
            "synthetic": _is_synthetic_description(desc),
            "lot": _extract_lot_number(f"{title} {desc}"),
            "floor": _extract_floor_key(item),
            "agency": ag_norm,
            "img": img_url,
            "source": str(item.get("source") or "").lower(),
        })

        bkey = (
            str(item.get("transactionType") or "VENTE"),
            str(item.get("commune") or ""),
            str(item.get("category") or ""),
            int(item.get("rooms") or 0),
        )
        buckets[bkey].append(idx)

    # Comparaison intra-bucket avec garde-fous anti-faux-doublons
    for indices in buckets.values():
        m_len = len(indices)
        if m_len < 2:
            continue
        for i in range(m_len):
            ia = indices[i]
            ma = meta[ia]
            pa = ma["price"]
            if pa <= 0:
                continue
            for j in range(i + 1, m_len):
                ib = indices[j]
                mb = meta[ib]
                pb = mb["price"]
                if pb <= 0:
                    continue

                # 1. Écart de prix toléré <= 6 %
                p_diff = abs(pa - pb) / max(pa, pb)
                if p_diff > 0.06:
                    continue

                # 2. Quartier compatible si les deux sont renseignés
                if ma["q"] and mb["q"] and ma["q"] != mb["q"]:
                    continue

                # 3. Jamais fusionner deux lots ou deux étages explicitement différents
                if ma["lot"] and mb["lot"] and ma["lot"] != mb["lot"]:
                    continue
                if ma["floor"] and mb["floor"] and ma["floor"] != mb["floor"]:
                    continue

                # 4. Surfaces compatibles si renseignées (tolérance 6 %)
                if ma["surf_h"] > 0 and mb["surf_h"] > 0:
                    if abs(ma["surf_h"] - mb["surf_h"]) / max(ma["surf_h"], mb["surf_h"]) > 0.06:
                        continue
                if ma["surf_t"] > 0 and mb["surf_t"] > 0:
                    if abs(ma["surf_t"] - mb["surf_t"]) / max(ma["surf_t"], mb["surf_t"]) > 0.06:
                        continue

                # 5. Match exact sur photo réelle (hors illustration)
                same_photo = bool(ma["img"] and mb["img"] and ma["img"] == mb["img"])
                if same_photo and p_diff <= 0.06:
                    union(ia, ib)
                    continue

                # 6. Descriptions synthétiques/courtes : exiger même agence + prix quasi identique + surface exacte
                if ma["synthetic"] or mb["synthetic"]:
                    same_ag = bool(ma["agency"] and mb["agency"] and ma["agency"] == mb["agency"])
                    exact_surf = (
                        (ma["surf_h"] > 0 and mb["surf_h"] > 0 and abs(ma["surf_h"] - mb["surf_h"]) <= 1.5)
                        or (ma["surf_t"] > 0 and mb["surf_t"] > 0 and abs(ma["surf_t"] - mb["surf_t"]) <= 10.0)
                    )
                    if same_ag and p_diff <= 0.01 and exact_surf and ma["q"] and ma["q"] == mb["q"]:
                        union(ia, ib)
                    continue

                # 7. Similarité textuelle Jaccard sur descriptifs riches
                ta, tb = ma["tokens"], mb["tokens"]
                union_len = len(ta | tb)
                if union_len < 6:
                    continue
                jacc = len(ta & tb) / union_len

                exact_spec = (
                    p_diff <= 0.01
                    and ma["q"]
                    and ma["q"] == mb["q"]
                    and (
                        (ma["surf_h"] > 0 and mb["surf_h"] > 0 and abs(ma["surf_h"] - mb["surf_h"]) <= 2.0)
                        or (ma["surf_t"] > 0 and mb["surf_t"] > 0 and abs(ma["surf_t"] - mb["surf_t"]) <= 15.0)
                    )
                )

                if jacc >= 0.55 or (jacc >= 0.38 and exact_spec):
                    union(ia, ib)

    # Regroupement des clusters de taille >= 2
    clusters: Dict[int, List[int]] = defaultdict(list)
    for idx in range(n):
        clusters[find(idx)].append(idx)

    group_counter = 0
    for root_idx, member_indices in clusters.items():
        if len(member_indices) < 2:
            continue
        group_counter += 1
        group_id = f"DUP-{group_counter:04d}"

        # Choisir la fiche canonique la plus riche
        canonical_idx = max(member_indices, key=lambda i: _quality_score(listings[i]))

        prices = [int(listings[i].get("currentPrice") or 0) for i in member_indices if int(listings[i].get("currentPrice") or 0) > 0]
        min_p = min(prices) if prices else 0
        max_p = max(prices) if prices else 0
        spread_p = max(0, max_p - min_p)

        portal_entries = []
        for i in member_indices:
            it = listings[i]
            portal_entries.append({
                "id": it.get("id"),
                "source": it.get("source"),
                "url": it.get("sourceUrl"),
                "agencyName": it.get("agencyName"),
                "price": int(it.get("currentPrice") or 0),
                "isCanonical": (i == canonical_idx),
            })
        portal_entries.sort(key=lambda e: (e["price"] or 0, not e["isCanonical"]))

        for i in member_indices:
            it = listings[i]
            it["isDuplicateSecondary"] = (i != canonical_idx)
            it["duplicateGroupId"] = group_id
            it["duplicateCount"] = len(member_indices)
            it["multiPortals"] = portal_entries
            it["minPriceAcrossPortals"] = min_p
            it["maxPriceAcrossPortals"] = max_p
            it["priceSpreadXpf"] = spread_p

    return listings
