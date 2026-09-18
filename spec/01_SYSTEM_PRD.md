# 新安高中部光伏月度填报自动化系统 PRD 索引

## 来源文档

完整 PRD 位于：

```text
深圳新安中学高中部650KW分布式光伏项目/新安高中部光伏月度填报自动化系统 PRD.md
```

MVP 交付规划位于：

```text
深圳新安中学高中部650KW分布式光伏项目/新安高中部光伏月度填报自动化系统 MVP 交付规划表.md
```

当前跨项目实施口径位于：

```text
需求/2026-07-10需求/01_光伏填报Agent系统_PRD_V1.4.md
需求/2026-07-10需求/02_MVP-018至MVP-023实施计划_V1.4.md
```

当前状态：本地填报、OpenClaw WebChat 和字段级人工复核闭环已完成；正在执行 MVP-020 企业微信智能机器人 WebSocket 长连接接入。企业微信 Phase 1 仅做独立 channel 和文本健康验证，不触发业务任务或 Worker。

## MVP 目标

先跑通新安高中部 2026 年 5 月样例，实现：

```text
3 份来源 PDF
  -> OCR/字段识别
  -> 统一月度数据
  -> 业务校验
  -> 2 个 Excel 月份副本
  -> 识别 JSON、校验报告、处理日志
```

## 输入文件

```text
福域发电单202605-2.pdf
福域电费结算单202605_1.pdf
福域电量结算单202605_1-2.pdf
```

## 输出文件

```text
outputs/新安高中部/2026-05/
  recognized_data.json
  validation_report.json
  process_log.txt
  新安中学（高中部）光伏发电项目学校用电统计表_2026-05.xlsx
  汇总表格_2026-05.xlsx
```

## 统一月度数据

MVP 统一输出字段包括：

- `site_key`
- `site_name`
- `project_name`
- `month`
- `meter_no`
- `period_start`
- `period_end`
- `previous_reading`
- `current_reading`
- `multiplier`
- `total_generation_kwh`
- `grid_export_kwh`
- `grid_price`
- `grid_fee`
- `school_base_price`
- `school_discount_rate`
- `company_name`
- `statement_date`

## Excel 写入摘要

学校用电统计表：

- 工作表：`1`
- 写入标题、起止日期、表号、B5:F5 数值、G5:J5 公式、公司名称、填表日期。

汇总表格：

- 工作表：`Sheet1`
- 按月份更新已有行或追加新行。
- B:M 列写入月份、读数、倍率、电量、电价、电费和合计公式。

## 阻断条件

出现以下情况时不自动写入最终 Excel：

- 3 份来源文件缺失或重复。
- 必填字段缺失。
- 发电量公式校验失败。
- 月份冲突。
- 上网电量大于月总发电量。
- 金额差异超过容差。
- 目标 Excel 文件或工作表不存在。
- 关键字段置信度低于阈值且未人工确认。

