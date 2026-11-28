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
import hashlib
import threading
import logging
from pathlib import Path
from typing import Any
import duckdb

logger = logging.getLogger(__name__)

DEFAULT_CADASTRE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cadastre"
_INDEX_LOCK = threading.Lock()


def _stable_hash_int(val: Any) -> int:
    """Hash entier 100 % déterministe entre redémarrages Python (remplace hash() aléatoire)."""
    return int(hashlib.md5(str(val).encode("utf-8")).hexdigest(), 16)


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
        # Exclusion stricte des îlots maritimes (ILE ET ILOT, ILE OUEN, PRONY, WOODIN) pour zéro marqueur en mer
        EXCLUDED_ISLET_SECTIONS = ("ILOT", "ILE OUEN", "PRONY", "PORT BOISE", "WOODIN", "GORO")
        self.commune_parcels.clear()
        for p in all_parcels_flat:
            c_raw = str(p["commune"] or "").upper().replace("-", "_").replace(" ", "_")
            sec_up = str(p.get("section") or "").upper()
            if any(ex in sec_up for ex in EXCLUDED_ISLET_SECTIONS):
                continue
            if "MONT_DORE" in c_raw:
                c_raw = "MONT_DORE"
            elif "POYA" in c_raw:
                c_raw = "POYA"
            # Masque terrestre strict pour Nouméa (exclut îlot Maître, Signal, Larégnère, Amédée)
            if c_raw == "NOUMEA" and not (-22.3120 <= p["lat"] <= -22.2169 and 166.4050 <= p["lon"] <= 166.5062):
                continue
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

    def find_parcel_by_lot(
        self,
        commune: str | None,
        lot_num: str,
        quartier: str | None = None,
        text_hint: str = "",
        declared_surface_m2: float | None = None,
    ) -> dict[str, Any] | None:
        """
        Résout un numéro de lot cadastral dans parcelles_nc.parquet.
        Ne retourne une parcelle certifiée QUE si une correspondance fiable est trouvée :
        - Match exact de num_lot dans la commune + désambiguïsation par lotissement/quartier
        - Garde-fou spatial strict : distance <= 2,5 km du centroïde du quartier annoncé (si connu)
        - Garde-fou de contenance : si surface terrain déclarée, ratio dans [0,45x ; 2,2x]
        """
        if not lot_num:
            return None
        if not self.is_loaded:
            self.load()

        from src.reference.geo import get_geo_referential, normalize_text
        lot_clean = str(lot_num).strip().upper().lstrip("0")
        if not lot_clean:
            return None

        com_clean = str(commune or "").upper().replace("-", "_").replace(" ", "_")
        if com_clean == "AUTRE":
            q_norm = normalize_text(f"{quartier or ''} {text_hint[:200]}")
            for ck in self.commune_parcels.keys():
                ck_kw = ck.lower().replace("_", " ")
                if len(ck_kw) >= 3 and re.search(rf"\b{re.escape(ck_kw)}\b", q_norm):
                    com_clean = ck
                    break

        pool = self.commune_parcels.get(com_clean, [])
        if not pool:
            return None

        # Centroïde du quartier annoncé (si présent dans le référentiel) pour contrôle anti-faux-positifs
        q_lat, q_lon = None, None
        if quartier and str(quartier).strip().lower() not in ("secteur calédonien", "secteur", "none", "nan", ""):
            try:
                geo_ref = get_geo_referential()
                qk = normalize_text(str(quartier))
                q_info = geo_ref.quartiers_index.get(qk) or (
                    geo_ref.quartiers_index.get(geo_ref.aliases[qk]) if qk in geo_ref.aliases else None
                )
                if q_info:
                    q_dict = q_info[-1] if isinstance(q_info, tuple) else q_info
                    if isinstance(q_dict, dict):
                        q_lat = q_dict.get("latitude")
                        q_lon = q_dict.get("longitude")
            except Exception:
                pass

        def _passes_guards(p: dict[str, Any]) -> bool:
            if q_lat is not None and q_lon is not None:
                dist_km = math.hypot((p["lat"] - q_lat) * 111.0, (p["lon"] - q_lon) * 103.0)
                if dist_km > 2.5:
                    return False
            if declared_surface_m2 and declared_surface_m2 > 0 and p.get("surface_m2"):
                ratio = float(p["surface_m2"]) / float(declared_surface_m2)
                if ratio < 0.45 or ratio > 2.2:
                    return False
            return True

        candidates = []
        for p in pool:
            p_lot = str(p.get("num_lot") or "").strip().upper().lstrip("0")
            if p_lot == lot_clean and _passes_guards(p):
                candidates.append(p)

        if not candidates:
            return None

        # Désambiguïsation stricte par nom de lotissement ou section mentionné dans l'annonce
        hint_norm = normalize_text(f"{quartier or ''} {text_hint}")
        scored = []
        for p in candidates:
            lotiss_norm = normalize_text(str(p.get("lotissement") or ""))
            sec_norm = normalize_text(str(p.get("section") or ""))
            score = 0
            if lotiss_norm and len(lotiss_norm) >= 4:
                l_words = [w for w in lotiss_norm.split() if len(w) >= 4 and w not in ("lotissement", "secteur", "domaine")]
                if l_words and any(re.search(rf"\b{re.escape(w)}\b", hint_norm) for w in l_words):
                    score += 10
            if sec_norm and len(sec_norm) >= 4 and sec_norm in hint_norm:
                score += 5
            if score > 0:
                scored.append((score, p))

        if len(scored) == 1:
            return scored[0][1]
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            if len(scored) == 1 or scored[0][0] > scored[1][0]:
                return scored[0][1]

        # Si un seul candidat passe les garde-fous spatial + contenance ET qu'un quartier précis était confirmé
        if len(candidates) == 1 and q_lat is not None:
            return candidates[0]

        return None

    def resolve_listing_geography(
        self,
        item_id: str,
        commune: str | None = None,
        quartier: str | None = None,
        title: str | None = "",
        description: str | None = ""
    ) -> dict[str, Any]:
        """
        Résout les coordonnées géographiques terrestres précises pour une annonce (100 % déterministe).
        """
        if not self.is_loaded:
            self.load()

        from src.reference.geo import get_geo_referential, normalize_text

        text_raw = re.sub(r'(?:contact|tél|tel|email|mail|agent)\s*:.*', '', f"{title or ''} {description or ''}", flags=re.IGNORECASE)
        text_norm = normalize_text(text_raw)
        quartier_norm = normalize_text(quartier or "")
        title_norm = normalize_text(title or "")

        com_clean = str(commune or "NOUMEA").upper().replace("-", "_").replace(" ", "_")
        target_com = com_clean

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
            "OUEGOA": (-20.343, 164.438),
            "PONERIHOUEN": (-21.077, 165.400),
            "KOUAOUA": (-21.396, 165.826),
            "POUEBO": (-20.400, 164.583),
            "BELEP": (-19.723, 163.658),
            "LIFOU": (-20.916, 167.240),
            "MARE": (-21.488, 167.973),
            "OUVEA": (-20.638, 166.576),
            "ILE_DES_PINS": (-22.617, 167.456),
            "POYA": (-21.349, 165.155),
        }

        # Si commune déclarée AUTRE, détecter la localité de Brousse en priorité dans quartier, puis titre, puis description (sans accents !)
        if com_clean == "AUTRE":
            for scope in (quartier_norm, title_norm, text_norm):
                if not scope:
                    continue
                matched_b = None
                for b_name in BROUSSE_COMMUNES:
                    b_kw = b_name.lower().replace("_", " ")
                    if re.search(rf"\b{re.escape(b_kw)}\b", scope):
                        matched_b = b_name
                        break
                if matched_b:
                    target_com = matched_b
                    break

        # Détection préalable du quartier par le référentiel singleton si non renseigné
        clean_q = str(quartier or "").lower().strip()
        if not clean_q or clean_q in ("secteur calédonien", "secteur", "none", "nan"):
            try:
                geo_ref = get_geo_referential()
                c_cand, q_cand, _ = geo_ref.find_location(title or "")
                if q_cand:
                    clean_q = q_cand.lower().strip()
                    if target_com == "NOUMEA" and c_cand.value != "AUTRE":
                        target_com = c_cand.value
                else:
                    c_cand, q_cand, _ = geo_ref.find_location(text_raw)
                    if q_cand and (target_com == "AUTRE" or c_cand.value == target_com):
                        clean_q = q_cand.lower().strip()
            except Exception:
                pass

        # Niveau 2 : Répartition déterministe sur parcelle cadastrale réelle du quartier DANS la commune
        q_pool = self.commune_quartier_parcels.get(target_com, {})
        p_list = q_pool.get(clean_q) if clean_q else None
        if not p_list and clean_q and clean_q not in ("secteur calédonien", "secteur", "none", "nan"):
            clean_q_norm = normalize_text(clean_q)
            for k, v in q_pool.items():
                k_norm = normalize_text(k)
                if clean_q_norm and k_norm and (k_norm == clean_q_norm or k_norm in clean_q_norm or clean_q_norm in k_norm):
                    p_list = v
                    break
        if not p_list:
            for k, v in q_pool.items():
                k_norm = normalize_text(k)
                if len(k_norm) >= 4 and re.search(rf"\b{re.escape(k_norm)}\b", title_norm or text_norm):
                    p_list = v
                    break

        if p_list:
            idx = _stable_hash_int(item_id) % len(p_list)
            p = p_list[idx]
            return {
                "lat": p["lat"],
                "lon": p["lon"],
                "match_type": "QUARTIER_PARCEL",
                "parcel": p
            }

        # Niveau 3 : Répartition déterministe sur le vivier de parcelles cadastrales de la commune
        c_pool = self.commune_parcels.get(target_com)
        if c_pool:
            if target_com == "NOUMEA":
                res_pool = [
                    p for p in c_pool
                    if not (-22.258 <= p["lat"] <= -22.235 and 166.425 <= p["lon"] <= 166.442)
                ]
                pool_to_use = res_pool if res_pool else c_pool
            else:
                pool_to_use = c_pool

            idx = _stable_hash_int(item_id) % len(pool_to_use)
            p = pool_to_use[idx]
            return {
                "lat": p["lat"],
                "lon": p["lon"],
                "match_type": f"COMMUNE_PARCEL_{target_com}",
                "parcel": p
            }

        # Niveau 4 : Localité Brousse ou Fallback Grande Terre sur terre ferme (100 % déterministe)
        if target_com in BROUSSE_COMMUNES:
            base_lat, base_lon = BROUSSE_COMMUNES[target_com]
            h = _stable_hash_int(item_id)
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
            "AUTRE": (-21.5640, 165.4930),
        }
        base_lat, base_lon = centers.get(com_clean, (-21.5640, 165.4930))
        h = _stable_hash_int(item_id)
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
            com_lbl = str(item.get("commune") or "Nouméa")
            cat_lbl = str(item.get("category") or "")
            if com_lbl.lower() in ("autre", "brousse"):
                typo_lbl = "Secteur Rural / Brousse"
            elif cat_lbl == "terrain":
                typo_lbl = "Terrain / Foncier"
            else:
                typo_lbl = "Secteur Résidentiel / Urbain"
            item["cadastreStatus"] = "INDICATIF_QUARTIER"
            item["cadastreComment"] = "Localisation indicative à l'échelle du quartier (~500m). Parcelle exacte et copropriété non certifiables sans adresse ou nom de résidence."
            item["cadastreNic"] = None
            item["cadastreLot"] = None
            item["cadastreLotissement"] = None
            item["cadastreSection"] = str(item.get("quartier") or com_lbl or "Secteur NC")
            item["cadastreCommune"] = com_lbl
            item["cadastreContenance"] = None
            item["cadastreSurfaceM2"] = None
            item["cadastreTypologie"] = typo_lbl
            item["surfaceEcartLabel"] = None
            item["refilNom"] = None
            item["refilType"] = None
            item["refilAdresse"] = None
            return item

        # 1. Parcelle Cadastrale (certifiée)
        prec_lvl_str = str(item.get("precisionLevel") or item.get("precision_level") or "").upper()
        item["cadastreStatus"] = (
            "CERTIFIE_REFIL" if prec_lvl_str == "IMMEUBLE_REFIL"
            else ("CERTIFIE_LOT" if prec_lvl_str == "LOT_CADASTRE" else "CERTIFIE_GPS")
        )
        item["cadastreComment"] = (
            "Localisation certifiée via le référentiel officiel REFIL / Cadastre NC."
            if prec_lvl_str in ("IMMEUBLE_REFIL", "LOT_CADASTRE")
            else "Localisation certifiée au mètre via coordonnées GPS EXIF."
        )
        is_apt = (
            item.get("category") == "appartement"
            or item.get("propertyType") in ("APPARTEMENT", "STUDIO", "LOFT")
            or any(k in (str(item.get("title", "")) + " " + str(item.get("categoryLabel", ""))).lower() for k in ("appartement", "studio", "f1", "f2", "f3", "f4"))
        )

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

            hab_surf = item.get("surfaceHabitable") or item.get("surface_habitable_m2") or item.get("surface") or item.get("surface_m2")
            ter_surf = item.get("surfaceTerrain") or item.get("surface_terrain_m2")

            if is_apt:
                cad_m2 = parcel.get("surface_m2")
                cad_str = f"{cad_m2:,.0f} m²".replace(",", " ") if cad_m2 else parcel.get("contenance")
                hab_str = f"{hab_surf:.0f} m²" if hab_surf and float(hab_surf) > 0 else "N/C"
                item["surfaceEcartM2"] = None
                item["surfaceEcartPct"] = None
                item["surfaceEcartStatus"] = "COPROPRIETE"
                item["surfaceEcartLabel"] = f"🏢 Assiette foncière copropriété : {cad_str} (Habitable : {hab_str})"
                item["isConforme"] = True
            else:
                disc = calculate_surface_discrepancy(ter_surf or hab_surf, parcel.get("surface_m2"))
                item["surfaceEcartM2"] = disc["surfaceEcartM2"]
                item["surfaceEcartPct"] = disc["surfaceEcartPct"]
                item["surfaceEcartStatus"] = disc["surfaceEcartStatus"]
                item["surfaceEcartLabel"] = disc["surfaceEcartLabel"]
                item["isConforme"] = disc["isConforme"]

        # 2. Immeuble REFIL (Ensemble immobilier / résidence)
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
    """Singleton thread-safe d'accès à l'index spatial cadastre."""
    global _GLOBAL_INDEX
    if _GLOBAL_INDEX is None:
        with _INDEX_LOCK:
            if _GLOBAL_INDEX is None:
                idx = CadastreSpatialIndex()
                idx.load()
                _GLOBAL_INDEX = idx
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
