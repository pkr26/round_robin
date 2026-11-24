"""Provider wire schemas retain the full contract in local validation."""
from copy import deepcopy


def wire_schema(schema: dict, provider: str) -> dict:
    schema = deepcopy(schema)
    unsupported = {"minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "maxItems", "uniqueItems", "contains", "minContains", "maxContains"}

    def transform(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object":
                value["required"] = list(value.get("properties", {}))
                value["additionalProperties"] = False
            if provider == "anthropic":
                removed = {key: value.pop(key) for key in list(value) if key in unsupported or key == "minItems" and value[key] not in {0, 1}}
                if removed:
                    constraints = ", ".join(f"{key}={bound}" for key, bound in sorted(removed.items()))
                    value["description"] = (value.get("description", "") + f" Application validation requires {constraints}.").strip()
            for child in value.values():
                transform(child)
        elif isinstance(value, list):
            for child in value:
                transform(child)

    transform(schema)
    return schema
