"""
config/universe.py — Static sector mapping for the equities universe.

SECTOR_MAP:  symbol → GICS-aligned sector string (used by SectorExposureTracker)
SECTOR_CAPS: sector → max simultaneous open positions (long + short combined)

Default cap is MAX_SECTOR_EXPOSURE = 3.
MACRO OVERRIDES (geopolitical headline risk):
  Defense  → 1  (GD, LDOS — Iran/tariff headlines move these hard)
  Energy   → 1  (SLB, EXE — oil supply shock risk)
  Semiconductors → 2  (extreme intra-sector correlation; one news event moves all 6)

This dict is intentionally static — no runtime API calls, zero event-loop latency.
Add new symbols here before adding them to equities_engine.SYMBOLS.
"""

# ── Sector assignments ─────────────────────────────────────────────────────────
SECTOR_MAP: dict[str, str] = {
    # ── Technology (Software / SaaS / Data) ───────────────────────────────────
    "NOW": "Technology",
    "PTC": "Technology",
    "VRSK": "Technology",
    "ZS": "Technology",
    "GEN": "Technology",
    "NTAP": "Technology",
    "CRM": "Technology",
    "DDOG": "Technology",
    "WDAY": "Technology",
    "INTU": "Technology",
    "SMCI": "Technology",
    "PLTR": "Technology",
    "TTD": "Technology",
    "JKHY": "Technology",
    "FICO": "Technology",
    "ORCL": "Technology",
    "TEAM": "Technology",
    "SNOW": "Technology",
    "CRWD": "Technology",
    "PAYX": "Technology",
    "VRSN": "Technology",
    "LITE": "Technology",
    "HPE": "Technology",
    "DELL": "Technology",
    "KEYS": "Technology",
    "COHR": "Technology",
    "CSCO": "Technology",
    "GRMN": "Technology",
    "JBL": "Technology",
    "STX": "Technology",
    "SNDK": "Technology",
    "WDC": "Technology",
    # ── Semiconductors (separate — extreme intra-sector correlation) ───────────
    "INTC": "Semiconductors",
    "MRVL": "Semiconductors",
    "KLAC": "Semiconductors",
    "MPWR": "Semiconductors",
    "LRCX": "Semiconductors",
    "TER": "Semiconductors",
    # ── Industrials ───────────────────────────────────────────────────────────
    "CTAS": "Industrials",
    "CPRT": "Industrials",
    "J": "Industrials",
    "FAST": "Industrials",
    "ETN": "Industrials",
    "HUBB": "Industrials",
    "GLW": "Industrials",
    "Q": "Industrials",
    "WAB": "Industrials",
    "FIX": "Industrials",
    "GEV": "Industrials",
    "FTV": "Industrials",
    "ODFL": "Industrials",
    "EME": "Industrials",
    "VRT": "Industrials",
    "GWW": "Industrials",
    "CSX": "Industrials",
    "CMI": "Industrials",
    "CAT": "Industrials",
    # ── Utilities ─────────────────────────────────────────────────────────────
    "ETR": "Utilities",
    "PPL": "Utilities",
    "NI": "Utilities",
    "SRE": "Utilities",
    "CMS": "Utilities",
    "PNW": "Utilities",
    "WEC": "Utilities",
    "LNT": "Utilities",
    "FE": "Utilities",
    "EVRG": "Utilities",
    "DTE": "Utilities",
    "EIX": "Utilities",
    "ED": "Utilities",
    "CNP": "Utilities",
    "DUK": "Utilities",
    # ── Consumer Discretionary ────────────────────────────────────────────────
    "NKE": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "DLTR": "Consumer Discretionary",
    "DG": "Consumer Discretionary",
    "LEN": "Consumer Discretionary",
    "SYY": "Consumer Discretionary",
    "ULTA": "Consumer Discretionary",
    "TSCO": "Consumer Discretionary",
    "TJX": "Consumer Discretionary",
    "COST": "Consumer Discretionary",
    "RL": "Consumer Discretionary",
    "HLT": "Consumer Discretionary",
    "ROST": "Consumer Discretionary",
    "TGT": "Consumer Discretionary",
    "MAR": "Consumer Discretionary",
    # ── Consumer Staples ──────────────────────────────────────────────────────
    "HRL": "Consumer Staples",
    "SJM": "Consumer Staples",
    "CPB": "Consumer Staples",
    "MKC": "Consumer Staples",
    "GIS": "Consumer Staples",
    "EL": "Consumer Staples",
    "PM": "Consumer Staples",
    "STZ": "Consumer Staples",
    "TSN": "Consumer Staples",
    # ── Healthcare ────────────────────────────────────────────────────────────
    "PODD": "Healthcare",
    "ISRG": "Healthcare",
    "COR": "Healthcare",
    # ── Defense ───────────────────────────────────────────────────────────────
    "GD": "Defense",
    "LDOS": "Defense",
    # ── Energy ────────────────────────────────────────────────────────────────
    "EXE": "Energy",
    "SLB": "Energy",
    # ── Financials ────────────────────────────────────────────────────────────
    "GPN": "Financials",
    "STT": "Financials",
    "GL": "Financials",
    "NTRS": "Financials",
    # ── Real Estate ───────────────────────────────────────────────────────────
    "CSGP": "Real Estate",
    "SBAC": "Real Estate",
    "DLR": "Real Estate",
    "EQIX": "Real Estate",
    # ── Materials ─────────────────────────────────────────────────────────────
    "MOS": "Materials",
    "CTVA": "Materials",
    "FCX": "Materials",
    # ── Communication Services ────────────────────────────────────────────────
    "NFLX": "Communication Services",
    "LYV": "Communication Services",
    # ── Precious Metals (commodity ETFs) ──────────────────────────────────────
    # PPLT/PALL/CPER/URNM excluded — fail MIN_ADV=1M filter
    "GLD": "Precious Metals",  # SPDR Gold Shares (~$438, ADV ~7M)
    "SLV": "Precious Metals",  # iShares Silver Trust (~$68, ADV ~16M)
    # ── Energy Commodities ────────────────────────────────────────────────────
    # ⚠️  MACRO OVERRIDE — Iran war risk (2026-04): crude oil is a long-side
    # geopolitical hedge. Cap=1; do NOT raise while Iran tensions elevated.
    # UNG excluded (price ~$11, fails $20 floor).
    "USO": "Energy ETF",  # United States Oil Fund (~$127, ADV ~5M)
    # ── Nuclear Energy ────────────────────────────────────────────────────────
    # ⚠️  MACRO OVERRIDE — Iran nuclear program makes uranium politically
    # sensitive. Cap=1. URNM excluded (ADV ~300K fails filter).
    "URA": "Nuclear Energy",  # Global X Uranium ETF (~$51, ADV ~1.5M)
    # ── Russell 3000 additions ────────────────────────────────────────────────
    "BKNG": "Consumer Discretionary",
    "AXON": "Industrials",
    "VEEV": "Healthcare",
    "ADBE": "Technology",
    "HUBS": "Technology",
    "ADSK": "Technology",
    "BSX": "Healthcare",
    "MDB": "Technology",
    "ABT": "Healthcare",
    "ADP": "Industrials",
    "NTNX": "Technology",
    "GWRE": "Technology",
    "MANH": "Technology",
    "CAR": "Industrials",
    "PVH": "Consumer Discretionary",
    "FLEX": "Technology",
    "SNX": "Technology",
    "BK": "Financials",
    "C": "Financials",
    "BURL": "Consumer Discretionary",
    "CROX": "Consumer Discretionary",
    "HOG": "Consumer Discretionary",
}

# ── Per-sector position caps (long + short combined) ──────────────────────────
MAX_SECTOR_EXPOSURE: int = 3  # default for unlisted sectors

SECTOR_CAPS: dict[str, int] = {
    "Technology": 3,
    "Semiconductors": 2,  # extreme intra-sector correlation
    "Industrials": 3,
    "Utilities": 3,
    "Consumer Discretionary": 3,
    "Consumer Staples": 3,
    "Healthcare": 3,
    "Defense": 1,  # MACRO OVERRIDE — geopolitical headline risk
    "Energy": 1,  # MACRO OVERRIDE — oil supply shock exposure
    "Energy ETF": 1,  # MACRO OVERRIDE — Iran war / crude oil spike risk
    "Nuclear Energy": 1,  # MACRO OVERRIDE — Iran nuclear program sensitivity
    "Precious Metals": 2,  # GLD + SLV; safe-haven inflow during geopolitical stress
    "Financials": 3,
    "Real Estate": 3,
    "Materials": 3,
    "Communication Services": 3,
}
