CANDIDATE_ASSET_FX_MAPPINGS = [
    # Energy
    {"commodity": "Brent Oil", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Crude Oil", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Natural Gas", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Heating Oil", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "energy_exporter", "priority": "secondary"},
    {"commodity": "LNG", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "LNG", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "secondary"},
    {"commodity": "Gasoline", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "energy_exporter", "priority": "secondary"},
    {"commodity": "Coal", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},

    # Metals
    {"commodity": "Iron Ore", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Iron Ore", "currency": "BRL", "fx_symbol": "FX_IDC:BRLUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "secondary"},
    {"commodity": "Copper", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "exporter_china_demand", "priority": "primary"},
    {"commodity": "Gold", "currency": "USD", "fx_symbol": "FX:EURUSD", "expected_sign": 1, "relationship_type": "usd_inverse_macro", "priority": "primary"},
    {"commodity": "Gold", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "secondary"},
    {"commodity": "Silver", "currency": "USD", "fx_symbol": "FX:EURUSD", "expected_sign": 1, "relationship_type": "usd_inverse_macro", "priority": "secondary"},
    {"commodity": "Aluminum", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "industrial_metal", "priority": "experimental"},
    {"commodity": "Nickel", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "industrial_metal", "priority": "experimental"},
    {"commodity": "Zinc", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "industrial_metal", "priority": "experimental"},
    {"commodity": "Platinum", "currency": "USD", "fx_symbol": "FX:EURUSD", "expected_sign": 1, "relationship_type": "usd_inverse_macro", "priority": "experimental"},
    {"commodity": "Palladium", "currency": "USD", "fx_symbol": "FX:EURUSD", "expected_sign": 1, "relationship_type": "usd_inverse_macro", "priority": "experimental"},
    {"commodity": "Lithium", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Lithium Hydroxide", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Uranium", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "resource_exporter", "priority": "experimental"},
    {"commodity": "Uranium", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "resource_exporter", "priority": "experimental"},

    # Agriculture
    {"commodity": "Soybeans", "currency": "BRL", "fx_symbol": "FX_IDC:BRLUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Corn", "currency": "BRL", "fx_symbol": "FX_IDC:BRLUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "secondary"},
    {"commodity": "Wheat", "currency": "USD", "fx_symbol": "FX:EURUSD", "expected_sign": 1, "relationship_type": "usd_inverse_macro", "priority": "experimental"},
    {"commodity": "Sugar", "currency": "BRL", "fx_symbol": "FX_IDC:BRLUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Coffee", "currency": "BRL", "fx_symbol": "FX_IDC:BRLUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
    {"commodity": "Cotton", "currency": "USD", "fx_symbol": "FX:EURUSD", "expected_sign": 1, "relationship_type": "usd_inverse_macro", "priority": "experimental"},
    {"commodity": "Cocoa", "currency": "GBP", "fx_symbol": "FX:GBPUSD", "expected_sign": 1, "relationship_type": "market_center_experimental", "priority": "experimental"},
    {"commodity": "Cattle", "currency": "AUD", "fx_symbol": "FX:AUDUSD", "expected_sign": 1, "relationship_type": "agri_exporter", "priority": "experimental"},
    {"commodity": "Lumber", "currency": "CAD", "fx_symbol": "DERIVED:CADUSD", "expected_sign": 1, "relationship_type": "exporter", "priority": "primary"},
]