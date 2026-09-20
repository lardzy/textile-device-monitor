"""Convert installed defaults into editable, self-contained node configuration."""

from dataclasses import asdict

from app.execution.microscopy_families import microscopy_family_for_key


def family_profile(key):
    family = microscopy_family_for_key(key)
    if family is None:
        raise ValueError(f"未知内置家族：{key}")
    result = asdict(family)
    result["project_name_aliases"] = sorted(result["project_name_aliases"])
    result["template_bindings"] = {str(key): value for key, value in result["template_bindings"].items()}
    return result


def expand_domain_profiles(document):
    for node in document["definition"]["nodes"]:
        if node["type"] in {"microscopy.image_candidates", "microscopy.original_record.render", "microscopy.check_record.render"}:
            config = node["config"]
            if node["type_version"] == 1 and microscopy_family_for_key(config.get("record_family")):
                config["family_profile"] = family_profile(config["record_family"])
                node["type_version"] = 2
    return document
