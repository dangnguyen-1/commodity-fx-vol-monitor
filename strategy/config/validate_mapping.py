from strategy.config.asset_fx_mapping import CANDIDATE_ASSET_FX_MAPPINGS
from strategy.config.market_symbols import COMMODITY_MARKET_SYMBOLS, VALID_FX_SYMBOLS

VALID_SIGNS = {-1, 1}
VALID_PRIORITIES = {"primary", "secondary", "experimental"}

VALID_RELATIONSHIP_TYPES = {
    "exporter",
    "energy_exporter",
    "industrial_metal",
    "resource_exporter",
    "agri_exporter",
    "exporter_china_demand",
    "usd_inverse_macro",
    "market_center_experimental",
}


def validate_mapping() -> None:
    seen = set()
    errors = []

    for i, row in enumerate(CANDIDATE_ASSET_FX_MAPPINGS, start=1):
        required = [
            "commodity",
            "currency",
            "fx_symbol",
            "expected_sign",
            "relationship_type",
            "priority",
        ]

        for field in required:
            if field not in row:
                errors.append(f"Row {i}: missing field {field}")

        commodity = row.get("commodity")
        fx_symbol = row.get("fx_symbol")

        if commodity not in COMMODITY_MARKET_SYMBOLS:
            errors.append(f"Row {i}: invalid commodity '{commodity}'")

        if fx_symbol not in VALID_FX_SYMBOLS:
            errors.append(f"Row {i}: invalid fx_symbol '{fx_symbol}'")

        if row.get("expected_sign") not in VALID_SIGNS:
            errors.append(f"Row {i}: invalid expected_sign {row.get('expected_sign')}")

        if row.get("priority") not in VALID_PRIORITIES:
            errors.append(f"Row {i}: invalid priority '{row.get('priority')}'")

        if row.get("relationship_type") not in VALID_RELATIONSHIP_TYPES:
            errors.append(f"Row {i}: invalid relationship_type '{row.get('relationship_type')}'")

        key = (commodity, row.get("currency"), fx_symbol)
        if key in seen:
            errors.append(f"Row {i}: duplicate mapping {key}")

        seen.add(key)

    if errors:
        print("Mapping validation failed:")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    print(f"Mapping validation passed: {len(CANDIDATE_ASSET_FX_MAPPINGS)} relationships")


if __name__ == "__main__":
    validate_mapping()