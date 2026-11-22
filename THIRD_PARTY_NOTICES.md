# 代码来源、许可与致谢

## 本次发布的项目代码

| 文件 | 来源和用途 |
| --- | --- |
| [`r007_geometry.py`](src/r007_geometry.py) | Main Skill 的 R007 轮廓验证、LINE/ARC 有界几何及材料侧判定。 |
| [`r007_reprojection.py`](src/r007_reprojection.py) | Main Skill 的 R007 解析重投影和来源视图比较。 |
| [`r008_frames.py`](src/r008_frames.py) | Main Skill 的 R008 正交右手草图坐标系及映射。 |

这三份文件来自项目的已提交版本，按源码原样发布。该仓库呈现可公开查阅的研究成果，目前未对本项目源码另行声明开源许可证。上游项目的许可独立适用。

## 依赖、参考与平台

- 感谢 [wzyn20051216/solidworks-automation-skill](https://github.com/wzyn20051216/solidworks-automation-skill) 的维护者与贡献者。上游 Automation 提供常规 CAD 特征与导出，Engineering Drawing 提供常规视图、标注和图纸工作流。上游采用 [MIT 许可证](https://github.com/wzyn20051216/solidworks-automation-skill/blob/main/LICENSE)。本次包未分发上游源码。
- RapidOCR、eDOCr2 与 Agent Desktop 是外部工具，历史上仅按任务取用。本次包未分发这些工具或其模型权重。
- SolidWorks 为外部专有平台。历史原生验证在有许可的本地环境进行。本包未分发其安装程序、模板、SDK 或类型库。
- 原始图纸与零件、完整原生执行和纠错实现、运行配置及日志不在本次公开范围。
