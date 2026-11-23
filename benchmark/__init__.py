"""跨境服务商对标应用。

模块划分：
- materials：合成资料加载、指纹校验与版本标识
- revisions：修订/撤回解析，产生当前有效申报且保留版本轨迹
- calibers：业务口径变更与可比性标注
- attribution：收入归属、实际服务产出与转包识别
- metrics：指标口径、观察月要求与共同分母相容组合
- peergroup：可比组构建、权重、覆盖、不确定性与放宽建议
- disclosure：联盟披露审查、历史组合推导与最小公开策略
- ledger：已发布内容的只追加台账
- service / access / cli：应用编排、权限视图与命令行
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
