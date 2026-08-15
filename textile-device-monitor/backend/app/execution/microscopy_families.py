"""GB/T 36422-2018 电镜记录家族注册表。

“纤维微观形貌”（5103.5）与“纤维横截面”（5103.426）共用同一套
选图、原始记录生成、特种毛图片上传、复核与检验记录登记链路，仅在任务
项目匹配、原始记录 A1 标题和旧系统登记模板上不同。本模块是唯一事实源：
流程定义通过节点 config 的 ``record_family`` 选择家族，写门禁再按任务
项目的编号/名称独立复核，配置拼写错误或项目漂移都会 fail closed。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from app.execution.errors import ExecutionApiError


MICROSCOPY_FAMILY_KEY = "microscopy"
CROSS_SECTION_FAMILY_KEY = "cross_section"

MICROSCOPY_TEST_METHOD = "GB/T 36422-2018"
MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION = (
    "gbt36422-2018-legacy-template-binding-v1"
)

# 旧系统检验记录登记模板绑定（由只读探针按模板逐一签发映射配置指纹）。
# 微观形貌家族支持 1/2/3/5/6/7/10 张图七个模板。
MICROSCOPY_LEGACY_TEMPLATE_BINDINGS: dict[int, dict[str, Any]] = {
    1: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 1,
        "legacy_template_name": "微观形貌.xls",
        "local_asset_name": "gbt36422-2018-microscopy-1-image-v1.xls",
        "local_asset_sha256": (
            "b169fb5cf236004058f0666ee28979836266168311172efc01c7ec7d5906c6fe"
        ),
        "mapping_config_sha256": (
            "a09399783171826d10b239bd01cb596569428bbc34a8c4636077e98f34dc690e"
        ),
    },
    2: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 2,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-2张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-2-images-v1.xls",
        "local_asset_sha256": (
            "98145d6ea4dfafada8cbd09ad5aa8a9991ce27c3b07f248f70178d60e6cbd191"
        ),
        "mapping_config_sha256": (
            "2ff546b96da9ac423613374ee28955bf7dfe3e62e5d40d8ad979c93636836f23"
        ),
    },
    3: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 3,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-3张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-3-images-v1.xls",
        "local_asset_sha256": (
            "4a4e7b69a5dba7684fe837779929b4d093fe509cbf398ae37114cd22071b70b4"
        ),
        "mapping_config_sha256": (
            "43ae3872f231c2499b98976ea63827162b4fddf7151e3e8daceee8a7591d4268"
        ),
    },
    5: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 5,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-5张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-5-images-v1.xls",
        "local_asset_sha256": (
            "3b148fd8ccbb28b8fe8e60ceee0e4898c144c1fbe15b8a451ed44e0d2141d38d"
        ),
        "mapping_config_sha256": (
            "d21e82cd1672ada28beab35673e1d679bf3ffa1099969f02dbed466645dc9b6d"
        ),
    },
    6: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 6,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-6张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-6-images-v1.xls",
        "local_asset_sha256": (
            "98145d6ea4dfafada8cbd09ad5aa8a9991ce27c3b07f248f70178d60e6cbd191"
        ),
        "mapping_config_sha256": (
            "5f56deb633c0dd2b2dc046ba70ab012c2780dbbae9039663b4d40903804a6304"
        ),
    },
    7: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 7,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-7张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-7-images-v1.xls",
        "local_asset_sha256": (
            "98145d6ea4dfafada8cbd09ad5aa8a9991ce27c3b07f248f70178d60e6cbd191"
        ),
        "mapping_config_sha256": (
            "3aea5aa68bccb1a8e9035be8762d305bb2f0d6e4104ad29557082a3b1abada01"
        ),
    },
    10: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 10,
        "legacy_template_name": "纤维微观形貌-GB T 36422-2018-10张图.xls",
        "local_asset_name": "gbt36422-2018-microscopy-10-images-v1.xls",
        "local_asset_sha256": (
            "85627e8ca9824274fe61f13276b390a75288a119987053e3a244157ee0f46e8c"
        ),
        "mapping_config_sha256": (
            "976a88ed86af2a3fb30df5aa830e0529ea35e15e1e2fa59244e3035578940f74"
        ),
    },
}

# 横截面家族（5103.426 / 纤维横截面）：旧系统只配置了 1/2/3 张图三个模板。
# 映射配置指纹由 260191285 只读探针签发（配置行数均为 9，与微观形貌一致）。
CROSS_SECTION_LEGACY_TEMPLATE_BINDINGS: dict[int, dict[str, Any]] = {
    1: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 1,
        "legacy_template_name": "纤维横截面.xls",
        "local_asset_name": "gbt36422-2018-cross-section-1-image-v1.xls",
        "local_asset_sha256": (
            "537099b8725b76d306c453f25da27d90bb3290129db5b3f9705f0b7cd675f862"
        ),
        "mapping_config_sha256": (
            "d35d97a79e7b1160d437c67f4b1b21298261cdd08949df8aaa1526f4f8a053b5"
        ),
    },
    2: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 2,
        "legacy_template_name": "纤维横截面-2张图.xls",
        "local_asset_name": "gbt36422-2018-cross-section-2-images-v1.xls",
        "local_asset_sha256": (
            "14106cad1f5088af65dda65a4bc833549fc3a40ce7a8f16f0c3406fec3770ddc"
        ),
        "mapping_config_sha256": (
            "bffb70d1536917dc76d090d091c0056810738cca02a61351c506861de9ff1ca9"
        ),
    },
    3: {
        "binding_version": MICROSCOPY_LEGACY_TEMPLATE_BINDING_VERSION,
        "image_count": 3,
        "legacy_template_name": "纤维横截面-3张图.xls",
        "local_asset_name": "gbt36422-2018-cross-section-3-images-v1.xls",
        "local_asset_sha256": (
            "5c37d9e6bfa3877d495d234c5c7f87fede3882a59419f7a2452884caf517b107"
        ),
        "mapping_config_sha256": (
            "52f9cbb73bd50484eff78ebac43a662ec5cb650858a845222d225779dbeaa893"
        ),
    },
}


@dataclass(frozen=True, slots=True)
class MicroscopyRecordFamily:
    key: str
    project_name_aliases: frozenset[str]
    test_method: str
    check_item_no: str
    check_item_name: str
    # 生成的 39-8B 原始记录 A1 标题；None 表示保持模板原文（微观形貌）。
    record_title: Optional[str]
    # 生成的检验记录登记工作簿文件名片段。
    check_record_filename_segment: str
    max_selected_images: int
    template_bindings: dict[int, dict[str, Any]]


_FAMILIES = (
    MicroscopyRecordFamily(
        key=MICROSCOPY_FAMILY_KEY,
        project_name_aliases=frozenset({"纤维微观形貌", "膜平面形貌"}),
        test_method=MICROSCOPY_TEST_METHOD,
        check_item_no="5103.5",
        check_item_name="纤维微观形貌",
        record_title=None,
        check_record_filename_segment="纤维微观形貌",
        max_selected_images=10,
        template_bindings=MICROSCOPY_LEGACY_TEMPLATE_BINDINGS,
    ),
    MicroscopyRecordFamily(
        key=CROSS_SECTION_FAMILY_KEY,
        project_name_aliases=frozenset({"纤维横截面"}),
        test_method=MICROSCOPY_TEST_METHOD,
        check_item_no="5103.426",
        check_item_name="纤维横截面",
        record_title="纤维横截面原始记录",
        check_record_filename_segment="纤维横截面",
        max_selected_images=3,
        template_bindings=CROSS_SECTION_LEGACY_TEMPLATE_BINDINGS,
    ),
)

MICROSCOPY_RECORD_FAMILIES: dict[str, MicroscopyRecordFamily] = {
    family.key: family for family in _FAMILIES
}

ALL_PROJECT_NAME_ALIASES: frozenset[str] = frozenset(
    alias for family in _FAMILIES for alias in family.project_name_aliases
)


def microscopy_family_for_key(value: object) -> Optional[MicroscopyRecordFamily]:
    return MICROSCOPY_RECORD_FAMILIES.get(str(value or "").strip())


def microscopy_family_from_config(config: object) -> MicroscopyRecordFamily:
    """Resolve the family pinned by a workflow node's ``record_family`` config."""

    key = (
        config.get("record_family")
        if isinstance(config, dict)
        else None
    )
    if key in (None, ""):
        return MICROSCOPY_RECORD_FAMILIES[MICROSCOPY_FAMILY_KEY]
    family = microscopy_family_for_key(key)
    if family is None:
        raise ExecutionApiError(
            422,
            "microscopy_record_family_unknown",
            "流程节点配置了未知的电镜记录家族",
            details={"record_family": str(key)},
        )
    return family


def _normalized_lookup_text(value: object) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", str(value or "")).strip().split()
    )


def microscopy_family_for_project(
    check_item_no: object,
    check_item_name: object,
    db: object = None,
) -> Optional[MicroscopyRecordFamily]:
    """Exact fail-closed lookup used by the external write gates.

    With a database session the lookup is governed by the admin-editable
    project rules (``binding`` facts), so rule edits reach the write path;
    without one it falls back to the code constants (pure/test contexts).
    """

    if db is not None:
        from app.execution.project_rules import rule_for_facts

        rule = rule_for_facts(
            db,
            check_item_no=check_item_no,
            check_item_name=check_item_name,
        )
        if rule is None:
            return None
        return microscopy_family_for_key(rule.binding.get("family_key"))

    no = _normalized_lookup_text(check_item_no)
    name = _normalized_lookup_text(check_item_name)
    for family in _FAMILIES:
        if family.check_item_no == no and family.check_item_name == name:
            return family
    return None
