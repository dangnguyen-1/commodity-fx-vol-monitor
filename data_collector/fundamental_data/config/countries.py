ISO3_TO_M49 = {
    "USA": "842",
    "CHN": "156",
    "DEU": "276",
    "JPN": "392",
    "GBR": "826",
    "FRA": "251",
    "ITA": "381",
    "CAN": "124",
    "KOR": "410",
    "AUS": "036",
    "BRA": "076",
    "IND": "356",
    "RUS": "643",
    "SAU": "682",
    "ZAF": "710",
    "TUR": "792",
    "MEX": "484",
    "IDN": "360",
    "ARG": "032",
    "NLD": "528",
    "BEL": "056",
    "SWE": "752",
    "NOR": "578",
    "POL": "616",
    "ESP": "724",
    "ARE": "784",
    "EGY": "818",
    "ETH": "231",
    "IRN": "364",
    "SGP": "702",

    # Commodity exporters, added because the thirty above were picked as
    # major economies and the dashboard tracks currencies. Ten of the
    # nineteen currencies in dashboard/data/fx.py had no trade rows at all,
    # so Chile and Peru were mapped to copper and silver with nothing behind
    # them, and the trade-flow and country-exposure views were empty for
    # them.
    #
    # These are the producers rather than the clearers. Without them the
    # cocoa series describes Dutch and American grinders, and the uranium
    # series describes enriched fuel moving around western Europe, because
    # Ghana and Kazakhstan were not being asked.
    "COL": "170",   # oil, coffee
    "CHL": "152",   # copper
    "PER": "604",   # copper, silver
    "COD": "180",   # copper, cobalt
    "ZMB": "894",   # copper
    "GHA": "288",   # gold, cocoa
    "UKR": "804",   # wheat, corn
    "KAZ": "398",   # uranium, wheat
    "PRY": "600",   # soybeans
    # CHE was requested here and returned 5,226 rows, every one NULL.
    # Switzerland refines most of the world's gold and reports none of
    # it, because its customs statistics exclude precious metals. It
    # was dropped rather than left producing empty rows.
}

COUNTRIES = list(ISO3_TO_M49.keys())