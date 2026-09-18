# WeCom Entry Prestudy

## Conclusion

首期推荐采用 **企业微信自建应用作为主入口**，OpenClaw Gateway 作为企业微信回调后的业务网关；**应用机器人可作为同一自建应用下的交互形态补充**；**群机器人 webhook 只建议作为通知补充，不作为主文件入口**。

原因：本系统需要可靠接收 3 份 PDF、下载附件、识别用户身份、绑定 `task_id`、记录审计事件，并支持 `need_review` 后的人工确认重跑。群机器人 webhook 更适合向群里发通知，通常不适合作为需要下载用户附件和做身份审计的主入口。

## Entry Options

| 入口形态 | 附件上传/下载 | 用户身份 | 适合审计 | 结果回传 | 配置复杂度 | MVP 风险 | 结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 自建应用 | 适合接收消息事件和媒体文件；Gateway 可按 media/file id 下载并保存 | 可获得企业微信用户标识，适合映射 task operator | 强，可写入 task.json 和 events.jsonl | 可向用户或会话推送任务状态 | 中 | 中 | 主入口推荐 |
| 应用机器人 | 适合同一应用内的机器人式交互；能力取决于自建应用配置 | 可沿用应用消息上下文中的用户/会话 | 中到强 | 适合交互式提示和复核消息 | 中 | 中 | 可作为交互形态补充 |
| 群机器人 webhook | 通常偏单向通知；不适合做可靠附件下载主链路 | 用户身份弱，更多是群上下文 | 弱，难绑定确认人 | 适合发送群通知 | 低 | 高 | 只做通知补充 |

## Recommended MVP Shape

```text
企业微信自建应用
  -> OpenClaw Gateway
  -> 文件型任务服务 runtime/tasks/<task_id>/
  -> Codex Worker 结构化任务
  -> run_task / confirm_task / show_task / list_outputs
  -> 企业微信状态消息
```

### Why Not Webhook As Main Entry

群机器人 webhook 的优势是配置简单、通知容易；但它不适合作为主入口：

- 很难稳定表达“谁上传了哪份文件、谁确认了哪个字段”。
- 附件下载和文件权限链路不适合承载完整任务输入。
- 容易把群文本误当作业务指令，绕过 MVP-013 Worker 合同。
- 对 review/manual 这类需要用户身份审计的流程支持弱。

## Attachment Download And Save Plan

Gateway 负责从企业微信消息事件里提取附件标识和元数据，不把附件直接交给 Worker。

建议流程：

1. Gateway 收到用户消息事件。
2. 如果是文件消息，读取企业微信提供的文件标识、文件名、大小、发送人、会话。
3. Gateway 使用企业微信访问凭证下载附件到临时区。
4. Gateway 执行文件安全检查：扩展名必须为 `.pdf`，大小上限按运维配置，文件名保留原名但路径做规范化。
5. Gateway 根据文件名或用户上下文调用任务服务 `add-file`，保存到 `runtime/tasks/<task_id>/inputs/`。
6. 任务文件齐全后，Gateway 派发 Worker `run_task`。

### Retry And Failure Handling

| 场景 | 处理 |
| --- | --- |
| 下载失败 | Gateway 重试有限次数，失败后发“附件下载失败”消息，不创建 Worker 任务。 |
| 文件类型不支持 | 拒绝登记，提示只支持 PDF 来源单据。 |
| 文件月份不匹配 | 拒绝登记，提示重新上传同月份 3 份文件。 |
| 缺少文件 | 保持 task status=`collecting_files`，提示缺哪类文件。 |
| 文件重复 | Gateway 提示是否替换；正式接入前默认拒绝或覆盖策略需确认。 |

## Identity Mapping

Gateway 必须把企业微信身份映射为任务审计字段，但不引入复杂用户系统。

建议最小字段：

```json
{
  "wecom_user_id": "zhangsan",
  "wecom_user_name": "张三",
  "wecom_conversation_id": "single_or_group_id",
  "wecom_corp_id": "corp_xxx",
  "role_hint": "operator"
}
```

写入位置：

- `task.json`: 可在后续兼容增加 `operator`、`source_channel="wecom"`、`conversation_id`。
- `audit/events.jsonl`: `task_created`、`file_received`、`confirmed_data_received` 事件必须带用户上下文。
- `confirmed_data.json`: `confirmed_by` 使用企业微信用户 id，`confirmed_at` 使用 Gateway 服务时间。

MVP-014 不修改现有 `task.json` 合同，只定义正式接入时的向后兼容字段。

## Task Service / Worker Mapping

企业微信入口不能直接调用业务引擎。Gateway 只能调用任务服务或派发 MVP-013 Worker 合同。

| 企业微信用户行为 | Gateway 行为 | 任务服务动作 | Worker action |
| --- | --- | --- | --- |
| 发送“创建 2026-05 新安高中部任务” | 解析站点和月份，创建任务 | `create-task` | 无 |
| 上传发电单 PDF | 下载附件，登记文件 | `add-file generation_statement` | 无 |
| 上传电费单 PDF | 下载附件，登记文件 | `add-file fee_statement` | 无 |
| 上传电量单 PDF | 下载附件，登记文件 | `add-file energy_statement` | 无 |
| 三份文件齐全 | 派发结构化任务 | 无 | `run_task` |
| 用户查看进度 | 查询 task.json | `show-task` | `show_task` 可选 |
| 用户查看输出 | 列出 outputs | `list-outputs` | `list_outputs` 可选 |
| 用户确认复核字段 | 写 `confirmed_data.json` | 无 | `confirm_task` |

## Result Handling

| Worker result status | Gateway message |
| --- | --- |
| `completed` | 发送完成摘要、输出清单、Excel 文件或下载链接。 |
| `need_review` | 发送复核摘要、问题字段、当前值、来源文本、确认格式。 |
| `failed` | 发送失败原因、task_id、process_log 路径或运维线索。 |
| `collecting_files` | 提示缺少的来源文件类型。 |
| `ready_to_run` | 提示文件已收齐，可以开始处理。 |
| `running` | 提示任务处理中。 |

## Security Boundary

- 企业微信文本只由 Gateway 解释为“创建任务、上传文件、确认字段、查询状态”等固定意图。
- Gateway 派给 Worker 的必须是 `worker_task.schema.json` 合法 JSON。
- Worker 只允许执行 `run_task`、`confirm_task`、`show_task`、`list_outputs`。
- Worker 不接收 `command`、`shell`、`prompt`、`user_text`、`python`、`sql` 等字段。
- 任何低置信度、字段缺失、业务校验失败都必须进入 `need_review`，不能为了自动化率直接写 Excel。
- Excel 写入仍由 `validation_report.json.can_write_excel=true` 控制。
- 企业微信凭证、密钥、回调 token 不写入仓库。

## Out Of Scope For This Stage

- 正式企业微信生产入口。
- 真实回调 URL、Token、EncodingAESKey、Secret 配置。
- 企业微信 OAuth 或复杂用户权限系统。
- 数据库后台、Web 管理端、审批流。
- 多人协同复核和权限审批。
- 群机器人作为主文件入口。
- Worker 解释用户自然语言命令。

## Open Questions Before Production

- 企业微信最终入口采用单聊应用、群聊应用，还是两者并存。
- 附件最大大小、下载超时和重试次数。
- 任务是否允许用户替换已上传文件。
- 企业微信用户 id 与内部 operator 的映射表在哪里维护。
- Excel 输出是直接回传文件、生成内网下载链接，还是仅通知人工到共享目录取件。
