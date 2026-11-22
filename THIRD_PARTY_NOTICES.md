# 代码来源、许可证与第三方核查

## 本仓库当前授权

项目方有权授权的原创代码及配套文件，自本次许可证变更后的当前发布版本起，使用根目录的 [PolyForm Noncommercial License 1.0.0](LICENSE)（**非 OSI 开源许可证**）。

**源码公开，未经授权不得商业使用。** 以该许可证实际授予的许可目的和例外为准。请一并保留 [Required Notice](NOTICE)：

`Required Notice: Copyright (c) 2026 zhuhhz`

**历史授权不追溯撤销。** 此前公开提交曾使用 MIT License。已合法依原 MIT 条款取得代码的人，仍可继续依原许可使用对应副本，包括原 MIT 允许的商业使用。详情见[历史许可记录](docs/LICENSE_HISTORY.md)。本次切换不改变Git历史中的原许可事实。

## 已核查的外部依赖

| 依赖或平台 | 来源与核查结果 | 本仓库是否打包 |
| --- | --- | --- |
| [SolidWorks Automation Skill](https://github.com/wzyn20051216/solidworks-automation-skill)（含 Engineering Drawing） | 上游[LICENSE](https://github.com/wzyn20051216/solidworks-automation-skill/blob/main/LICENSE)为 MIT；提供常规 CAD 及制图 API。其许可证独立适用。 | **否**；由使用者单独取得 |
| [pywin32](https://github.com/mhammond/pywin32) | 项目公开资料以 PSF 类授权为主，内部含不同许可组成；对具体分发的组件和版本应查阅其[发行信息](https://pypi.org/project/pywin32/)及文件级声明。 | 否 |
| [comtypes](https://github.com/enthought/comtypes) | [官方许可证](https://github.com/enthought/comtypes/blob/main/LICENSE.txt)与[包元数据](https://pypi.org/project/comtypes/)显示 MIT。 | 否 |
| [pytest](https://github.com/pytest-dev/pytest) | 仅为测试工具，[官方LICENSE](https://github.com/pytest-dev/pytest/blob/main/LICENSE)为 MIT。 | 否 |
| SolidWorks / Dassault Systèmes | 外部专有软件和商标。使用本仓库不提供或替代其许可。 | 否 |

历史实验曾使用 RapidOCR、eDOCr2、Agent Desktop 等外部工具，本公开仓库未包含这些工具的源码、二进制或模型权重；本次没有对所有历史可选工具的版本及许可进行逐项穷尽审核。

## 本项目的发布源码

| 范围 | 角色 |
| --- | --- |
| [R004受控执行适配器及必要项目源码](src/solidworks_main_skill/backends/solidworks_automation/r004_execution_adapter.py) | R004实际适配器、R003按PID绑定与回读、文档生命周期、组合推理和相关测试 |
| [R007轮廓几何](src/r007_geometry.py) | 受限LINE/ARC轮廓及材料侧判定 |
| [R007解析重投影](src/r007_reprojection.py) | 候选投影和来源视图比较 |
| [R008坐标系](src/r008_frames.py) | 正交右手草图框架 |

R004源文件按原实用版本及依赖整理，不将上游API、第三方执行工具归于本项目原创。查看[R004使用说明](docs/R004.md)。

**审核范围说明：** 本次完成了公开依赖的上游许可证核对和本仓库已列出的源文件范围核对；并非对历史所有源码贡献、潜在嵌入片段或知识产权归属作出法律保证。若发现某文件含第三方权利内容，须优先遵守原权利人的条款并单独核查。

原始工程图、模型、完整纠错产品链、六件按案脚本、机器配置和原始日志未在公开仓库中发布。
