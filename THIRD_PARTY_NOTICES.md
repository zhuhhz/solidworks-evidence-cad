# 代码来源、许可与致谢

## 本仓库许可证

本仓库中由项目贡献者拥有并有权授权的原创源码、测试和文档，按根目录的 [MIT License](LICENSE) 发布：

- 版权所有者署名：Copyright (c) 2026 zhuhhz。
- 使用、复制、修改、再分发或商业使用时须按MIT条款保留版权及许可声明。
- MIT许可证不改变第三方依赖自身的许可证，也不授予SolidWorks软件、品牌、模型、工程图或其他第三方材料的权利。

若发现个别文件包含不属于本项目的第三方内容，应以原权利人的许可为准，并通过Issue指出具体文件和来源以便核查。

## 本次发布的项目代码

| 范围 | 来源与用途 |
| --- | --- |
| [R004执行适配器及必要依赖](src/solidworks_main_skill/backends/solidworks_automation/r004_execution_adapter.py) | Main Skill的R004实际适配器、R003按PID绑定与原生采集、执行绑定、会话和文档生命周期、必要的原有推理和测试 |
| [R007轮廓验证](src/r007_geometry.py) | LINE/ARC有界轮廓几何与材料侧判定 |
| [R007解析重投影](src/r007_reprojection.py) | 受限候选的正投影视图与源图比较 |
| [R008坐标框架](src/r008_frames.py) | 正交右手草图坐标系及映射 |

R004源码依据六件实际使用的适配器版本和原有依赖整理，保留原实现，不将第三方工具描述为自研成果。运行和测试范围见[R004使用说明](docs/R004.md)。

## 外部软件及未包含的资产

- **SolidWorks Automation Skill**：感谢 [wzyn20051216/solidworks-automation-skill](https://github.com/wzyn20051216/solidworks-automation-skill) 及其维护者和贡献者。上游提供通用CAD操作和Engineering Drawing，按其[MIT许可证](https://github.com/wzyn20051216/solidworks-automation-skill/blob/main/LICENSE)单独授权。本仓库不分发上游源码。
- **SolidWorks**：Dassault Systèmes的专有CAD平台。本仓库不提供安装包、许可证、SDK或类型库，亦无官方关联或背书。
- **Python运行依赖**：`pywin32`、`comtypes`、`pytest`及其上游各自按独立许可提供，需视使用情形另行安装。
- **其他历史工具**：RapidOCR、eDOCr2、Agent Desktop曾用于部分历史任务；本仓库未分发其源码、模型权重或二进制。
- **未发布资产**：原始工程图、模型、部分实验记录、完整纠错产品链、六件按案脚本及机器专属配置保留于原项目档案，不在本仓库的公开授权范围内。
