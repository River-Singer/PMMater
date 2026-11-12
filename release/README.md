# pmmaster Skill 开发交付

当前交付包为 `pmmaster-skill-v7-20261111-r2.zip`，包含新版 HTML 设计与 2026-11-11 飞书母版规则同步。解压后得到完整的 `pmmaster` 目录。`v7` 表示数据结构版本，`r2` 表示本次修订；相邻 manifest JSON 记录每个文件、源文件集合与整个压缩包的 SHA-256 校验值。

- 先看包内 `docs/README.md`，再按使用入口阅读 `SKILL.md` 和 `使用手册.md`。
- 飞书内部智能体：配置指令、知识材料、真实表字段、成员/日历/消息工具和宿主触发，按 `references/feishu-setup.md` 接入。
- 通用智能体：将整个 `pmmaster` 目录安装到其技能目录；离线使用 Excel，需要包内说明的 Python 依赖。
- `项目管理模板.xlsx` 用于新项目；`demo/` 是虚构示例，包含 WK 汇报、延期审议、月度资源和日期变更演示。

当前交付以智能体对话、Excel 与独立 HTML 为入口。解压后直接双击 `demo/本周项目汇报.html` 或 `demo/持续跟进与延期审议.html` 即可查看，文件内保留页签与层级交互，无需启动本地网页服务。`run_demo.bat` 可选用于另行生成离线材料；周报定时生成脚本仍保留，按需由宿主或本地任务调用生成命令。

默认不会发送消息。本次交付没有安装到真实飞书租户，也没有开启真实提醒。不要直接把虚构身份、链接或时刻用于正式项目。

## 从源码构建

完成仓库 README 中的测试和生成一致性检查后，在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/package_release.py --name pmmaster-skill-v7-20261111-r2
.\.venv\Scripts\python.exe -X utf8 scripts/package_release.py --name pmmaster-skill-v7-20261111-r2 --check
```

脚本只收集 `project/` 的源码、文档、模板与虚构演示，排除工作目录、缓存、凭据文件、备份以及非指定演示数据库。文件内容仍需发布前人工核对，模式扫描不能代替资料脱敏。压缩包内路径、权限和时间固定；在相同 Python/zlib 环境中，同一份来源可生成相同字节。源文件校验值不依赖机器路径、文件修改时间或打包时间。

仓库通过 `.gitattributes` 保留已审阅文件的原始换行字节，避免 Windows 与其他系统检出时自动转换换行而改变校验值。调整换行也是来源变更，需要重新生成对应修订包。

已有同名交付包内容不同时，脚本拒绝覆盖；应使用新的修订名。`--check` 会把现有压缩包及 manifest 与当前源文件逐一核对，不修改文件。上次修订包仅在本地留作对照，不作为本次仓库交付。
