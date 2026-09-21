import json
import re
import unicodedata
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

from src.config.settings import REFERENCE_DATA_DIR
from src.models.listing import Commune


def normalize_text(text: str) -> str:
    """Supprime les accents, met en minuscules et nettoie la ponctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("utf-8")
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    return " ".join(text.lower().split())


class GeoReferential:
    def __init__(self, json_path: Optional[Path] = None):
        self.path = json_path or (REFERENCE_DATA_DIR / "referentiel_grand_noumea.json")
        self.data = self._load()
        self._build_index()

    def _load(self) -> Dict[str, Any]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_index(self):
        """Indexe les communes et quartiers pour une détection rapide et sans faille."""
        self.communes_map: Dict[str, Commune] = {
            "noumea": Commune.NOUMEA,
            "dumbea": Commune.DUMBEA,
            "mont dore": Commune.MONT_DORE,
            "le mont dore": Commune.MONT_DORE,
            "paita": Commune.PAITA,
        }

        # Structure : { normalized_quartier_name: (official_name, commune_enum, quartier_dict) }
        self.quartiers_index: Dict[str, Tuple[str, Commune, Dict[str, Any]]] = {}

        # Communes de Brousse et Îles
        self.brousse_communes = [
            "bourail", "la foa", "boulouparis", "pouembout", "kone", "voh",
            "koumac", "kaala gomen", "poindimie", "touho", "hienghene", "houailou",
            "canala", "thio", "yate", "moindou", "farino", "sarramea", "poum",
            "lifou", "mare", "ouvea", "ile des pins", "poya"
        ]

        # Alias fréquents dans les annonces locales
        self.aliases: Dict[str, str] = {
            # Acronymes & abréviations
            "vdc": "vallee des colons",
            "bdc": "baie des citrons",
            "anse vata": "anse vata",
            "val plaisance": "val plaisance",
            "dsm": "dumbea sur mer",
            "dumbea sur mer": "dumbea sur mer",
            "tina sur mer": "tina",
            "presquile de tina": "tina",
            "pont des francais": "pont des francais",
            "vallon dore": "vallon dore",
            "la coulee": "la coulee",
            "faubourg": "faubourg blanchot",
            "faubourg blanchot": "faubourg blanchot",
            "motor pool": "motor pool",
            "port plaisance": "orphelinat",
            "orphelinat": "orphelinat",
            "baie de l orphelinat": "orphelinat",
            "baie de lorphelinat": "orphelinat",
            "val boise": "val boise",
            # PK (Kilomètres Nouméa)
            "pk 6": "pk6",
            "p k 6": "pk6",
            "pk6": "pk6",
            "p k6": "pk6",
            "pk 7": "pk7",
            "p k 7": "pk7",
            "pk7": "pk7",
            "p k7": "pk7",
            "pk 4": "pk4",
            "p k 4": "pk4",
            "pk4": "pk4",
            "p k4": "pk4",
            # Portes de Fer
            "portes de fer": "portes de fer",
            "porte de fer": "portes de fer",
            # Magenta
            "haut magenta": "haut magenta",
            "magenta aerodrome": "magenta",
            "aerodrome magenta": "magenta",
            "plage de magenta": "magenta",
            # Quartier Latin & Centre
            "quartier latin": "quartier latin",
            "centre ville": "centre ville",
            "place des cocotiers": "centre ville",
            # Presqu'île de Ducos & Hauteurs
            "mont coffyn": "faubourg blanchot",
            "mont coffees": "faubourg blanchot",
            "kamere": "kamere",
            "tindu": "tindu",
            "nouville": "nouville",
            "montravel": "montravel",
            # Dumbéa & Païta
            "val fleuri": "val fleuri",
            "plaine de koe": "plaine de koe",
            "koe": "plaine de koe",
            "tontouta": "tontouta",
            "la tontouta": "tontouta",
            "naia": "naia",
        }

        for com_key, com_info in self.data.get("communes", {}).items():
            commune_enum = Commune[com_key]
            for q in com_info.get("quartiers", []):
                official_name = q["nom"]
                norm_q = normalize_text(official_name)
                self.quartiers_index[norm_q] = (official_name, commune_enum, q)

                # Variantes sans tiret / pluriel
                norm_alt = norm_q.replace("des ", "").replace("les ", "").replace("sur ", "")
                if norm_alt != norm_q:
                    self.quartiers_index[norm_alt] = (official_name, commune_enum, q)

    def find_location(self, text: str) -> Tuple[Commune, Optional[str], Optional[Dict[str, Any]]]:
        """
        Détecte la commune et le quartier depuis une chaîne de texte
        (ex: titre, localisation déclarée ou description).
        Priorité absolue aux mentions entre parenthèses dans le titre 'à Commune (Quartier)'.
        Retourne (Commune, nom_officiel_quartier, metadata_quartier).
        """
        if not text:
            return Commune.AUTRE, None, None

        # 1. Détection prioritaire dans les parenthèses du format classique calédonien
        # Ex: "Maison F5 à Nouméa (P.k. 6)", "Appartement F3 à Nouméa (Quartier latin)"
        paren_match = re.search(r'\(([^)]+)\)', text)
        if paren_match:
            candidate = paren_match.group(1).strip()
            norm_cand = normalize_text(candidate)
            # Tester alias direct
            if norm_cand in self.aliases:
                target = self.aliases[norm_cand]
                if target in self.quartiers_index:
                    off_name, com_enum, q_info = self.quartiers_index[target]
                    return com_enum, off_name, q_info
            # Tester quartier index direct
            if norm_cand in self.quartiers_index:
                off_name, com_enum, q_info = self.quartiers_index[norm_cand]
                return com_enum, off_name, q_info
            # Tester si le candidat contient un alias ou quartier
            for a_key, target in self.aliases.items():
                if re.search(rf"\b{re.escape(a_key)}\b", norm_cand):
                    if target in self.quartiers_index:
                        off_name, com_enum, q_info = self.quartiers_index[target]
                        return com_enum, off_name, q_info

        norm = normalize_text(text)

        # 2. Vérifier les alias spécifiques d'abord (du plus long au plus court)
        sorted_aliases = sorted(self.aliases.items(), key=lambda x: len(x[0]), reverse=True)
        for alias, target_q in sorted_aliases:
            if re.search(rf"\b{re.escape(alias)}\b", norm):
                if target_q in self.quartiers_index:
                    official_name, commune_enum, q_info = self.quartiers_index[target_q]
                    return commune_enum, official_name, q_info

        # 3. Chercher les quartiers répertoriés (du plus long au plus court pour éviter les faux positifs)
        sorted_quartiers = sorted(self.quartiers_index.keys(), key=len, reverse=True)
        for q_key in sorted_quartiers:
            if re.search(rf"\b{re.escape(q_key)}\b", norm):
                official_name, commune_enum, q_info = self.quartiers_index[q_key]
                return commune_enum, official_name, q_info

        # 4. Si aucun quartier n'est identifié, chercher la commune du Grand Nouméa
        for com_key, com_enum in self.communes_map.items():
            if re.search(rf"\b{re.escape(com_key)}\b", norm):
                return com_enum, None, None

        # 5. Chercher une commune de Brousse / Îles
        for b_com in self.brousse_communes:
            if re.search(rf"\b{re.escape(b_com)}\b", norm):
                return Commune.AUTRE, b_com.title(), None

        return Commune.AUTRE, None, None

    def get_quartier_info(self, commune: Commune, quartier_nom: str) -> Optional[Dict[str, Any]]:
        """Récupère les caractéristiques (standing, secteur, coordonnées) d'un quartier."""
        com_key = commune.value
        com_dict = self.data.get("communes", {}).get(com_key, {})
        for q in com_dict.get("quartiers", []):
            if q["nom"].lower() == quartier_nom.lower():
                return q
        return None
