---
id: PKB-V2-013
title: RFQ询价文件基本结构
version: 2.0-draft
language: zh-CN
knowledge_type: general_procurement_guidance
category: 询价与比价
source_url: https://github.com/arunbalajiraju-proc/procurement-ai-assistant/blob/main/skills/rfp-drafting-skill.md
source_type: public_reference
company_policy: false
review_status: approved_for_index
review_class: B
index_allowed: true
reviewed_at: '2026-10-13'
source_validation: '已核对可访问的公开二级参考；只允许支撑通用方法，不作为企业制度'
---

# RFQ询价文件基本结构

> 适用边界：采购通用方法与风险识别。不是本模拟企业的强制制度，也不证明当前Agent实现了相关业务操作。具体企业审批、供应商状态、申请数据必须查询现有有效规则或业务API。

## 询价请求的组成

典型RFQ应包含采购范围、物品/服务规格、数量、交付地点和期限、报价币种与税费口径、报价有效期、质保售后、提交时间以及问题澄清渠道。

**业务示例：**缺少统一报价口径时，三家供应商给出的价格可能无法直接比较。

## 必须明确的价格口径

让供应商分别列明含税/未税单价、税费、运输、安装、培训、售后、可选项以及一次性费用。

**业务示例：**仅比较商品裸价，可能忽略上门安装和后期维护成本。

## 询价期间的变更

如果更改需求，应将同一版本要求传递给所有参与比较的供应商，记录截止时间和变更说明。

**业务示例：**不得凭对某一家供应商的私人解释默默更换评价口径。

## 来源与适用范围

- 外部参考：https://github.com/arunbalajiraju-proc/procurement-ai-assistant/blob/main/skills/rfp-drafting-skill.md
- 本文是独立中文整理，不是企业制度或原文翻译；实时价格、资质、库存、状态与政策必须通过有效业务源复核。
