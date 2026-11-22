# SolidWorks Evidence CAD

[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-blue.svg)](LICENSE)
[![Source Available](https://img.shields.io/badge/Code-Source%20Available-orange.svg)](LICENSE)
![Status: Research Prototype](https://img.shields.io/badge/Status-Research%20Prototype-orange.svg)
![Language: Python](https://img.shields.io/badge/Language-Python-3776AB.svg)
![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D4.svg)

**Evidence-Grounded CAD Reconstruction & Repair**

基于工程图证据的 **SolidWorks CAD 推理、原生对象验证与局部纠错**研究原型。项目由 SolidWorks Main Skill 的阶段性成果整理而来；常规建模与基础出图复用第三方 [SolidWorks Automation Skill](https://github.com/wzyn20051216/solidworks-automation-skill)，本仓库重点公开项目自身的证据推理和受控执行实现。

**项目状态：** 阶段性研究原型 · 非完整自主建模产品  
**技术环境：** Python · Windows COM · SolidWorks（原生运行需外部安装）  
**源码许可：** [PolyForm Noncommercial License 1.0.0](LICENSE) · **源码公开，未经授权不得商业使用**（具体许可目的及历史 MIT 授权例外见下文）

## 要解决的问题

机械工程图中的投影、尺寸和剖面不一定唯一确定三维结构。即使 CAD API 成功执行操作，还需要确认：建模计划的依据是什么、实际修改的是哪个原生对象、结果是否符合原图。

本项目把**证据推理、原生执行与结果复核**分别记录，允许明确报告 `PASS`、`FAIL` 和 `NOT_PROVEN`，而不是将“API成功调用”等同于“工程验收通过”。

## 三项核心研究能力

| 能力 | 已实现内容 | 主要边界 |
| --- | --- | --- |
| **工程图证据推理** | B008–B010 将受限投影图元与人工确认的证据转换为 FeatureGraph / ModelingPlan；R007 提供受限约束推理与解析重投影，R008 支持有来源的 UNKNOWN 条件参数绑定 | 有界几何与已审核输入；并非任意复杂工程图自动理解 |
| **原生对象身份验证** | R003/R008 基于 SolidWorks 持久引用、Feature 归属、草图 Support 与坐标关系检查当前对象 | 某些对象或 Support 仍可能为 `NOT_PROVEN` |
| **源图约束局部纠错** | 在模型副本上根据人工给出的修正意图更新既有草图或尺寸，保存重开后复核下游依赖与再生图纸 | 不等于通用自主纠错或制造级验收 |

### 执行分工

```text
工程图证据 → 人工确认 → FeatureGraph / ModelingPlan
                                ↓
                    R003/R004 受控会话与身份
                                ↓
               原版 Automation / Engineering Drawing
                                ↓
                    SolidWorks 原生模型与图纸
                                ↓
                    原生回读、源图对照、局部纠错
```

常规草图、拉伸、切除、倒角、圆角和基础工程图由上游 Automation 与其 Engineering Drawing 子技能实现。六个复杂件的交付还包含 Agent 读图与按案脚本；不能据此声称 B/F 生成器已独立完成这些复杂件。

详见[方法及分工](docs/METHOD.md)。

## 已公开的源码

- **R004 实际执行适配器**：[`r004_execution_adapter.py`](src/solidworks_main_skill/backends/solidworks_automation/r004_execution_adapter.py)、[执行绑定](src/solidworks_main_skill/backends/solidworks_automation/r004_execution_binding.py)、[受控工厂](experiments/three_view_reconstruction/run_r004_q01_real_qualification.py)及必要的R003身份、文档生命周期与清理源码。
- **受限轮廓与重投影**：[`r007_geometry.py`](src/r007_geometry.py)、[`r007_reprojection.py`](src/r007_reprojection.py)。
- **草图坐标框架**：[`r008_frames.py`](src/r008_frames.py)。
- **现有离线测试**：[`tests/unit/`](tests/unit/)，复用历史测试，不声称覆盖全部Native执行路径。

[查看R004使用说明、依赖和离线测试命令](docs/R004.md)。运行Native路径需要使用者自行准备Windows、SolidWorks、原版Automation、`pywin32`、`comtypes`和相应环境配置。本仓库**不是开箱即用的完整工程图到CAD产品**，不附带原始图纸、模型或完整纠错产品链。

## 历史实测与未完成项

| 实测项目 | 已有证据 | 未证明的范围 |
| --- | --- | --- |
| R007结构化三视图 | 三个受限输入生成真实SLDPRT并通过有界Native验收 | 工程图往返 `NOT_PROVEN` |
| R005原生身份与拓扑 | 两个五特征限定用例通过独立有界验收 | 整体工程状态 `NOT_VALIDATED` |
| 深度修复 28 → 32 mm | 原生修改、下游横孔有界检查及图纸几何往返通过 | 可见性 `NOT_PROVEN` |
| 复杂支架原草图纠错 | 局部修正、保存重开、图纸几何往返通过，保留13项特征 | 整图比较 `FAIL`，仍有22项差异、52项可见性UNKNOWN及未证FACE Support |
| 六个复杂件协作 | 审计样例中6/6使用R004受控会话 | 不代表相对上游的成功率或性能优势 |

原始证据状态见[历史实验与限制](docs/EXPERIMENTS.md)。R004公开候选曾通过**119项选定离线测试**；另有一项模拟出图测试在导入`pythoncom`时被阻断，未取得结果。该发布过程未启动SolidWorks（`REAL_SW_INVOKED=NO`），也未重新验证完整Native端到端链路。

## 源码许可与商业使用

**源码公开，未经授权不得商业使用。** 本仓库当前版本以 [PolyForm Noncommercial License 1.0.0](LICENSE) 授权项目方有权授权的原创软件源码和相关文件（Copyright © 2026 zhuhhz）。这是**源码可见 / Source-Available** 许可，**不是符合 OSI 开源定义的标准开源许可证**；不得把本项目描述为“OSI认证开源”或“MIT开源”。

- **许可证允许的用途**：在其条款规定的许可目的内研究、学习、实验、修改和分发；[官方条款](https://polyformproject.org/licenses/noncommercial/1.0.0)对个人非商业活动和教育机构、公益、公共研究等组织有具体定义。
- **商业用途**：超出许可证允许范围的商业使用需要另行取得权利人授权。不得将项目代码擅自用于收费软件、商业服务或其他未经许可的商业场景。是否符合条款应以许可证全文及具体使用情形判断，而非仅凭本摘要。
- **保留声明**：再分发时须提供许可条款全文或官方链接，并保留本项目提供的 [Required Notice](NOTICE)。
- **第三方与品牌**：本许可证不覆盖上游 Automation、Python 第三方包、SolidWorks 软件或商标，也不授权未公开的工程图与模型。

**历史 MIT 授权的重要说明：** 本仓库在 2026-11-22 的既有公开提交中曾以 MIT License 授权相应代码。**已经依据当时 MIT 条款合法取得的副本与权限，不会因为本次改许可而被自动或追溯撤销**；这些历史版本仍可依其有效 MIT 授权使用，包括符合 MIT 条款的商业使用。因此本次改许可不能保证历史上已按 MIT 发布的相同代码今后绝对无法被商业使用。历史提交保留，不改写。[详细历史许可记录](docs/LICENSE_HISTORY.md)。

[完整许可证](LICENSE) · [Required Notice](NOTICE) · [历史许可记录](docs/LICENSE_HISTORY.md) · [第三方来源与许可证核查](THIRD_PARTY_NOTICES.md)

感谢 [wzyn20051216/solidworks-automation-skill](https://github.com/wzyn20051216/solidworks-automation-skill) 的维护者与贡献者。该独立上游项目采用自身的 MIT 许可，本仓库不分发其源码。本项目为独立研究原型，与 Dassault Systèmes / SOLIDWORKS 无官方关联或背书；不宣称任意复杂件自主重建、通用自动纠错或制造级验收能力。
