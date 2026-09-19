from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field, computed_field

from src.config.settings import XPF_TO_EUR_RATE


class TransactionType(str, Enum):
    VENTE = "VENTE"
    LOCATION = "LOCATION"


class PropertyType(str, Enum):
    APPARTEMENT = "APPARTEMENT"
    MAISON_VILLA = "MAISON_VILLA"
    TERRAIN = "TERRAIN"
    IMMEUBLE = "IMMEUBLE"
    LOCAL_COMMERCIAL = "LOCAL_COMMERCIAL"
    DOCK = "DOCK"
    AUTRE = "AUTRE"


class Commune(str, Enum):
    NOUMEA = "NOUMEA"
    DUMBEA = "DUMBEA"
    MONT_DORE = "MONT_DORE"
    PAITA = "PAITA"
    AUTRE = "AUTRE"


class RawListing(BaseModel):
    """Représente une annonce brute extraite telle quelle du portail source."""
    source: str
    source_id: str
    url: str
    title: str
    description: Optional[str] = ""
    raw_price: Optional[str] = None
    raw_surface: Optional[str] = None
    raw_rooms: Optional[str] = None
    raw_location: Optional[str] = None
    transaction_type_declared: Optional[str] = None
    property_type_declared: Optional[str] = None
    agency_name: Optional[str] = None
    image_url: Optional[str] = None
    images_json: Optional[str] = None
    facilities: Optional[List[str]] = None
    published_at: Optional[datetime] = None
    created_at_declared: Optional[datetime] = None
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CleanedListing(BaseModel):
    """Représente une annonce nettoyée, standardisée et enrichie pour analyse."""
    id: str  # Hash unique (ex: immo_nc_12345)
    source: str
    source_id: str
    url: str
    title: str
    description: str = ""

    # Typologies normalisées
    transaction_type: TransactionType
    property_type: PropertyType
    commune: Commune
    quartier: Optional[str] = None

    # Données financières
    price_xpf: int = Field(ge=0, description="Prix exprimé en Francs Pacifique (XPF)")
    initial_price_xpf: Optional[int] = Field(default=None, ge=0, description="Prix initial lors de la première observation")
    charges_mensuelles_xpf: Optional[int] = Field(default=None, ge=0)

    # Surfaces (m²)
    surface_habitable_m2: Optional[float] = Field(default=None, ge=0)
    surface_terrain_m2: Optional[float] = Field(default=None, ge=0)
    surface_terrasse_m2: Optional[float] = Field(default=None, ge=0)

    # Agencement
    rooms: Optional[int] = Field(default=None, ge=1, description="Nombre de pièces (ex: F3 -> 3)")
    bedrooms: Optional[int] = Field(default=None, ge=0)
    bathrooms: Optional[int] = Field(default=None, ge=0)
    parkings: Optional[int] = Field(default=None, ge=0)

    # Attributs qualitatifs / Feature Engineering
    has_sea_view: bool = False
    has_pool: bool = False
    has_air_conditioning: bool = False
    is_secured: bool = False
    is_furnished: Optional[bool] = None
    standing_estime: Optional[str] = None

    # Données temporelles et métadonnées
    agency_name: Optional[str] = None
    image_url: Optional[str] = None
    images_json: Optional[str] = None
    published_at: Optional[datetime] = None
    first_seen_at: Optional[datetime] = None
    last_price_change_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True

    # Géolocalisation haute précision & Datation photos
    lat_precise: Optional[float] = None
    lon_precise: Optional[float] = None
    precision_score: Optional[int] = None
    precision_level: Optional[str] = None
    precision_detail: Optional[str] = None
    photo_date_taken: Optional[datetime] = None
    photo_age_months: Optional[int] = None

    @computed_field
    @property
    def price_eur(self) -> float:
        """Conversion automatique en Euros."""
        return round(self.price_xpf / XPF_TO_EUR_RATE, 2)

    @computed_field
    @property
    def prix_m2_habitable_xpf(self) -> Optional[float]:
        """Calcul automatique du prix au m² habitable."""
        if self.surface_habitable_m2 and self.surface_habitable_m2 > 0:
            return round(self.price_xpf / self.surface_habitable_m2, 2)
        return None

    @computed_field
    @property
    def prix_m2_habitable_eur(self) -> Optional[float]:
        """Calcul du prix au m² habitable en Euros."""
        if self.prix_m2_habitable_xpf:
            return round(self.prix_m2_habitable_xpf / XPF_TO_EUR_RATE, 2)
        return None

    @computed_field
    @property
    def room_type(self) -> Optional[str]:
        """Typologie immobilière standard (Studio, F1, F2, F3, F4, F5+)."""
        if not self.rooms or self.rooms < 1 or self.rooms > 15:
            return None
        if self.property_type in (PropertyType.TERRAIN, PropertyType.DOCK, PropertyType.LOCAL_COMMERCIAL, PropertyType.IMMEUBLE):
            return None
        if self.rooms == 1:
            title_desc = f"{self.title} {self.description}".lower()
            if "studio" in title_desc:
                return "Studio"
            return "F1"
        if self.rooms >= 5:
            return f"F{self.rooms}"
        return f"F{self.rooms}"

    @computed_field
    @property
    def room_type_code(self) -> Optional[str]:
        """Code de filtre typologie ('1', '2', '3', '4', '5+')."""
        if not self.rooms or self.rooms < 1 or self.rooms > 15:
            return None
        if self.property_type in (PropertyType.TERRAIN, PropertyType.DOCK, PropertyType.LOCAL_COMMERCIAL, PropertyType.IMMEUBLE):
            return None
        if self.rooms >= 5:
            return "5+"
        return str(self.rooms)

    @computed_field
    @property
    def furnished_label(self) -> Optional[str]:
        """Libellé explicite de l'ameublement (Meublé, Non meublé, ou None)."""
        if self.is_furnished is True:
            return "Meublé"
        elif self.is_furnished is False:
            return "Non meublé"
        return None

