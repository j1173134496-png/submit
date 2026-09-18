# WeCom Message Templates

## Template Principles

- Every message must include `task_id` when a task exists.
- Review and confirmation messages must include field names and expected confirmation format.
- Messages must never ask users to type shell commands or free-form Worker instructions.
- Gateway must convert user replies into structured `confirmed_data.json` or task-service actions.

## 1. Task Created

```text
已创建月度填报任务

任务：{task_id}
站点：{site_name}
月份：{month}
状态：等待上传来源文件

请上传以下 3 份 PDF：
1. 发电单
2. 电费结算单
3. 电量结算单
```

## 2. Missing Files

```text
任务 {task_id} 还缺少来源文件

已收到：
{received_files}

仍缺少：
{missing_files}

请继续上传同一月份的 PDF 文件。系统只接受来源 PDF，不接受截图、Excel 或压缩包作为本阶段输入。
```

## 3. File Received

```text
已收到文件

任务：{task_id}
文件类型：{file_type_label}
文件名：{file_name}
当前状态：{task_status}

{next_hint}
```

`next_hint` 示例：

```text
还缺少：电费结算单、电量结算单。
```

或：

```text
3 份文件已收齐，即将开始识别和校验。
```

## 4. Ready To Run

```text
任务 {task_id} 文件已收齐

站点：{site_name}
月份：{month}
状态：准备处理

系统将开始 OCR、字段映射、业务校验和 Excel 写入。处理完成后会推送结果。
```

## 5. Running

```text
任务 {task_id} 正在处理

当前步骤：{stage}
请稍候。系统不会覆盖原始 PDF 或 Excel 模板。
```

`stage` 可取值：

```text
文件扫描
OCR 原始结构生成
字段映射
业务校验
Excel 写入
```

## 6. Completed

```text
任务 {task_id} 已完成

站点：{site_name}
月份：{month}
状态：校验通过，Excel 已生成

输出文件：
{output_files}

审计日志：audit/events.jsonl
```

如果 Gateway 支持文件回传：

```text
已生成：
1. 学校用电统计表：{detail_workbook}
2. 汇总表格：{summary_workbook}

请下载附件核对。
```

如果 Gateway 只支持链接：

```text
下载链接：
{download_links}

链接有效期：{expires_at}
```

## 7. Need Review

```text
任务 {task_id} 需要人工复核

原因：发现低置信度、字段缺失或业务校验失败。
复核报告：{review_report}

待确认问题：
{review_issues}

请按以下格式回复确认值：
确认 {field_name}={value}，原因：{reason}
```

`review_issues` 示例：

```text
1. 字段：grid_fee
   原因：low_confidence
   当前值：407.29
   置信度：0.80
   来源：福域电费结算单202605_1.pdf 第 1 页
   原文：上网电费 407.29 元
```

## 8. Confirmed Data Received

```text
已收到人工确认

任务：{task_id}
确认人：{confirmed_by}
确认字段：{confirmed_fields}

系统将使用 confirmed_data.json 重新校验。只有校验通过才会写 Excel。
```

## 9. Manual Rerun Completed

```text
任务 {task_id} 已完成复核后重跑

状态：校验通过，Excel 已生成
确认字段：{confirmed_fields}

输出文件：
{output_files}
```

## 10. Manual Rerun Still Needs Review

```text
任务 {task_id} 复核后仍需人工处理

说明：人工确认值已写入，但重新校验仍未通过。
复核报告：{review_report}

问题：
{review_issues}
```

## 11. Failed

```text
任务 {task_id} 运行失败

失败原因：{error}
状态文件：task.json
审计日志：audit/events.jsonl
处理日志：{process_log}

请联系运维或重新创建任务。系统未写入 Excel 结果。
```

## 12. Unsupported Text

当用户发送无法映射为白名单动作的文本：

```text
暂不支持该指令

你可以发送：
- 创建 {month} {site_name} 任务
- 上传 3 份来源 PDF
- 查询任务 {task_id}
- 确认 字段=值，原因：...

系统不会执行自然语言中的命令或脚本。
```

## 13. Attachment Download Failed

```text
附件下载失败

文件：{file_name}
原因：{error}

请重新上传该 PDF。若多次失败，请联系管理员检查企业微信文件下载权限。
```

## 14. File Type Rejected

```text
文件已拒绝

文件：{file_name}
原因：本阶段只接受 PDF 来源单据。

请上传发电单、电费结算单、电量结算单 PDF。
```
