# pmmaster 技能包导出说明

> 历史记录：下文保留 2026-11-10 的原版导出说明。旧本地网页工作台及其启动脚本已不属于当前入口；当前版本通过智能体对话、Excel 和独立 HTML 使用，操作见 [README](README.md) 与 [使用手册](使用手册.md)。

- 导出日期：2026-11-10
- 对应版本：v1.0.8（与 Aily 技能市场「pmmaster」技能当前发布版本内容一致，2026-11-09 发布提交）
- 包内文件：51 个（50 个技能文件 + 本说明；编译缓存 __pycache__ 已排除）

## 这是什么

pmmaster 是单项目管理工作台：以 Excel / 飞书多维表格双入口管理项目计划与交付物验收，覆盖 WBS 任务、责任人、进度、风险、周报生成、快照留档、本地 Web 工作台与 HTML 汇报。

## 解压后目录

```
pmmaster/
├── SKILL.md                 技能入口：能力清单、命令路由、安全边界（智能体调用约束 5 条）
├── README.md                快速开始：Windows 启动、建立正式项目、日常维护与汇报
├── 使用手册.md               完整手册：初始化、逐表填写、HTML 汇报、定时留档
├── 项目管理模板.xlsx          空白业务模板（含日历、下拉与填写规则）
├── requirements.txt         Python 依赖清单
├── run_demo.bat             一键启动只读演示
├── run_live_demo.bat        一键启动可保存修改的演示工作台
├── install_weekly_task.ps1  注册 Windows 定时周报任务
├── pm_core/                 Python 实现包：cli / server / store / schema / tracking /
│                            weekly / reports / excel_io / archive / calendar /
│                            project_report / fixtures + web 工作台页面
├── docs/                    字段字典、字段填写说明、飞书母版与智能体驱动、
│                            三表分工与维护指南、角色与生命周期工作流、设计原则、schema.sql
├── tests/                   10 个测试模块（unittest，168 项测试 + 64 子测试）
├── data/                    schema.json、2026 中国节假日表、日历源配置
└── demo/                    演示工作台.xlsx、示例汇报与周报 HTML、演示数据库
```

## 快速开始

1. 解压后进入 pmmaster 目录。
2. 按根目录 README.md「Windows 启动」一节建虚拟环境并装依赖：

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

3. 双击 `run_live_demo.bat` 先看演示效果；建立正式项目的初始化命令见 README.md「建立正式项目」。
4. 逐表填写与日常维护的完整流程见《使用手册.md》。

## 验证安装

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -m pm_core.cli check --db demo/demo.sqlite3
```

## 环境要求与已知边界

- Python：README 声明 3.11 或更高；开发环境实测 3.10.12 全部测试通过（声明偏保守，建议按 3.11+ 使用）。
- 平台：启动脚本为 .bat / .ps1，面向 Windows；其他平台可手动执行等价的 python 命令。
- WBS 编号自动补号属于飞书侧自动化能力（由飞书多维表格自动化触发），不随本包分发；本地 Excel 流程中编号由导入与导出流程维护。
- 飞书母版多维表格与本包 docs/飞书母版与智能体驱动.md 配套使用；本包本地运行不直连飞书。

## 与 Aily 技能市场版本的关系

本包与 Aily 技能市场「pmmaster」技能当前版本 v1.0.8 导出自同一安装目录，内容一致。作为 Aily 技能安装时由智能体按 SKILL.md 调度；本地解压使用时按上述快速开始操作——两者共用同一套代码、文档与测试。
