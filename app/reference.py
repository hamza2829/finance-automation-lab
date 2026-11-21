"""Static master data: cost centers, booking categories, vendors.

All names, people and e-mail addresses are fictional (``example.com`` is a
reserved domain).
"""
from __future__ import annotations

# category -> (general ledger account, default cost center, label)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "IT_SOFTWARE": ("6340", "CC-100", "Software licences and cloud services"),
    "IT_HARDWARE": ("6350", "CC-100", "IT hardware below capitalisation limit"),
    "CONSULTING": ("6800", "CC-500", "External consulting and audit"),
    "MARKETING": ("6600", "CC-200", "Advertising, events and print"),
    "TRAVEL": ("6670", "CC-600", "Travel, hotels and transport"),
    "OFFICE_SUPPLIES": ("6815", "CC-300", "Office supplies"),
    "FACILITIES": ("6330", "CC-300", "Cleaning, security, logistics, rent side costs"),
    "TRAINING": ("6821", "CC-400", "Training and further education"),
}

# id -> (name, manager name, manager e-mail, annual budget in cents)
COST_CENTERS: dict[str, tuple[str, str, str, int]] = {
    "CC-100": ("IT", "Alex Brandt", "alex.brandt@example.com", 48_000_000),
    "CC-200": ("Marketing", "Sam Keller", "sam.keller@example.com", 36_000_000),
    "CC-300": ("Facilities", "Robin Vogt", "robin.vogt@example.com", 30_000_000),
    "CC-400": ("HR and Training", "Kim Lorenz", "kim.lorenz@example.com", 12_000_000),
    "CC-500": ("Finance", "Jo Albers", "jo.albers@example.com", 15_000_000),
    "CC-600": ("Sales", "Mika Hartmann", "mika.hartmann@example.com", 24_000_000),
}

# second approval level after escalation
FINANCE_LEAD = "finance.lead@example.com"

# id -> (name, typical category or None for mixed vendors, booking amount field)
VENDORS: dict[str, tuple[str, str | None, str]] = {
    "V-001": ("Kontor Software GmbH", "IT_SOFTWARE", "gross"),
    "V-002": ("Cloudhafen Hosting AG", "IT_SOFTWARE", "gross"),
    "V-003": ("Bytewerk Hardware AG", "IT_HARDWARE", "gross"),
    "V-004": ("Clearline Consulting", "CONSULTING", "gross"),
    "V-005": ("Datenfluss Analytics", "CONSULTING", "gross"),
    "V-006": ("Funkelstern Media", "MARKETING", "gross"),
    "V-007": ("Messebau Kranich", "MARKETING", "gross"),
    "V-008": ("Reisewelt Business Travel", "TRAVEL", "gross"),
    "V-009": ("Hotel Am Hafen", "TRAVEL", "gross"),
    "V-010": ("Papierhaus Meier", "OFFICE_SUPPLIES", "gross"),
    "V-011": ("Glanzwerk Facility Services", "FACILITIES", "gross"),
    # deliberately misconfigured interface mapping: books net instead of gross
    "V-012": ("Nordwind Logistik", "FACILITIES", "net"),
    "V-013": ("Lernpfad Akademie", "TRAINING", "gross"),
    "V-014": ("Sicherheitsdienst Wacht", "FACILITIES", "gross"),
    "V-015": ("Druckerei Blau", "MARKETING", "gross"),
    "V-016": ("Allround Services KG", None, "gross"),
}


def account_for(category: str) -> str:
    return CATEGORIES[category][0]


def default_cost_center(category: str) -> str:
    return CATEGORIES[category][1]
