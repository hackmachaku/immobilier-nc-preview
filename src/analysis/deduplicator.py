"""
Moteur de dédoublonnage & de FUSION MULTI-SOURCES ENRICHISSANTE (Sentinel ImmoNC v2).
Regroupe intelligemment les annonces identiques publiées sur plusieurs portails
(immobilier.nc, bienmeloger.nc, immonc, yatoo.nc) ou par plusieurs agences sans aucune perte de données :
- Anti-faux-positifs stricts : références mandats CRM dans les URLs photos (LA2065, VM1499...), lots pluriels, étages normalisés.
- Rappel inter-portails doublé : alias d'agences, matching synthétique inter-portails, plafond 12 % sur forte similarité Jaccard >= 0,75, détection HT/TTC (TGC 6 %).
- FUSION NON-DESTRUCTIVE : la fiche canonique hérite automatiquement des champs manquants de ses jumelles
  (surfaces, charges, étage, SDB, parkings, contacts directs, galerie photo la plus complète, précision GPS/Cadastre).
"""

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
from src.reference.geo import normalize_text
from src.processing.cleaner import normalize_etage

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

_LOT_RE = re.compile(
    r"\b(?:lots?|parcelles?)\s*(?:n°|numero|no|#)?\s*(\d+[a-z]?(?:\s*(?:a|et|-)\s*\d+[a-z]?)?)\b",
    re.IGNORECASE,
)
_FLOOR_RE = re.compile(
    r"\b(rdc|rez[- ]de[- ]chaussee|rdj|rez[- ]de[- ]jardin|dernier\s+etage|(?:premier|deuxieme|troisieme|quatrieme|\d+(?:er|eme|e)?)\s+etage)\b",
    re.IGNORECASE,
)
_CRM_MANDATE_RE = re.compile(r"(?:^|[_/\-])((?:LA|LM|LV|VA|VM|VT|VC|VP|VL|MV|ML)\d{3,6})(?:[_\-.]|$)", re.IGNORECASE)

# Alias d'agences connues entre portails pour éviter les faux négatifs inter-portails
_AGENCY_CANON_MAP: Dict[str, str] = {
    "le service": "caledonienne d immobilier",
    "le service immobilier": "caledonienne d immobilier",
    "caledonienne d immobilier": "caledonienne d immobilier",
    "vkpimmo": "top immo",
    "vkp immo": "top immo",
    "top immo": "top immo",
    "agence du faubourg": "agence du faubourg",
    "du faubourg": "agence du faubourg",
    "caillard kaddour": "caillard kaddour",
    "caillard et kaddour": "caillard kaddour",
}


def _canonical_agency(agency_name: Any) -> str:
    norm = normalize_text(str(agency_name or ""))
    if not norm or norm in ("particulier", "particulier nc", "particulier immonc", "agence immobiliere", "inconnu"):
        return ""
    return _AGENCY_CANON_MAP.get(norm, norm)


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
    return re.sub(r"\s+", " ", m.group(1).strip().lower()) if m else None


def _extract_crm_mandate(images: List[str]) -> Optional[str]:
    """Extrait le code mandat CRM intégré dans les URLs photos (ex: LA2065, VM1499) pour distinguer 2 biens d'une même agence."""
    for u in (images or [])[:6]:
        fname = str(u).split("?")[0].rsplit("/", 1)[-1]
        m = _CRM_MANDATE_RE.search(fname)
        if m:
            return m.group(1).upper()
    return None


def _extract_floor_key(item: Dict[str, Any]) -> Optional[str]:
    et = normalize_etage(item.get("etage"))
    if et:
        return normalize_text(et)
    m = _FLOOR_RE.search(normalize_text(str(item.get("description") or "")))
    if m:
        et2 = normalize_etage(m.group(1))
        return normalize_text(et2) if et2 else m.group(1).lower()
    return None


def _unique_photo_count(item: Dict[str, Any]) -> int:
    """Compte le nombre de photos RÉELLES uniques (immunisé contre les doublons www/vignette)."""
    if item.get("isDefaultImage"):
        return 0
    seen = set()
    for u in (item.get("images") or []):
        s = str(u or "").strip()
        if not s.startswith("http") or "unsplash.com" in s:
            continue
        fname = s.split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()
        key = fname if ("immonc.com" in s.lower() and fname) else s.lower()
        seen.add(key)
    return len(seen)


def _quality_score(item: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """Score de richesse pour choisir la fiche canonique dans un groupe de doublons."""
    has_real_img = 0 if item.get("isDefaultImage") else 1
    img_count = _unique_photo_count(item)
    desc_len = len(str(item.get("description") or ""))
    prec_score = int(item.get("precisionScore") or 40)
    spec_count = sum(
        1 for k in (
            "surfaceHabitable", "surfaceTerrain", "surfaceVarangue",
            "chargesMensuelles", "rooms", "bedrooms", "bathrooms", "etage", "parkings"
        )
        if item.get(k)
    )
    has_phone = 1 if (item.get("agencyPhone") and item.get("agencyPhone") != "Non renseigné") else 0
    has_comp = 1 if item.get("complementaryDetails") else 0
    neg_price = -(int(item.get("currentPrice") or 0))
    return (
        has_real_img,
        1 if prec_score >= 70 else 0,
        min(img_count, 10) * 10 + spec_count * 6 + has_phone * 6 + has_comp * 8,
        desc_len,
        neg_price,
    )


def _fuse_cluster_fields(canonical: Dict[str, Any], siblings: List[Dict[str, Any]]) -> None:
    """
    Fusion non-destructive multi-sources : enrichit la fiche canonique avec tous les champs
    manquants ou plus précis disponibles chez ses jumelles sur les autres portails.
    """
    enriched_from: Set[str] = set()
    enriched_fields: List[str] = []

    def _record(src: str, field: str):
        if src and src != canonical.get("source"):
            enriched_from.add(src)
        if field not in enriched_fields:
            enriched_fields.append(field)

    # Trier les jumelles de la plus riche à la moins riche
    sorted_sibs = sorted(siblings, key=_quality_score, reverse=True)

    # 1. Champs numériques / scalaires manquants
    scalar_fields = [
        ("surfaceHabitable", lambda v: v is not None and float(v) > 0),
        ("surfaceTerrain", lambda v: v is not None and float(v) > 0),
        ("surfaceVarangue", lambda v: v is not None and float(v) > 0),
        ("chargesMensuelles", lambda v: v is not None and int(v) > 0),
        ("rooms", lambda v: v is not None and int(v) > 0),
        ("bedrooms", lambda v: v is not None and int(v) >= 0),
        ("bathrooms", lambda v: v is not None and int(v) > 0),
        ("parkings", lambda v: v is not None and int(v) > 0),
        ("parkingDetail", lambda v: bool(v)),
        ("etage", lambda v: bool(v)),
        ("standing", lambda v: bool(v)),
        ("complementaryDetails", lambda v: isinstance(v, dict) and len(v) > 0),
        ("directNegotiator", lambda v: bool(v)),
        ("directPhone", lambda v: bool(v)),
    ]

    for field, is_valid in scalar_fields:
        if is_valid(canonical.get(field)):
            continue
        for sib in sorted_sibs:
            cand = sib.get(field)
            if is_valid(cand):
                # Garde-fou terrain : ne pas injecter surfaceHabitable dans un terrain nu
                if field == "surfaceHabitable" and canonical.get("category") == "terrain":
                    continue
                canonical[field] = cand
                _record(str(sib.get("source") or ""), field)
                break

    # 2. Booléens d'équipements (OR logique conservateur)
    for bfield in ("hasElevator", "hasPool", "hasAirConditioning", "hasSeaView", "isSecured"):
        if not canonical.get(bfield):
            for sib in sorted_sibs:
                if sib.get(bfield) is True:
                    canonical[bfield] = True
                    _record(str(sib.get("source") or ""), bfield)
                    break

    # isFurnished (uniquement si None)
    if canonical.get("isFurnished") is None:
        for sib in sorted_sibs:
            if sib.get("isFurnished") is not None:
                canonical["isFurnished"] = sib["isFurnished"]
                canonical["furnishedLabel"] = sib.get("furnishedLabel") or ("Meublé" if sib["isFurnished"] else "Non meublé")
                _record(str(sib.get("source") or ""), "isFurnished")
                break

    # 3. Téléphone agence / WhatsApp / Email / Contacts directs (union dédupliquée)
    if not canonical.get("agencyPhone") or canonical.get("agencyPhone") == "Non renseigné":
        for sib in sorted_sibs:
            ph = sib.get("agencyPhone")
            if ph and ph != "Non renseigné":
                canonical["agencyPhone"] = ph
                _record(str(sib.get("source") or ""), "agencyPhone")
                break
    if not canonical.get("whatsapp"):
        for sib in sorted_sibs:
            if sib.get("whatsapp"):
                canonical["whatsapp"] = sib["whatsapp"]
                _record(str(sib.get("source") or ""), "whatsapp")
                break
    if not canonical.get("agencyEmail"):
        for sib in sorted_sibs:
            if sib.get("agencyEmail"):
                canonical["agencyEmail"] = sib["agencyEmail"]
                _record(str(sib.get("source") or ""), "agencyEmail")
                break

    existing_contacts = list(canonical.get("directContacts") or [])
    seen_raw_ph = {str(c.get("raw_phone") or c.get("phone") or "") for c in existing_contacts if isinstance(c, dict)}
    for sib in sorted_sibs:
        for c in (sib.get("directContacts") or []):
            if not isinstance(c, dict):
                continue
            key_c = str(c.get("raw_phone") or c.get("phone") or "")
            if key_c and key_c not in seen_raw_ph:
                seen_raw_ph.add(key_c)
                existing_contacts.append(c)
                _record(str(sib.get("source") or ""), "directContacts")
    canonical["directContacts"] = existing_contacts

    # 4. Galerie photos (remplace placeholder ou galerie pauvre par la meilleure galerie réelle)
    canon_imgs_n = _unique_photo_count(canonical)
    for sib in sorted_sibs:
        sib_imgs_n = _unique_photo_count(sib)
        if sib_imgs_n > canon_imgs_n and not sib.get("isDefaultImage"):
            canonical["images"] = list(sib.get("images") or [])
            canonical["image"] = sib.get("image")
            canonical["isDefaultImage"] = False
            canon_imgs_n = sib_imgs_n
            _record(str(sib.get("source") or ""), "images")

    # 5. Description & Fiche structurée (sauvetage si la canonique est une coquille courte)
    if _is_synthetic_description(str(canonical.get("description") or "")):
        for sib in sorted_sibs:
            sib_desc = str(sib.get("description") or "")
            if not _is_synthetic_description(sib_desc) and len(sib_desc) > len(str(canonical.get("description") or "")) + 60:
                canonical["description"] = sib_desc
                if sib.get("structuredDescription"):
                    canonical["structuredDescription"] = sib["structuredDescription"]
                if sib.get("descriptionHighlights"):
                    canonical["descriptionHighlights"] = sib["descriptionHighlights"]
                _record(str(sib.get("source") or ""), "description")
                break

    # 6. Montée en précision GPS & Cadastre/REFIL (si une jumelle a une localisation certifiée supérieure)
    canon_prec = int(canonical.get("precisionScore") or 40)
    geo_fields = (
        "lat", "lon", "latPrecise", "lonPrecise", "precisionScore", "precisionLevel",
        "precisionDetail", "precisionBadge", "precisionBadgeClass", "precisionDot",
        "isCadastreCertifie", "cadastreStatus", "cadastreComment", "cadastreNic",
        "cadastreLot", "cadastreLotissement", "cadastreSection", "cadastreCommune",
        "cadastreContenance", "cadastreSurfaceM2", "cadastreTypologie",
        "surfaceEcartM2", "surfaceEcartPct", "surfaceEcartStatus", "surfaceEcartLabel",
        "isConforme", "refilNom", "refilType", "refilAdresse", "refilLivraison", "refilGestion",
    )
    for sib in sorted_sibs:
        sib_prec = int(sib.get("precisionScore") or 40)
        if sib_prec > canon_prec and sib.get("latPrecise") and sib.get("lonPrecise"):
            for gf in geo_fields:
                if gf in sib:
                    canonical[gf] = sib[gf]
            canon_prec = sib_prec
            _record(str(sib.get("source") or ""), "geoPrecision")
            break

    # 7. Recalcul du prix/m² si une surface manquante vient d'être fusionnée
    price_xpf = int(canonical.get("currentPrice") or 0)
    if price_xpf > 0 and (not canonical.get("priceM2") or float(canonical.get("priceM2") or 0) <= 0):
        if canonical.get("category") == "terrain" and float(canonical.get("surfaceTerrain") or 0) > 0:
            canonical["priceM2"] = round(price_xpf / float(canonical["surfaceTerrain"]), 0)
        elif float(canonical.get("surfaceHabitable") or 0) > 0:
            canonical["priceM2"] = round(price_xpf / float(canonical["surfaceHabitable"]), 1 if canonical.get("isLocation") else 0)

    canonical["enrichedFromPortals"] = sorted(enriched_from)
    canonical["enrichedFields"] = enriched_fields


def detect_and_annotate_duplicates(listings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Détecte les annonces doublons (multi-portails et multi-agences), fusionne les champs complémentaires
    vers la fiche canonique, et annote chaque fiche.
    """
    n = len(listings)
    if n == 0:
        return listings

    for item in listings:
        item["isDuplicateSecondary"] = False
        item["duplicateGroupId"] = None
        item["duplicateCount"] = 1
        item["multiPortals"] = []
        item["minPriceAcrossPortals"] = item.get("currentPrice")
        item["maxPriceAcrossPortals"] = item.get("currentPrice")
        item["priceSpreadXpf"] = 0
        item["priceSpreadNote"] = None
        item["enrichedFromPortals"] = []
        item["enrichedFields"] = []

    if n < 2:
        return listings

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
        ag_canon = _canonical_agency(item.get("agencyName"))
        imgs = item.get("images") or []
        img_url = "" if item.get("isDefaultImage") else str(item.get("image") or "").strip()
        src = str(item.get("source") or "").lower()
        cat = str(item.get("category") or "")
        # Regrouper maison_villa et maison_riviere_mer dans le même bucket (même bien avec/sans tag vue mer)
        bucket_cat = "maison_villa" if cat == "maison_riviere_mer" else cat

        meta.append({
            "price": price,
            "surf_h": surf_h,
            "surf_t": surf_t,
            "q": q_norm,
            "tokens": _extract_tokens(desc),
            "synthetic": _is_synthetic_description(desc),
            "lot": _extract_lot_number(f"{title} {desc}"),
            "mandate": _extract_crm_mandate(imgs),
            "floor": _extract_floor_key(item),
            "agency": ag_canon,
            "img": img_url,
            "source": src,
            "rooms": int(item.get("rooms") or 0),
        })

        bkey = (
            str(item.get("transactionType") or "VENTE"),
            str(item.get("commune") or ""),
            bucket_cat,
        )
        buckets[bkey].append(idx)

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

                is_cross_portal = ma["source"] != mb["source"]

                # 0. Nombre de pièces compatible si les deux sont renseignés
                if ma["rooms"] > 0 and mb["rooms"] > 0 and ma["rooms"] != mb["rooms"]:
                    continue

                # 1. Plafond d'écart de prix : <= 12 % en inter-portails (vérifié plus bas par Jaccard >= 0,75), <= 6 % sinon
                p_diff = abs(pa - pb) / max(pa, pb)
                if p_diff > (0.12 if is_cross_portal else 0.06):
                    continue

                # 2. Quartier compatible si les deux sont renseignés
                if ma["q"] and mb["q"] and ma["q"] != mb["q"]:
                    continue

                # 3. Garde-fous anti-faux-positifs : mandats CRM distincts, lots distincts, étages normalisés distincts
                if ma["mandate"] and mb["mandate"] and ma["mandate"] != mb["mandate"]:
                    continue
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

                # 6. Descriptions synthétiques/courtes :
                #    - Inter-portails : même agence + prix quasi identique (<= 1 %) + même quartier + mêmes pièces (même si surface=0 sur stub immonc)
                #    - Intra-portail : exiger en plus surface exacte (évite fusion de 2 lots distincts d'une même agence)
                if ma["synthetic"] or mb["synthetic"]:
                    same_ag = bool(ma["agency"] and mb["agency"] and ma["agency"] == mb["agency"])
                    exact_surf = (
                        (ma["surf_h"] > 0 and mb["surf_h"] > 0 and abs(ma["surf_h"] - mb["surf_h"]) <= 1.5)
                        or (ma["surf_t"] > 0 and mb["surf_t"] > 0 and abs(ma["surf_t"] - mb["surf_t"]) <= 10.0)
                    )
                    if is_cross_portal and same_ag and p_diff <= 0.01 and ma["q"] and ma["q"] == mb["q"]:
                        if exact_surf or (ma["surf_h"] == 0 or mb["surf_h"] == 0):
                            union(ia, ib)
                    elif (not is_cross_portal) and same_ag and p_diff <= 0.005 and exact_surf and ma["q"] and ma["q"] == mb["q"]:
                        # Intra-portail : ne jamais fusionner deux annonces synthétiques sans preuve de mandat identique
                        if ma["mandate"] and mb["mandate"] and ma["mandate"] == mb["mandate"]:
                            union(ia, ib)
                    continue

                # 7. Similarité textuelle Jaccard sur descriptifs riches
                ta, tb = ma["tokens"], mb["tokens"]
                union_len = len(ta | tb)
                if union_len < 6:
                    continue
                jacc = len(ta & tb) / union_len

                # Écart de prix > 6 % : autorisé UNIQUEMENT en inter-portails avec très haute similarité textuelle (>= 0,75)
                if p_diff > 0.06:
                    if is_cross_portal and jacc >= 0.75:
                        union(ia, ib)
                    continue

                # Intra-portail : exiger seuil plus strict (évite les templates d'agence de résidences neuves)
                if not is_cross_portal:
                    if jacc >= 0.78 and p_diff <= 0.01 and ma["q"] and ma["q"] == mb["q"]:
                        union(ia, ib)
                    continue

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

    clusters: Dict[int, List[int]] = defaultdict(list)
    for idx in range(n):
        clusters[find(idx)].append(idx)

    group_counter = 0
    for root_idx, member_indices in clusters.items():
        if len(member_indices) < 2:
            continue
        group_counter += 1
        group_id = f"DUP-{group_counter:04d}"

        canonical_idx = max(member_indices, key=lambda i: _quality_score(listings[i]))
        canonical_item = listings[canonical_idx]
        sibling_items = [listings[i] for i in member_indices if i != canonical_idx]

        # Fusion non-destructive des champs manquants vers la fiche canonique
        _fuse_cluster_fields(canonical_item, sibling_items)

        prices = [int(listings[i].get("currentPrice") or 0) for i in member_indices if int(listings[i].get("currentPrice") or 0) > 0]
        min_p = min(prices) if prices else 0
        max_p = max(prices) if prices else 0
        spread_p = max(0, max_p - min_p)
        spread_note = None
        if spread_p > 0 and min_p > 0:
            ratio = max_p / min_p
            if abs(ratio - 1.06) <= 0.006:
                spread_note = "Écart compatible HT / TTC (TGC 6 %)"

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
            it["priceSpreadNote"] = spread_note

    return listings
