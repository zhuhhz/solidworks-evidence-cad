# SolidWorks Evidence CAD

**Evidence-Grounded CAD Reconstruction & Repair**

SolidWorks Main Skill 的公开研究原型，面向工程图证据推理、原生对象验证与局部纠错。

机械工程图中的投影、尺寸和剖面往往只能限定一组可能的三维结构。即使 SolidWorks 成功生成了零件，仍需验证建模假设、对象归属以及再生工程图是否符合来源。本研究把这三个问题分开核对，为受限重建与局部纠错保留可追溯的证据。

| 项目研究实现 | 当前范围 |
| --- | --- |
| **工程图证据推理** | B008–B010 从受限图像提取投影图元，经人工确认形成 FeatureGraph 与 ModelingPlan。R007 加入跨视图约束、候选排除和解析重投影，R008 记录 UNKNOWN 并支持有来源的条件参数绑定。 |
| **原生对象身份验证** | R003/R008 回解 SolidWorks 原生持久引用，核对所属 Feature、草图支撑和坐标系。几何相似只能帮助筛选候选，身份结论取决于实际原生对象和关系。 |
| **源图约束下的局部纠错** | 修正请求绑定源图依据、模型副本、修改前状态、目标原生对象和下游依赖。修改后保存重开，通过原生回读及再生工程图检查影响范围。实测案例的修正意图由人提供。 |

```text
工程图证据 → 人工确认 → FeatureGraph / ModelingPlan
                               ↓
                  自有会话与原生对象绑定
                               ↓
              原版 Automation / Engineering Drawing
                               ↓
                  SolidWorks 原生零件与工程图
                               ↓
            重开回读、源图对照、按范围复核或修正
```

原版 [SolidWorks Automation Skill](https://github.com/wzyn20051216/solidworks-automation-skill) 提供常规草图、实体特征与导出，其 Engineering Drawing 子技能提供常规视图和标注。项目实现位于证据推理、原生身份和结果复核这几层。六个复杂件的工作流包含 Agent 读图判断、原版模块、项目辅助代码和按案脚本。感谢上游项目的维护者与贡献者。[方法与执行分工](docs/METHOD.md) · [来源与许可](THIRD_PARTY_NOTICES.md)

## 已有实验

| 历史实验 | 实际结果 |
| --- | --- |
| R007 结构化三视图 | 三个受限 Native 案例通过，原生工程图往返仍为 `NOT_PROVEN`。 |
| R005 五特征身份与拓扑验收 | 两个 A6 案例取得独立有界验收通过，整体工程状态为 `NOT_VALIDATED`。 |
| Boss 深度 **28 → 32 mm** | 原生恢复深度，下游横孔有界检查通过。零件保存重开和图纸几何往返通过，源图几何比较通过，可见性未证。 |
| 复杂支架原草图局部纠错 | 实测补齐多余开口并移除错误悬梁，保留 13 项特征。零件保存重开通过，工程图几何往返通过，整图源图比较仍为 `FAIL`。 |
| 六个复杂件协作执行 | **6/6** 使用项目 R004 受控会话。该数字衡量所审计案例的路径覆盖。 |

原始回执状态、失败及未证事项见[实验结果与边界](docs/EXPERIMENTS.md)。

## 公开范围与研究状态

本仓库公开 [R004 实际执行适配器](src/solidworks_main_skill/backends/solidworks_automation/r004_execution_adapter.py)、原有受控工厂及其项目依赖、现有离线测试和使用说明。依赖包含 R003 按 PID 绑定、原生引用和回读，以及 R004 执行绑定、生命周期和清理。原版 Automation、SolidWorks 与本机运行环境由使用者单独准备。[R004 使用与验证](docs/R004.md)

原有 [LINE/ARC 轮廓几何](src/r007_geometry.py)、[受限解析重投影](src/r007_reprojection.py) 和 [右手草图坐标系](src/r008_frames.py) 继续公开。完整图纸推理、纠错产品链和历史案例材料仍在各自项目归档。公开源码可按说明进行离线核查，完整历史 Native 实验依赖相应原始材料与环境。

复杂图纸的歧义仍需确认，六件复杂交付仍有按案编排。任意复杂件自主重建、通用自动纠错及制造级验收尚未验证。本次公开整理使用既有证据，`REAL_SW_INVOKED=NO`。
