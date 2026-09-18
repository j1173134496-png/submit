# 新安高中部光伏月度填报自动化系统架构说明

## 当前代码状态

当前仓库已经完成本地 CLI 端到端 MVP、任务服务、Worker Adapter、OpenClaw WebChat 和字段级人工复核闭环；MVP-002 至 MVP-013、MVP-015 至 MVP-019 已完成。

已具备能力：

- 站点配置加载。
- 项目目录扫描、3 类来源 PDF 分类和月份识别。
- PDF 渲染与 OCR 原始结构接口。
- `ocr_raw` 字段映射并输出 `recognized_data.json`，保留 OCR 字段证据。
- 数据归一和业务校验并输出 `validation_report.json`。
- 低置信度、字段缺失和业务校验失败时输出 `review_report.json`，状态为 `need_review`。
- `confirmed_data.json` manual 重跑，并在重新校验通过后写 Excel。
- 文件型任务服务：`runtime/tasks/<task_id>/task.json`、`inputs/`、`outputs/`、`audit/events.jsonl`。
- 任务服务 CLI：`create-task`、`add-file`、`run-task`、`confirm-task`、`show-task`、`list-outputs`。
- OpenClaw / Codex Worker 合同：`worker_task.schema.json`、`worker_result.schema.json`、`gateway_contract.md`。
- 合同校验 CLI：`validate-worker-task`、`validate-worker-result`。
- 学校用电统计表结果副本写入。
- 汇总表格结果副本写入。
- `run-month` 端到端 CLI、`raw_ocr/*.json`、`process_log.txt` 和回归测试。
- OpenClaw 固定意图、会话附件暂存、结构化 Worker 派发和真实 WebChat 成功路径。
- `need_review -> prepare -> commit -> confirm_task -> completed` 两阶段字段确认闭环。

当前主要限制：

- 当前 OCR 路径使用本地可复现的 OCR 原始结构适配器，尚未接入外部 PaddleOCR/PP-StructureV3/MinerU/云 OCR 引擎。
- 企业微信智能机器人 WebSocket 方案和官方 SDK 预检已完成，当前正在实现独立 WeCom channel 的 Phase 1。
- 企业微信真实文本健康验证、PDF 收取、任务运行、字段复核和 Excel 回传尚未完成。

当前已有资产包括：

- PRD 与 MVP 交付规划文档。
- 新安高中部 2026 年 5 月 3 份来源 PDF。
- 学校用电统计表 Excel 模板。
- 汇总表格 Excel 模板。
- `src/submit_flow_agent/` 本地 MVP 代码。
- `tests/` 回归测试。
- `spec/contracts/` Gateway / Worker 合同文档。

## 企业微信入口边界

- 协议：企业微信智能机器人官方 WebSocket 长连接。
- SDK：`@wecom/aibot-node-sdk@1.0.7`，锁定版本和发布完整性。
- 归属：OpenClaw 独立 channel/plugin 负责 SDK 生命周期、可信 sender、独立 session、附件和出站消息；Submit Flow Agent 不接触企业微信凭证或协议。
- 凭证：只从仓库外受控运行环境注入 `WECOM_BOT_ID`、`WECOM_BOT_SECRET`，不得进入 Tool 参数、Git、日志或回执。
- 当前阶段：MVP-020 Phase 1 只允许文本事件进入独立 WeCom session，禁止 PDF、Submit Flow、Worker、复核和 Excel。
- 后续阶段：附件和出站文件能力必须先依据官方 SDK 文档单独评估和验收。

## 建议技术形态

MVP 优先采用本地命令行工具形态，避免一开始引入后台、数据库和复杂界面。

建议目录：

```text
src/
  submit_flow_agent/
    cli.py
    config.py
    file_scanner.py
    pdf_renderer.py
    ocr/
      base.py
      local_stub.py
    field_mapper.py
    normalizer.py
    validator.py
    excel_writer.py
    logger.py
config/
  sites.json
tests/
  fixtures/
```

## 模块边界

| 模块 | 职责 | 输出 |
| --- | --- | --- |
| 站点配置 | 读取新安高中部固定参数、模板名、sheet 名和单元格映射 | 站点配置对象 |
| 文件扫描 | 扫描项目目录，识别发电单、电费结算单、电量结算单和月份 | 文件清单 |
| PDF 渲染 | 将图片型 PDF 渲染为页面图片 | 页面图片路径 |
| OCR 适配器 | 调用 PaddleOCR/PP-StructureV3/MinerU 或本地 stub | 原始结构 JSON |
| 字段映射 | 从原始结构中抽取业务字段，保留来源和置信度 | recognized_data.json |
| 数据归一 | 统一日期、数字、单位和固定配置 | 月度数据对象 |
| 业务校验 | 校验字段完整性、公式、月份和金额 | validation_report.json |
| Excel 写入 | 写入两个 Excel 月份结果副本 | xlsx 输出文件 |
| 日志审计 | 记录输入、识别、校验、写入和异常 | process_log.txt |

## 配置边界

新安高中部配置至少包含：

- `site_key`: `xinan_high_school`
- `site_name`: `新安高中部`
- `project_name`: `新安中学（高中部）光伏发电项目`
- `meter_no`: `09001SF00000042508945216`
- `company_name`: `福域(深圳)新能源有限公司`
- `multiplier`: `150`
- `school_base_price`: `0.6998`
- `school_discount_rate`: `0.95`
- `detail_workbook`: `新安中学（高中部）光伏发电项目学校用电统计表.xlsx`
- `detail_sheet`: `1`
- `summary_workbook`: `汇总表格.xlsx`
- `summary_sheet`: `Sheet1`

## 识别接口约束

OCR 工具可替换，但下游接口固定为两层：

```text
OCR 原始结构 JSON
  -> 项目字段映射器
  -> recognized_data.json
```

原始结构 JSON 需要保留文本、坐标、来源文件、页码和置信度。业务字段 JSON 需要保留字段值、来源文件、页码、置信度和原始文本片段。

## Excel 模板核对结果

已核对当前模板：

- 学校用电统计表工作表为 `1`，最大区域约 A1:J7。
- 汇总表格包含 `Sheet1`、`Sheet2`、`Sheet3`，当前新安数据位于 `Sheet1`。
- 学校用电统计表关键单元格与 PRD 一致：`E2`、`H2:J2`、`A5:J7`。
- 汇总表格关键映射与 PRD 一致：`B:M` 列承载月份、读数、倍率、电量、电价、电费和合计。

## 测试策略

- 使用 2026 年 5 月样例作为固定回归。
- 文件扫描、配置加载、字段归一和业务校验优先做单元测试。
- Excel 写入测试读取输出副本，检查关键单元格和公式。
- 端到端测试覆盖成功路径、缺文件、字段缺失、公式失败、月份冲突。
