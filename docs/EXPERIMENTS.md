# 历史实验结果与边界

本页为既有原生及离线回执的脱敏摘要。公开字段保留记录中的状态和覆盖范围。原始图纸、模型、路径、进程信息、日志和完整回执留存于原实验归档。本次发布 `REAL_SW_INVOKED=NO`。

| 历史实验 | 摘要 | 边界 |
| --- | --- | --- |
| Skill V1 安装与离线走查 | 安装检查通过，整件重建、局部纠错和仅工程图三类请求完成离线走查。 | 新图经安装包执行的 Native 链路在该轮为 `NOT_RUN`。 |
| R008 UNKNOWN 参数绑定 | 待补参数和条件计划达到有界离线验证。 | 未补足真实来源尺寸，未新增 Native 资格。 |
| R007-P4 | 三份结构化三视图 A、B、独立 C 经同一条执行链生成真实 SLDPRT，`PASS_BOUNDED`。 | 轴对齐矩形宿主 Boss 与限定的 Z 向圆柱 Cut。工程图往返 `NOT_PROVEN`，工程验收 `NOT_VALIDATED`。 |
| R005 A6 Q401、Q402 | 两案为 `PASS_BOUNDED_QUALIFICATION`，重开前后几何和语义 Trim 检查通过。 | 五特征限定合同，原生物理边映射为 `PARTIAL_NATIVE_NULL_BOUNDARIES`，工程状态 `NOT_VALIDATED`。 |
| 六个复杂件 | 被审计的六件中 **6/6** 走 R004 自有会话路线。 | 原版 Automation、Agent 判断及项目按案脚本协作。覆盖计数不用于计算代码复用率、性能增幅或 B/F 自主建模通过率。 |

## 深度恢复实验

源尺寸来自既有审核的结构化合成工程约束。实验先对模型副本原生注入 **28 mm** 深度，保存重开证实缺陷成立，再以人工提供的修正意图从正式入口恢复至 **32 mm**。

| 验证项 | 历史结果 |
| --- | --- |
| 修复深度 | 原生重开回读 **32 mm**。 |
| 下游横孔 | `PASS_BOUNDED_DOWNSTREAM_CHECK`，未报告意外依赖变化。 |
| 零件保存重开、图纸几何往返 | `PASS`、`PASS_GEOMETRY`。 |
| 源图证据几何比较 | `PASS`，几何差异 0。 |
| 可见性、源图比较总结果 | 均为 `NOT_PROVEN`。 |
| 整体工程验收 | `NOT_VALIDATED`。 |

## 复杂支架原草图纠错

根据人工提供的源图材料侧修正意图，在 SOLIDWORKS 2025 SP05 的受控会话中回解副本的 Feature、Sketch 和端点。对已有草图修改六个端点，补实 **35 × 11 mm** 多余开口并去除 **35 × 9 mm** 错误悬梁，保留原 Boss 和 13 项特征。

| 原始回执字段 | 历史值 |
| --- | --- |
| `local_geometry` | `BOUNDED_LOCAL_CORRECTION_CONFIRMED` |
| `save_close_reopen` | `PASS` |
| `native_drawing_roundtrip` | `PASS_GEOMETRY` |
| `source_comparison` | `FAIL` |
| `engineering_validation` | `NOT_VALIDATED` |
| `current_difference_count` | **22**，含 19 条原生边差异和 3 条匹配器汇总 |
| `visibility_unknown_count` | **52** |
| `support_validation` | `NOT_PROVEN`，有一处具体 FACE Support 未证明 |

既有草图仍为欠约束。条件参数和未标小圆角继续按原始证据处理。整图结果和局部几何结果分别据实保留。
