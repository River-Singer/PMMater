# PMMater / pmmaster

作者：**allenxujianyang@outlook.com**。采用 [自定义非商业使用许可](LICENSE)：商业使用及再分发须事先取得书面授权（法律及托管平台已授予的权限除外）；不得删除作者声明或冒名发布。使用范围和授权方式见 [许可与作者](project/docs/许可与作者.md)。

一套表管理一个项目的持续跟进 Skill。首选飞书内部智能体与多维表格；通用智能体可使用离线 Excel。成员更新本人任务，PM 统筹计划、风险、资源和决定。

通过智能体对话维护记录与生成材料，Excel 用于离线填写，HTML 用于查看与审议。生成的 HTML 可直接双击打开，内含导航、层级展开和时间范围切换，无需启动本地网页服务。

## 从这里开始

- [使用与文档导航](project/docs/README.md)
- [Skill 入口](project/SKILL.md)与[本地使用手册](project/使用手册.md)
- [飞书母版与智能体驱动](project/docs/飞书母版与智能体驱动.md)
- [飞书母版变更同步记录](project/docs/飞书母版变更同步记录.md)
- [HTML 报告制作说明](project/docs/HTML报告制作说明.md)
- [项目汇报示例](project/demo/本周项目汇报.html)与[延期审议示例](project/demo/持续跟进与延期审议.html)
- [可安装交付包](release/README.md)

项目 WBS 与运维、零星优化分表记录。支持周一计划、周五总结、工作日非员工安排确认、月度与阶段复核、延期原因及补救审议、妙记行动回填，以及日期修改的历史核对。报告包含项目工作周 WK 时间轴、可展开的 WBS 层级和人员资源信息。

飞书显示编号与稳定记录身份分别处理。WBS 编号由母版公式生成，通讯录用姓名展示；离线数据仍保留稳定 ID。实际字段映射、人员身份、权限和宿主触发需要按项目配置。默认不发送消息，本仓库不代表已部署真实租户，也不声称 `SKILL.md` 可直接一键导入 Aily。

## 安装与验证

需要 Python 3.11 或更高版本。在仓库根目录建立环境并安装依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r project/requirements.txt
Set-Location project
..\.venv\Scripts\python.exe -m pm_core.cli --help
..\.venv\Scripts\python.exe -m unittest discover -s tests -q
..\.venv\Scripts\python.exe -m pm_core.design_tokens --check
..\.venv\Scripts\python.exe -m pm_core.demo_reports --check
```

通用智能体安装时，把 `project/` 的完整内容放入名为 `pmmaster` 的技能目录，或解压交付包。飞书内部智能体按[飞书接入](project/references/feishu-setup.md)绑定指令、知识、实际工具和触发。

`project/项目管理模板.xlsx` 是空白模板；[demo](project/demo/README.md) 中的人员、任务、账号与链接均为虚构示例。下载仓库后直接打开示例 HTML 即可查看；GitHub 文件页展示的是源码。`project/pmmaster_导出说明.md` 仅保留旧版导出背景，当前行为以本仓库文档为准。

## 维护与发布

源码、测试、模板、演示与用户文档在 `project/`；开发流程和契约在 `.trellis/`，可复用开发技能在 `.agents/skills/`。本地任务记录、原始导入资料、凭据和正式项目工作库不纳入仓库。

改版 HTML 时维护渲染器与设计 token，并重新生成演示。质量检查通过后，在仓库根目录运行[打包脚本](scripts/package_release.py)，构建方式及校验命令见[交付说明](release/README.md)。

修改发布工具后，在仓库根目录运行 `.\.venv\Scripts\python.exe -m unittest discover -s tests -q` 检查打包契约。仓库 `LICENSE` 与 `project/LICENSE` 须保持一致，交付包必须带上许可。
