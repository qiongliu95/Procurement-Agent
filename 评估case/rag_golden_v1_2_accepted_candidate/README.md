# 采购RAG Golden正式候选

版本：rag-golden-1.2.1-accepted-candidate；46条。独立候选，未替换旧Case、未同步Airtable、未运行RAG。

详见`E:\AI-Research\Procurement-Agent\procurement_eval_v08\evaluation\reports\rag_golden_v1_2_acceptance\final_quality_acceptance_report.md`。JSONL为主数据，CSV为可读镜像，evaluation_schema.json为结构约束。

仅REQUIRED评分点参与严格通过，接受同义；SUPPLEMENTARY事实和OPTIONAL点不进入必要证据召回分母。来源目录保留补充证据，不能按全部doc_id强制双源。原文到Chunk映射未实现，不得按文档命中算事实召回。
