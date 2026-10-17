# 组件级异常：区分「后端调用失败」与「调用成功但结果为空」
#
# 这些 provider 此前把失败吞成空结果（返回 []），调用方无法分辨
# 「没有匹配」和「后端挂了」——基础设施故障被静默降级成一句
# 「知识库无相关内容」。统一抛这些异常后，上层可以明确返回 503
# 并给出可操作提示，而不是拿一个看似正常的空答案糊弄过去。
from __future__ import annotations


class ProviderError(RuntimeError):
    """外部依赖调用失败（向量库 / Embedding / 联网搜索）"""


class VectorStoreError(ProviderError):
    """向量库不可用或返回错误"""


class EmbeddingError(ProviderError):
    """Embedding 服务不可用，或维度未知"""


class WebSearchError(ProviderError):
    """联网搜索调用失败"""
