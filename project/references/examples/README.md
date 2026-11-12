# 虚构填写与执行样例

这里全部是格式示例，不是真实项目指令。业务编号与 [demo](../../demo/README.md) 对应；真实执行必须按当前记录回读 expected、替换来源和身份，不能把 example.invalid 当真实证据。

| 文件 | 用法 |
|---|---|
| [excel-config.json](excel-config.json) | 离线 mode=excel，默认 disabled；配合 --db，消息能力由宿主另接 |
| [daily-confirmation.json](daily-confirmation.json) | 下午半日安排、小时量与未来休假无变化，供 attendance-confirm |
| [weekly-commitment.json](weekly-commitment.json) | 有来源的候选周成果，供 actions-run 预览；计划会实际确认后再更新 confirmed/confirmed_at/confirmed_by_id |
| [meeting-actions.json](meeting-actions.json) | 延期说明/措施的结构示例，目标编号需替换 |
| [friday-decision.json](friday-decision.json) | 具名周五决定，expected 保留原值，approval 引用实际有权决定 |
| [booking.json](booking.json) | 已确定具体时段与名单的预约；需飞书/原生工具，不由 Excel 产生外部日程 |

```text
python -m pm_core.cli actions-run --config references/examples/excel-config.json --db demo/demo.sqlite3 --ledger workspace/example-runtime.sqlite3 --file references/examples/weekly-commitment.json
python -m pm_core.cli agent-tick --config references/examples/excel-config.json --db demo/demo.sqlite3 --ledger workspace/example-runtime.sqlite3
```

这两条是预览。预约需要飞书配置中真实 calendar_id、organizer_id 与 allowed_attendees，book 操作也须在授权范围；样例身份不能直接调用真实平台。周五示例表示虚构未来场景，不能提前写成实际会议决定。
