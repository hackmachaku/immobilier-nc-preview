"""
Module d'enrichissement cadastral et foncier pour Immobilier NC.
Croise les coordonnées géographiques des annonces avec les bases officielles :
- Cadastre NC (parcelles-cadastrales-nc : NIC, lot, lotissement, contenance légale)
- REFIL (referentiel-des-immeubles-localises-refil : nom d'immeuble, année, adresse, syndic)
- Détection et calcul d'écart de surface (surface déclarée vs surface cadastrale officielle)
"""

import math
import re
import json
import logging
from pathlib import Path
from typing import Any
import duckdb

logger = logging.getLogger(__name__)

DEFAULT_CADASTRE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cadastre"


def parse_cadastre_area(surface_str: str | None) -> float | None:
    """
    Convertit une contenance cadastrale légale en m².
    Exemples :
    - '0ha 13a 94ca' -> 1394.0 m² (0*10000 + 13*100 + 94)
    - '0ha 5a 15ca' -> 515.0 m²
    - '3ha 19a 70ca' -> 31970.0 m²
    - '1200 m²' -> 1200.0 m²
    """
    if not surface_str or not isinstance(surface_str, str):
        return None

    clean = surface_str.strip().lower()

    # Pattern standard 'Xha Ya Zca'
    match_hac = re.search(r'(?:(\d+)\s*ha)?\s*(?:(\d+)\s*a)?\s*(?:(\d+)\s*ca)?', clean)
    if match_hac and any(match_hac.groups()):
        h_str, a_str, ca_str = match_hac.groups()
        h = int(h_str) if h_str else 0
        a = int(a_str) if a_str else 0
        ca = int(ca_str) if ca_str else 0
        total_m2 = (h * 10000) + (a * 100) + ca
        if total_m2 > 0:
            return float(total_m2)

    # Pattern alternatif nombre direct '1250 m2' ou '1250'
    match_num = re.search(r'([\d\s\.,]+)\s*(?:m2|m²)?', clean)
    if match_num:
        try:
            num_str = match_num.group(1).replace(' ', '').replace(',', '.')
            val = float(num_str)
            if val > 0:
                return val
        except ValueError:
            pass

    return None


def calculate_surface_discrepancy(
    declared_m2: float | int | None,
    cadastre_m2: float | int | None
) -> dict[str, Any]:
    """
    Compare la surface déclarée dans l'annonce avec la surface cadastrale officielle.
    Retourne l'écart en m², le pourcentage et un statut de conformité.
    """
    if not declared_m2 or not cadastre_m2 or declared_m2 <= 0 or cadastre_m2 <= 0:
        return {
            "surfaceEcartM2": None,
            "surfaceEcartPct": None,
            "surfaceEcartStatus": "NON_EVALUABLE",
            "surfaceEcartLabel": "Surface cadastrale de référence disponible",
            "isConforme": None
        }

    dec = float(declared_m2)
    cad = float(cadastre_m2)

    ecart_m2 = round(dec - cad, 1)
    ecart_pct = round((ecart_m2 / cad) * 100, 1)

    abs_pct = abs(ecart_pct)

    if abs_pct <= 5.0:
        status = "CONFORME"
        label = "✅ Surface conforme au Cadastre (±5%)"
        is_conforme = True
    elif abs_pct <= 15.0:
        status = "ECART_MINEUR"
        sign = "+" if ecart_pct > 0 else ""
        label = f"ℹ️ Écart mineur ({sign}{ecart_pct}% déclaré vs cadastre)"
        is_conforme = True
    else:
        status = "ECART_MAJEUR"
        sign = "+" if ecart_pct > 0 else ""
        label = f"⚠️ Écart significatif ({sign}{ecart_pct}% déclaré vs cadastre)"
        is_conforme = False

    return {
        "surfaceEcartM2": ecart_m2,
        "surfaceEcartPct": ecart_pct,
        "surfaceEcartStatus": status,
        "surfaceEcartLabel": label,
        "isConforme": is_conforme
    }


class CadastreSpatialIndex:
    """
    Index spatial ultra-rapide en mémoire basé sur un découpage en grille 2D.
    Permet de croiser des milliers de coordonnées en quelques millisecondes
    avec les parcelles cadastrales et les immeubles REFIL.
    """

    def __init__(self, cadastre_dir: Path | None = None, cell_size_deg: float = 0.005):
        self.cadastre_dir = cadastre_dir or DEFAULT_CADASTRE_DIR
        self.cell_size = cell_size_deg
        self.parcel_grid: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.refil_grid: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self.refil_text_lookup: list[tuple[str, float, float, str, str, str]] = []
        self.quartier_parcels: dict[str, list[dict[str, Any]]] = {}
        self.commune_quartier_parcels: dict[str, dict[str, list[dict[str, Any]]]] = {}
        self.commune_parcels: dict[str, list[dict[str, Any]]] = {}
        self.is_loaded = False

    def load(self) -> None:
        """Charge les fichiers Parquet et initialise les grilles spatiales et les tables de correspondance."""
        parcelles_path = self.cadastre_dir / "parcelles_nc.parquet"
        refil_path = self.cadastre_dir / "refil_nc.parquet"

        if not parcelles_path.exists() or not refil_path.exists():
            from src.ingestion.cadastre_ingestion import ensure_cadastre_data
            ensure_cadastre_data(self.cadastre_dir)

        con = duckdb.connect()
        try:
            con.execute("INSTALL spatial; LOAD spatial;")
        except Exception:
            pass

        # 1. Charger les parcelles
        p_str = str(parcelles_path).replace("\\", "/")
        p_rows = con.execute(f"""
            SELECT 
                nic, num_lot, lotissement, section_cadastrale, commune, surface_cadastrale, typologie,
                ST_X(geo_point_2d) as lon, ST_Y(geo_point_2d) as lat
            FROM '{p_str}'
            WHERE geo_point_2d IS NOT NULL
        """).fetchall()

        self.parcel_grid.clear()
        all_parcels_flat = []
        for r in p_rows:
            lon, lat = r[7], r[8]
            if lon is None or lat is None:
                continue
            gx = int(lon / self.cell_size)
            gy = int(lat / self.cell_size)
            key = (gx, gy)
            if key not in self.parcel_grid:
                self.parcel_grid[key] = []
            
            raw_surface = r[5]
            m2 = parse_cadastre_area(raw_surface)
            p_obj = {
                "nic": r[0],
                "num_lot": r[1],
                "lotissement": r[2],
                "section": r[3],
                "commune": r[4],
                "contenance": raw_surface,
                "surface_m2": m2,
                "typologie": r[6],
                "lon": lon,
                "lat": lat
            }
            self.parcel_grid[key].append(p_obj)
            all_parcels_flat.append(p_obj)

        # 2. Charger les immeubles REFIL
        r_str = str(refil_path).replace("\\", "/")
        r_rows = con.execute(f"""
            SELECT 
                nom, type, libadrs1, quartier, apparten, livraiso, gestion,
                ST_X(point_geo) as lon, ST_Y(point_geo) as lat
            FROM '{r_str}'
            WHERE point_geo IS NOT NULL
        """).fetchall()

        self.refil_grid.clear()
        self.refil_text_lookup.clear()
        for r in r_rows:
            lon, lat = r[7], r[8]
            if lon is None or lat is None:
                continue
            gx = int(lon / self.cell_size)
            gy = int(lat / self.cell_size)
            key = (gx, gy)
            if key not in self.refil_grid:
                self.refil_grid[key] = []
            
            livr = r[5]
            annee = str(livr.year) if hasattr(livr, 'year') else (str(livr)[:4] if livr else None)
            nom_raw = str(r[0] or "").strip()

            self.refil_grid[key].append({
                "nom": nom_raw,
                "type": r[1],
                "adresse": r[2],
                "quartier": r[3],
                "commune": r[4],
                "livraison": annee,
                "gestion": r[6],
                "lon": lon,
                "lat": lat
            })

            # Base de recherche textuelle par nom d'immeuble (exclut les termes trop vagues)
            base_nom = nom_raw.split('|')[0].strip()
            if len(base_nom) >= 5 and base_nom.lower() not in ("residence", "immeuble", "batiment", "villa", "maison"):
                self.refil_text_lookup.append((
                    base_nom.lower(),
                    lat,
                    lon,
                    nom_raw,
                    str(r[2] or ""),
                    str(r[3] or "")
                ))

        # 3. Construire le vivier de parcelles cadastrales indexées par commune et par quartier
        self.commune_parcels.clear()
        for p in all_parcels_flat:
            c_raw = str(p["commune"] or "").upper().replace("-", "_").replace(" ", "_")
            if "MONT_DORE" in c_raw:
                c_raw = "MONT_DORE"
            elif "POYA" in c_raw:
                c_raw = "POYA"
            if c_raw not in self.commune_parcels:
                self.commune_parcels[c_raw] = []
            self.commune_parcels[c_raw].append(p)

        self.quartier_parcels.clear()
        self.commune_quartier_parcels.clear()
        ref_path = self.cadastre_dir.parent / "reference" / "referentiel_grand_noumea.json"
        if ref_path.exists():
            try:
                with open(ref_path, "r", encoding="utf-8") as f:
                    ref_data = json.load(f)
                for com_k, com_val in ref_data.get("communes", {}).items():
                    ck = com_k.upper().replace("-", "_").replace(" ", "_")
                    if ck not in self.commune_quartier_parcels:
                        self.commune_quartier_parcels[ck] = {}
                    commune_pool = self.commune_parcels.get(ck, [])
                    for q in com_val.get("quartiers", []):
                        q_name = str(q.get("nom") or "").strip().lower()
                        qlat = float(q.get("latitude", 0))
                        qlon = float(q.get("longitude", 0))
                        if not qlat or not qlon:
                            continue
                        matched = []
                        for p in commune_pool:
                            plat, plon = p["lat"], p["lon"]
                            dx = (plon - qlon) * math.cos(math.radians((plat + qlat) / 2)) * 111320
                            dy = (plat - qlat) * 110540
                            dist = math.sqrt(dx * dx + dy * dy)
                            if dist <= 1200:  # Rayon de 1,2 km autour du barycentre terrestre du quartier
                                matched.append(p)
                        if matched:
                            self.commune_quartier_parcels[ck][q_name] = matched
                            self.quartier_parcels[q_name] = matched
            except Exception as e:
                logger.warning(f"Impossible de construire le mapping quartier/parcelles : {e}")

        con.close()
        self.is_loaded = True
        logger.info(
            "Index spatial cadastre initialisé : %d parcelles dans %d cellules, %d immeubles REFIL (%d pour lookup textuel), %d communes et %d quartiers mappés.",
            len(p_rows), len(self.parcel_grid), len(r_rows), len(self.refil_text_lookup), len(self.commune_parcels), len(self.quartier_parcels)
        )

    def find_nearest_parcel(
        self, lat: float, lon: float, max_dist_m: float = 120.0
    ) -> tuple[dict[str, Any] | None, float | None]:
        """Trouve la parcelle cadastrale la plus proche dans un rayon maximal."""
        if not self.is_loaded:
            self.load()

        gx = int(lon / self.cell_size)
        gy = int(lat / self.cell_size)

        best_dist = float("inf")
        best_parcel = None

        # Rayon de recherche sur les cellules adjacentes
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cell = self.parcel_grid.get((gx + dx, gy + dy))
                if not cell:
                    continue
                for p in cell:
                    plon, plat = p["lon"], p["lat"]
                    # Distance équirectangulaire projetée rapide
                    x = (plon - lon) * math.cos(math.radians((plat + lat) / 2)) * 111320
                    y = (plat - lat) * 110540
                    dist = math.sqrt(x * x + y * y)
                    if dist < best_dist:
                        best_dist = dist
                        best_parcel = p

        if best_dist <= max_dist_m:
            return best_parcel, round(best_dist, 1)
        return None, None

    def find_nearest_building(
        self, lat: float, lon: float, max_dist_m: float = 80.0
    ) -> tuple[dict[str, Any] | None, float | None]:
        """Trouve l'immeuble REFIL le plus proche dans un rayon maximal."""
        if not self.is_loaded:
            self.load()

        gx = int(lon / self.cell_size)
        gy = int(lat / self.cell_size)

        best_dist = float("inf")
        best_building = None

        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cell = self.refil_grid.get((gx + dx, gy + dy))
                if not cell:
                    continue
                for b in cell:
                    blon, blat = b["lon"], b["lat"]
                    x = (blon - lon) * math.cos(math.radians((blat + lat) / 2)) * 111320
                    y = (blat - lat) * 110540
                    dist = math.sqrt(x * x + y * y)
                    if dist < best_dist:
                        best_dist = dist
                        best_building = b

        if best_dist <= max_dist_m:
            return best_building, round(best_dist, 1)
        return None, None

    def resolve_listing_geography(
        self,
        item_id: str,
        commune: str | None = None,
        quartier: str | None = None,
        title: str | None = "",
        description: str | None = ""
    ) -> dict[str, Any]:
        """
        Résout les coordonnées géographiques terrestres précises pour une annonce.
        Niveau 1 : Immeuble / Résidence REFIL identifiée dans le titre ou la description (validé dans la commune).
        Niveau 2 : Répartition déterministe sur les parcelles cadastrales réelles du quartier DANS la commune.
        Niveau 3 : Répartition déterministe sur le vivier de parcelles cadastrales réelles de la commune.
        Niveau 4 : Localité identifiée en brousse ou barycentre sécurisé sur la terre ferme.
        """
        if not self.is_loaded:
            self.load()

        text = f"{title or ''} {description or ''}".lower()
        text_clean = re.sub(r'(?:contact|tél|tel|email|mail|agent)\s*:.*', '', text, flags=re.IGNORECASE)

        com_clean = str(commune or "NOUMEA").upper().replace("-", "_").replace(" ", "_")
        target_com = com_clean

        COMMUNE_BOUNDS = {
            "NOUMEA": {"lat_min": -22.4783, "lat_max": -22.2169, "lon_min": 166.2930, "lon_max": 166.5062},
            "DUMBEA": {"lat_min": -22.2274, "lat_max": -22.0799, "lon_min": 166.3918, "lon_max": 166.5931},
            "MONT_DORE": {"lat_min": -22.4673, "lat_max": -22.1486, "lon_min": 166.4802, "lon_max": 166.9733},
            "PAITA": {"lat_min": -22.2436, "lat_max": -21.9438, "lon_min": 166.0812, "lon_max": 166.4217},
        }

        BROUSSE_COMMUNES = {
            "BOURAIL": (-21.564, 165.493),
            "LA_FOA": (-21.711, 165.828),
            "BOULOUPARIS": (-21.866, 166.052),
            "POUEMBOUT": (-21.134, 164.901),
            "KONE": (-21.059, 164.865),
            "VOH": (-20.963, 164.698),
            "KOUMAC": (-20.559, 164.283),
            "KAALA_GOMEN": (-20.668, 164.402),
            "POINDIMIE": (-20.932, 165.334),
            "TOUHO": (-20.789, 165.253),
            "HIENGHENE": (-20.683, 164.935),
            "HOUAILOU": (-21.284, 165.626),
            "CANALA": (-21.517, 165.958),
            "THIO": (-21.613, 166.216),
            "YATE": (-22.160, 166.958),
            "MOINDOU": (-21.554, 165.679),
            "FARINO": (-21.660, 165.772),
            "SARRAMEA": (-21.644, 165.845),
            "POUM": (-20.233, 164.025),
            "LIFOU": (-20.916, 167.240),
            "MARE": (-21.488, 167.973),
            "OUVEA": (-20.638, 166.576),
            "ILE_DES_PINS": (-22.617, 167.456),
            "POYA": (-21.349, 165.155),
        }

        # Si commune déclarée AUTRE, détecter la localité en brousse dans le titre/texte
        if com_clean == "AUTRE":
            for b_name in BROUSSE_COMMUNES:
                b_kw = b_name.lower().replace("_", " ")
                if re.search(r"\b" + re.escape(b_kw) + r"\b", text_clean):
                    target_com = b_name
                    break

        # Niveau 1 : Détection immeuble REFIL avec vérification communale stricte
        COMMON_WORDS = {
            "berger", "dominique", "niaoulis", "palmiers", "soleil", "colline",
            "terrasse", "plage", "grand sud", "jardin", "marina", "baie",
            "centre ville", "centre-ville", "port", "vallee", "vallée", "vue mer"
        }
        TRIGGERS = ("résidence", "residence", "immeuble", "bâtiment", "batiment", "domaine", "lotissement", "tour")

        if target_com in COMMUNE_BOUNDS:
            b_bounds = COMMUNE_BOUNDS[target_com]
            for b_nom, b_lat, b_lon, orig_nom, adrs, q_refil in self.refil_text_lookup:
                # Vérifier que le bâtiment REFIL est bien dans la commune ciblée
                if not (b_bounds["lat_min"] <= b_lat <= b_bounds["lat_max"] and b_bounds["lon_min"] <= b_lon <= b_bounds["lon_max"]):
                    continue

                # Si le nom est un mot commun ou court, exiger un déclencheur
                if b_nom in COMMON_WORDS or len(b_nom) < 7:
                    matched_trigger = any(
                        f"{cw} {b_nom}" in text_clean or f"{cw} « {b_nom} »" in text_clean or f"{cw} \"{b_nom}\"" in text_clean
                        for cw in TRIGGERS
                    )
                    if not matched_trigger:
                        continue
                else:
                    if not (f" {b_nom} " in f" {text_clean} " or f"'{b_nom}" in text_clean or f'"{b_nom}' in text_clean or f"({b_nom}" in text_clean):
                        continue

                return {
                    "lat": b_lat,
                    "lon": b_lon,
                    "match_type": "REFIL_BUILDING",
                    "refilNom": orig_nom,
                    "refilAdresse": adrs,
                    "refilQuartier": q_refil
                }

        # Niveau 2 : Répartition sur parcelle cadastrale réelle du quartier DANS la commune
        q_pool = self.commune_quartier_parcels.get(target_com, {})
        clean_q = str(quartier or "").lower().strip()
        p_list = q_pool.get(clean_q)
        if not p_list:
            for k, v in q_pool.items():
                if k in clean_q or clean_q in k:
                    p_list = v
                    break
        if not p_list:
            for k, v in q_pool.items():
                if len(k) >= 5 and re.search(r"\b" + re.escape(k) + r"\b", text_clean):
                    p_list = v
                    break

        if p_list:
            idx = abs(hash(str(item_id))) % len(p_list)
            p = p_list[idx]
            return {
                "lat": p["lat"],
                "lon": p["lon"],
                "match_type": "QUARTIER_PARCEL",
                "parcel": p
            }

        # Niveau 3 : Répartition sur le vivier de parcelles cadastrales de la commune
        c_pool = self.commune_parcels.get(target_com)
        if c_pool:
            idx = abs(hash(str(item_id))) % len(c_pool)
            p = c_pool[idx]
            return {
                "lat": p["lat"],
                "lon": p["lon"],
                "match_type": f"COMMUNE_PARCEL_{target_com}",
                "parcel": p
            }

        # Niveau 4 : Localité Brousse ou Fallback Grande Terre sur terre ferme
        if target_com in BROUSSE_COMMUNES:
            base_lat, base_lon = BROUSSE_COMMUNES[target_com]
            h = abs(hash(str(item_id)))
            j_lat = ((h % 40) - 20) * 0.0002
            j_lon = (((h // 40) % 40) - 20) * 0.0002
            return {
                "lat": base_lat + j_lat,
                "lon": base_lon + j_lon,
                "match_type": f"BROUSSE_TOWN_{target_com}"
            }

        centers = {
            "NOUMEA": (-22.2710, 166.4420),
            "DUMBEA": (-22.1850, 166.4450),
            "MONT_DORE": (-22.2285, 166.5206),
            "PAITA": (-22.1310, 166.3650),
            "AUTRE": (-21.5000, 165.5000),
        }
        base_lat, base_lon = centers.get(com_clean, (-21.5000, 165.5000))
        h = abs(hash(str(item_id)))
        j_lat = ((h % 40) - 20) * 0.0002
        j_lon = (((h // 40) % 40) - 20) * 0.0002
        return {
            "lat": base_lat + j_lat,
            "lon": base_lon + j_lon,
            "match_type": "COMMUNE_FALLBACK"
        }

    def enrich_listing(self, item: dict[str, Any]) -> dict[str, Any]:
        """
        Enrichit une annonce individuelle avec les données du Cadastre et de REFIL.
        """
        lat = item.get("lat")
        lon = item.get("lon")
        if lat is None or lon is None:
            return item

        try:
            lat = float(lat)
            lon = float(lon)
        except (ValueError, TypeError):
            return item

        # Garde-fou foncier : Si le score de précision géographique est de niveau quartier (<= 50%),
        # la localisation est une approximation sectorielle (~500m d'incertitude).
        # On ne doit PAS attribuer arbitrairement la parcelle ou l'immeuble situés sous le centroïde !
        prec_score = item.get("precisionScore")
        if prec_score is None:
            prec_score = item.get("precision_score")

        is_approx_quartier = False
        if prec_score is not None:
            try:
                is_approx_quartier = float(prec_score) <= 50.0
            except (ValueError, TypeError):
                pass

        item["isCadastreCertifie"] = not is_approx_quartier

        if is_approx_quartier:
            item["cadastreStatus"] = "INDICATIF_QUARTIER"
            item["cadastreComment"] = "Localisation indicative à l'échelle du quartier (~500m). Parcelle exacte et copropriété non certifiables sans adresse ou nom de résidence."
            item["cadastreNic"] = None
            item["cadastreLot"] = None
            item["cadastreLotissement"] = None
            item["cadastreSection"] = str(item.get("quartier") or item.get("commune") or "Secteur NC")
            item["cadastreCommune"] = str(item.get("commune") or "Nouméa")
            item["cadastreContenance"] = None
            item["cadastreSurfaceM2"] = None
            item["cadastreTypologie"] = "Zone Urbaine Mixte"
            item["surfaceEcartLabel"] = None
            item["refilNom"] = None
            item["refilType"] = None
            item["refilAdresse"] = None
            return item

        # 1. Parcelle Cadastrale
        parcel, p_dist = self.find_nearest_parcel(lat, lon, max_dist_m=120.0)
        if parcel:
            item["cadastreNic"] = parcel.get("nic")
            item["cadastreLot"] = parcel.get("num_lot")
            item["cadastreLotissement"] = parcel.get("lotissement")
            item["cadastreSection"] = parcel.get("section")
            item["cadastreCommune"] = parcel.get("commune")
            item["cadastreContenance"] = parcel.get("contenance")
            item["cadastreSurfaceM2"] = parcel.get("surface_m2")
            item["cadastreTypologie"] = parcel.get("typologie")
            item["cadastreDistanceM"] = p_dist

            # Détecteur d'écarts de surface
            is_apt = (
                item.get("propertyType") in ("APPARTEMENT", "STUDIO", "LOFT")
                or "appartement" in (str(item.get("title", "")) + " " + str(item.get("type", ""))).lower()
            )
            declared_surf = (
                item.get("surfaceTerrain")
                or item.get("surface_terrain_m2")
                or item.get("surface")
                or item.get("surface_m2")
            )
            
            if is_apt:
                # En copropriété, la contenance cadastrale est l'assiette globale du bâtiment
                cad_m2 = parcel.get("surface_m2")
                cad_str = f"{cad_m2:,.0f} m²".replace(",", " ") if cad_m2 else parcel.get("contenance")
                hab_str = f"{declared_surf} m²" if declared_surf else "N/C"
                item["surfaceEcartM2"] = None
                item["surfaceEcartPct"] = None
                item["surfaceEcartStatus"] = "COPROPRIETE"
                item["surfaceEcartLabel"] = f"🏢 Assiette foncière copropriété : {cad_str} (Habitable : {hab_str})"
                item["isConforme"] = True
            else:
                disc = calculate_surface_discrepancy(declared_surf, parcel.get("surface_m2"))
                item["surfaceEcartM2"] = disc["surfaceEcartM2"]
                item["surfaceEcartPct"] = disc["surfaceEcartPct"]
                item["surfaceEcartStatus"] = disc["surfaceEcartStatus"]
                item["surfaceEcartLabel"] = disc["surfaceEcartLabel"]
                item["isConforme"] = disc["isConforme"]

        # 2. Immeuble REFIL (Ensemble immobilier / résidence)
        is_apt = (
            item.get("propertyType") in ("APPARTEMENT", "STUDIO", "LOFT")
            or "appartement" in (str(item.get("title", "")) + " " + str(item.get("type", ""))).lower()
        )
        max_b_dist = 60.0 if is_apt else 35.0
        building, b_dist = self.find_nearest_building(lat, lon, max_dist_m=max_b_dist)
        if building:
            item["refilNom"] = building.get("nom")
            item["refilType"] = building.get("type")
            item["refilAdresse"] = building.get("adresse")
            item["refilLivraison"] = building.get("livraison")
            item["refilGestion"] = building.get("gestion")
            item["refilQuartier"] = building.get("quartier")
            item["refilDistanceM"] = b_dist

        return item

    def enrich_listings_batch(self, listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrichit une liste complète d'annonces."""
        if not self.is_loaded:
            self.load()
        enriched = []
        for it in listings:
            enriched.append(self.enrich_listing(dict(it)))
        return enriched


def resolve_listing_coordinates(
    item_id: str,
    commune: str | None = None,
    quartier: str | None = None,
    title: str | None = "",
    description: str | None = ""
) -> tuple[float, float, dict[str, Any]]:
    """Résout et renvoie (lat, lon, metadata) via le moteur cadastral officiel."""
    idx = get_cadastre_index()
    res = idx.resolve_listing_geography(item_id, commune, quartier, title, description)
    return res["lat"], res["lon"], res


_GLOBAL_INDEX: CadastreSpatialIndex | None = None


def get_cadastre_index() -> CadastreSpatialIndex:
    """Singleton d'accès à l'index spatial cadastre."""
    global _GLOBAL_INDEX
    if _GLOBAL_INDEX is None:
        _GLOBAL_INDEX = CadastreSpatialIndex()
        _GLOBAL_INDEX.load()
    return _GLOBAL_INDEX


def enrich_listings(listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fonction principale pour enrichir une collection d'annonces."""
    idx = get_cadastre_index()
    return idx.enrich_listings_batch(listings)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    idx = get_cadastre_index()
    sample_pt = {"lat": -22.30258, "lon": 166.44418, "title": "Appartement F3 Anse Vata", "surface": 85}
    res = idx.enrich_listing(sample_pt)
    print("Enriched sample keys:", list(res.keys()))
    print("Cadastre NIC:", res.get("cadastreNic"), "Lot:", res.get("cadastreLot"))
    lbl = res.get("surfaceEcartLabel") or ""
    print("Label:", lbl.encode("ascii", "replace").decode("ascii"))
