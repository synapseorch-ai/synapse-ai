#: One document per tenant, not a list — stored as a collection singleton.
_COLLECTION = "personal_details"


def default_personal_details() -> dict:
    return {
        "first_name": "",
        "last_name": "",
        "email": "",
        "phone_number": "",
        "address": {
            "address1": "",
            "address2": "",
            "city": "",
            "state": "",
            "zipcode": "",
        },
    }


async def load_personal_details() -> dict:
    from core.store import collections

    defaults = default_personal_details()
    try:
        data = await collections.load_one(_COLLECTION)
    except Exception as e:
        print(f"DEBUG: Error loading personal_details: {e}")
        return defaults

    merged = {**defaults, **(data if isinstance(data, dict) else {})}
    addr = merged.get("address") if isinstance(merged.get("address"), dict) else {}
    merged["address"] = {**defaults["address"], **addr}
    return merged


async def save_personal_details(details: dict) -> dict:
    # Normalize to our schema with defaults
    defaults = default_personal_details()
    d = details if isinstance(details, dict) else {}

    normalized = {**defaults, **d}
    addr = d.get("address") if isinstance(d.get("address"), dict) else {}
    normalized["address"] = {**defaults["address"], **addr}

    from core.store import collections
    await collections.save_one(_COLLECTION, normalized)

    return normalized
