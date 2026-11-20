"""Static domain constants for the synthetic marketplace.

Everything here is deliberately data, not code: shops, categories, promo
calendar and the fake catalogue vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class Shop:
    shop_id: str
    shop_name: str
    marketplace: str
    currency: str
    timezone: str
    fx_rate: float  # local currency -> CNY, baseline


@dataclass(frozen=True, slots=True)
class Category:
    code: str
    name: str
    price_mid_usd: float
    margin_target: float
    refund_rate: float
    referral_rate: float
    fba_fee_usd: float
    weight_kg: float


SHOPS: tuple[Shop, ...] = (
    Shop("US01", "JoyTop US", "US", "USD", "America/Los_Angeles", 7.15),
    Shop("DE01", "JoyTop DE", "DE", "EUR", "Europe/Berlin", 7.70),
    Shop("UK01", "JoyTop UK", "UK", "GBP", "Europe/London", 9.15),
)

CATEGORIES: tuple[Category, ...] = (
    Category("home", "Home & Kitchen", 25.0, 0.52, 0.040, 0.15, 3.20, 0.9),
    Category("electronics", "Electronics Accessories", 35.0, 0.48, 0.060, 0.15, 3.60, 0.4),
    Category("outdoor", "Outdoor & Sports", 60.0, 0.48, 0.050, 0.15, 5.10, 1.8),
    Category("baby", "Baby & Toddler", 18.0, 0.50, 0.030, 0.15, 2.80, 0.7),
    Category("apparel", "Apparel", 22.0, 0.60, 0.120, 0.17, 3.40, 0.5),
    Category("pet", "Pet Supplies", 15.0, 0.50, 0.040, 0.15, 2.60, 0.8),
)

CATEGORY_BY_CODE = {c.code: c for c in CATEGORIES}

BRANDS: tuple[str, ...] = (
    "NordHome",
    "VoltEdge",
    "TrailPeak",
    "LittleNest",
    "UrbanThread",
    "PawJoy",
    "ClearDay",
    "BrightPath",
)

# product nouns per category, used to build plausible titles
PRODUCTS: dict[str, tuple[str, ...]] = {
    "home": ("Bamboo Cutting Board", "Ceramic Mug Set", "Storage Basket", "Linen Throw Pillow"),
    "electronics": ("USB-C Hub", "Wireless Charger", "Laptop Stand", "Bluetooth Adapter"),
    "outdoor": ("Camping Lantern", "Insulated Bottle", "Trail Backpack", "Trekking Poles"),
    "baby": ("Silicone Bib", "Baby Monitor Mount", "Nursing Pillow", "Bottle Warmer"),
    "apparel": ("Merino Base Layer", "Rain Shell", "Cotton Hoodie", "Compression Socks"),
    "pet": ("Slow Feeder Bowl", "Cat Tunnel", "Dog Harness", "Pet Grooming Glove"),
}

COLORS: tuple[str, ...] = ("Black", "White", "Grey", "Navy", "Sand", "Olive")

START_DATE = date(2024, 6, 1)
DAYS = 90

# (start_day_index, end_day_index_exclusive, label)
PROMO_WINDOWS: tuple[tuple[int, int, str], ...] = (
    (30, 37, "Summer Sale"),
    (62, 69, "Prime Day"),
)

FULFILLMENT_METHODS: tuple[str, ...] = ("FBA", "FBA", "FBA", "FBM")

REFUND_REASONS: tuple[str, ...] = (
    "size_mismatch",
    "defective",
    "late_delivery",
    "buyer_remorse",
    "not_received",
)

WAREHOUSES: tuple[str, ...] = ("FBA", "FBA", "3PL")
