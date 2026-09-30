"""独立结果码：每一种业务异常都有自己的可核验结论。"""
from __future__ import annotations

# ---- 导入校验 ----
OK = "OK"
DUPLICATE_PRODUCT = "DUPLICATE_PRODUCT"        # 清单内重复商品
PRICE_LIMIT = "PRICE_LIMIT"                    # 价格越限
PRICE_INVALID = "PRICE_INVALID"                # 价格格式/非正
TAG_NOT_FOUND = "TAG_NOT_FOUND"                # 价签编号不存在
STORE_NOT_FOUND = "STORE_NOT_FOUND"            # 门店不存在
SKU_NOT_FOUND = "SKU_NOT_FOUND"                # 商品不存在
STORE_SKU_MISMATCH = "STORE_SKU_MISMATCH"      # 该门店没有此商品的价签
EFFECTIVE_TIME_INVALID = "EFFECTIVE_TIME_INVALID"
PROMO_OVERLAP = "PROMO_OVERLAP"                # 促销时段重叠
MUTEX_CONFLICT = "MUTEX_CONFLICT"              # 两批次同时改同一门店同一商品
NO_CHANGE = "NO_CHANGE"                        # 新价与当前期望价相同（提示）

# ---- 审批 ----
SELF_APPROVAL = "SELF_APPROVAL"                # 不能审批自己提交的清单
IMPORT_NOT_APPROVABLE = "IMPORT_NOT_APPROVABLE"

# ---- 下发 ----
ACK_SUCCESS = "ACK_SUCCESS"
ACK_TAG_OFFLINE = "ACK_TAG_OFFLINE"            # 价签离线
ACK_SIMULATED_FAIL = "ACK_SIMULATED_FAIL"      # 模拟适配器失败
ALREADY_AT_VERSION = "ALREADY_AT_VERSION"      # 已在目标版本（重试/回滚幂等）

# ---- 回滚 ----
RB_NO_NEED = "RB_NO_NEED"                      # 当前已等于目标版本，空操作
RB_VERSION_MISSING = "RB_VERSION_MISSING"      # 目标版本不存在
RB_BAD_VERSION = "RB_BAD_VERSION"
RB_TAG_NOT_FOUND = "RB_TAG_NOT_FOUND"

# ---- 抽检 ----
INSP_MATCH = "INSP_MATCH"
INSP_MISMATCH = "INSP_MISMATCH"                # 现场价格不一致
INSP_TAG_NOT_FOUND = "INSP_TAG_NOT_FOUND"
INSP_PRICE_INVALID = "INSP_PRICE_INVALID"

LABELS = {
    OK: "校验通过",
    DUPLICATE_PRODUCT: "重复商品",
    PRICE_LIMIT: "价格越限",
    PRICE_INVALID: "价格非法",
    TAG_NOT_FOUND: "价签不存在",
    STORE_NOT_FOUND: "门店不存在",
    SKU_NOT_FOUND: "商品不存在",
    STORE_SKU_MISMATCH: "门店无此商品",
    EFFECTIVE_TIME_INVALID: "生效时间非法",
    PROMO_OVERLAP: "促销时段重叠",
    MUTEX_CONFLICT: "批次互斥冲突",
    NO_CHANGE: "价格未变化",
    SELF_APPROVAL: "禁止自审",
    IMPORT_NOT_APPROVABLE: "清单状态不可审批",
    ACK_SUCCESS: "回执成功",
    ACK_TAG_OFFLINE: "价签离线",
    ACK_SIMULATED_FAIL: "适配器失败",
    ALREADY_AT_VERSION: "已在目标版本",
    RB_NO_NEED: "回滚空操作",
    RB_VERSION_MISSING: "回滚版本不存在",
    RB_BAD_VERSION: "回滚版本非法",
    RB_TAG_NOT_FOUND: "回滚价签缺失",
    INSP_MATCH: "现场一致",
    INSP_MISMATCH: "现场价格不一致",
    INSP_TAG_NOT_FOUND: "扫码价签不存在",
    INSP_PRICE_INVALID: "录入价格非法",
}


def label(code: str) -> str:
    return LABELS.get(code, code)
