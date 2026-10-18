"""将 65 篇候选文档的审校结果生成为可复核台账和正式知识副本。

原始候选目录始终只读；C 类文档只记录在台账中，不写入正式 knowledge 目录。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / "采购Agent_知识库统一候选集_V1-V4_65篇_待审校"
AUDIT = ROOT / "采购Agent_知识库审校_V1-V4_20261013"
FORMAL = ROOT / "采购Agent_正式知识库_v1"
REVIEWED_AT = "2026-10-13"


# 这些品类文章只引用通用项目首页，不足以支撑文中的具体技术判断。
C_IDS = {
    "PKB-V3-030": "显示器与多屏技术判断缺少品类直接来源；现有通用采购项目首页不构成有效支撑。",
    "PKB-V3-033": "交换机、PoE和网络容量判断缺少品类技术来源。",
    "PKB-V3-034": "WiFi/AP 规格、带宽与部署判断缺少品类技术来源。",
    "PKB-V3-035": "防火墙、吞吐、安全能力等判断缺少权威技术来源。",
    "PKB-V3-036": "UPS 容量、续航与电气安全判断缺少品类直接来源。",
    "PKB-V3-037": "服务器/NAS 容量、RAID与备份判断缺少品类技术来源。",
    "PKB-V3-038": "视频会议音视频规格判断缺少品类直接来源。",
    "PKB-V3-039": "投影与会议大屏的亮度、尺寸及部署判断缺少品类来源。",
    "PKB-V3-041": "文件柜、密集架承重与安全判断缺少直接来源。",
    "PKB-V3-042": "文具材质、安全与耐用性判断缺少品类直接来源。",
    "PKB-V3-043": "移动存储的耐用、加密和数据安全判断缺少权威来源。",
    "PKB-V3-044": "监控录像、隐私与存储规划判断缺少品类及合规来源。",
    "PKB-V3-045": "保修、维保和备件策略缺少直接专业来源。",
}


SPECIAL = {
    "KB-002": ("修订后通过", "原文的必填信息列表遗漏 item，已补入物品名称/明确规格，与 PRD 五个必填字段一致。", "是"),
    "KB-003": ("修订后通过", "删除“fixture 字段”的错误实现声称，改为有权人工角色通过服务端凭证核验动作记录状态。", "是"),
    "KB-007": ("修订后通过", "保留商品目录的已实现字段、权限和价格边界；将重复的品类选型段落收敛为 B 类文档引用。", "否"),
    "KB-010": ("修订后通过", "原文容易让人误以为向量检索已接入；已明确当前仅有版本化 JSON 规则查询，文档 RAG 尚未实现。", "是"),
    "KB-011": ("修订后通过", "保留 FAQ 这一快速查询层，增加规则原文指引，不再把完整解释重复写入速查文档。", "否"),
    "PKB-V4-048": ("修订后通过", "原文“预算不足不能直接创建”与真实实现不精确；后端允许草稿，但在提交时阻断。已按真实状态改写。", "是"),
    "PKB-V4-058": ("修订后通过", "精确化黑名单语义：可保留查询/草稿证据，但后端提交会进入供应商阻断，不能进入正常审批。", "否"),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_assessment(row: dict[str, str], klass: str) -> str:
    if klass == "A":
        return "本地权威源已核对：正式 PRD、backend.py 和 procurement_rules_v1.json"
    if klass == "C":
        return "链接可访问，但仅为通用项目首页，不是该品类的有效技术来源"
    url = row["source_url"]
    if "github.com" in url:
        return "已核对可访问的公开二级参考；只允许支撑通用方法，不作为企业制度"
    if "microsoft.com" in url or "energystar.gov" in url or "epa.gov" in url or "gov.uk" in url:
        return "已核对可访问的官方公开参考；仅按文档明示的适用范围使用"
    return "已核对可访问的专业框架参考；不作为本模拟企业制度"


def decision(row: dict[str, str]) -> dict[str, str | bool]:
    doc_id = row["id"]
    if doc_id in C_IDS:
        return {
            "class": "C", "result": "待核实", "reason": C_IDS[doc_id],
            "conflict": "否", "index_allowed": False, "dedup": "保留原篇，待补品类来源后再审",
        }
    klass = "A" if row["version"] == "V1" else "B"
    if doc_id in SPECIAL:
        result, reason, conflict = SPECIAL[doc_id]
    elif klass == "A":
        result, reason, conflict = (
            "通过",
            f"已与 PRD/后端真实规则核对；《{row['title']}》未发现未确认企业制度。",
            "否",
        )
    else:
        result, reason, conflict = (
            "通过",
            f"《{row['title']}》作为通用采购知识，已明示非企业制度、适用边界和外部参考。",
            "否",
        )
    if row["version"] == "V2":
        dedup = "保留方法论层；压缩重复的来源/边界段落"
    elif row["version"] == "V4":
        dedup = "保留情景处置层；合并各篇重复的使用限制"
    elif row["version"] == "V3":
        dedup = "保留品类选型层；与通用参数法互补"
    else:
        dedup = "保留企业规则层；通用选型内容指向 B 类"
    return {
        "class": klass, "result": result, "reason": reason,
        "conflict": conflict, "index_allowed": True, "dedup": dedup,
    }


def add_review_frontmatter(text: str, review: dict[str, str | bool], source: str) -> str:
    if not text.startswith("---\n"):
        raise ValueError("文档缺少 YAML frontmatter")
    marker = text.find("\n---", 4)
    if marker < 0:
        raise ValueError("文档 frontmatter 未闭合")
    frontmatter = text[4:marker]
    # V2–V4 原稿已有 review_status，必须替换而不是写入重复 YAML 键。
    if re.search(r"(?m)^review_status:", frontmatter):
        frontmatter = re.sub(
            r"(?m)^review_status:.*$", "review_status: approved_for_index", frontmatter
        )
    else:
        frontmatter += "\nreview_status: approved_for_index"
    frontmatter += (
        f"\nreview_class: {review['class']}"
        "\nindex_allowed: true"
        f"\nreviewed_at: '{REVIEWED_AT}'"
        f"\nsource_validation: '{source}'"
    )
    return "---\n" + frontmatter + text[marker:]


def revise_body(doc_id: str, version: str, text: str) -> str:
    replacements = {
        "KB-002": (
            "用户需要提供数量、单价、供应商和采购用途等必要信息。",
            "用户需要提供物品名称（或明确规格）、数量、单价、供应商和采购用途等必要信息。",
        ),
        "KB-003": (
            "当前演示系统通过 fixture 字段或手工标注的元数据表达凭证状态",
            "当前演示系统由有权限的人工审批角色通过服务端凭证核验动作记录凭证状态",
        ),
        "PKB-V4-048": (
            "预算不足不能直接创建被认定为合规的采购；不得通过人为拆分金额规避审批门槛。",
            "预算不足时可保留或形成草稿用于补充信息，但后端会在提交时阻断；不得把该草稿称为已提交或合规完成，也不得通过人为拆分金额规避审批门槛。",
        ),
        "PKB-V4-058": (
            "黑名单供应商必须阻断相关采购，不能仅凭价格优势绕过。",
            "黑名单供应商可保留查询或草稿证据，但提交时必须进入供应商阻断，不能仅凭价格优势绕过并进入正常审批。",
        ),
    }
    if doc_id in replacements:
        old, new = replacements[doc_id]
        if old not in text:
            raise ValueError(f"{doc_id} 待修订文本未找到")
        text = text.replace(old, new, 1)

    if doc_id == "KB-007":
        text = re.sub(
            r"## 办公电脑选型.*?(?=## 目录没有商品时)",
            "## 通用选型知识\n办公电脑、打印与网络设备的选型建议不是企业规则，由 B 类品类文档单独维护，本篇不再重复。具体型号、可采购性和参考价始终以当前商品 API 为准。\n\n",
            text,
            flags=re.S,
        )
    if doc_id == "KB-010":
        text = text.replace(
            "知识检索主要回答采购流程、填写规范、审批金额规则、预算扣减、供应商状态、商品选型常识和异常处理。回答应基于有效文档片段，展示文档标题和章节。",
            "当前 Agent 仅通过本地版本化 JSON 文件查询已确认规则，这批 Markdown 文档尚未接入向量检索或 Agent 运行时。未来接入后，检索可覆盖采购流程、填写规范、审批规则和通用采购知识；回答应展示文档标题和章节。",
            1,
        ).replace(
            "再修改文档并重建向量索引",
            "再修改文档；只有在已存在索引时才按审校清单重建索引",
            1,
        )
    if doc_id == "KB-011":
        anchor = "> 适用范围：采购 Agent 演示系统。含通用建议，不构成企业强制制度；涉及业务规则以正式 PRD 和模拟器为准。"
        text = text.replace(anchor, anchor + "\n\n> 本篇是 FAQ 速查层；规则依据与完整解释见 KB-001～KB-010，不以本篇代替原始条款。", 1)

    # V2 保留方法论，将 16 篇重复的三条声明压缩为两条，仍保留来源 URL。
    if version == "V2":
        text = re.sub(
            r"## 参考与使用提示\n\n- 参考资料：([^\n]+)\n- 本文为中文独立改写与业务说明，不是原文翻译或官方认可的制度。\n- 外部资料是知识线索，正式使用前应复核其时效、适用市场与本地正式PRD；未查询实时数据时不得编造价格、资质、库存或政策。",
            "## 来源与适用范围\n\n- 外部参考：\\1\n- 本文是独立中文整理，不是企业制度或原文翻译；实时价格、资质、库存、状态与政策必须通过有效业务源复核。",
            text,
        )
    # V4 保留情景层，合并 20 篇重复的使用限制，不删除个别情景。
    if version == "V4":
        text = re.sub(
            r"## 知识使用限制\n\n- 不得将本篇采购建议误作系统已支持的写操作，也不得凭本篇内容修改任何订单、审批、供应商或预算数据。\n- 涉及合同法律责任、税务、进出口或安全合规的具体争议，应由具备职责的人员根据合同和适用法规审查。\n- 如果用户询问自身采购记录或供应商当前状态，必须调用现有业务API，而非用本文推断。",
            "## 知识使用限制\n\n本篇只提供通用情景建议，不代表系统写能力；实时记录和供应商状态必须查业务 API，合同、法律、税务、进出口或安全争议必须交有权人员复核。",
            text,
        )
    return text


def render_report(records: list[dict[str, object]]) -> str:
    rows = []
    for item in records:
        rows.append(
            f"| {item['id']} | {item['title']} | {item['class']} | {item['result']} | "
            f"{item['reason']} | {item['conflict']} | {'是' if item['index_allowed'] else '否'} |"
        )
    return f"""# 采购知识库 V1–V4 审校报告

**审校日期：** {REVIEWED_AT}  
**候选集：** `{CANDIDATE}`  
**正式知识目录：** `{FORMAL / 'knowledge'}`

## 1. 结论

- 候选文档：65 篇；原件哈希全部通过候选集 manifest 校验，未覆盖原件。
- 允许入库：52 篇，其中 A 类 11 篇、B 类 41 篇。
- 待修订/待核实：13 篇，均为 C 类，未复制进正式 knowledge 目录。
- 发现内容冲突或实现越界表述：4 篇（KB-002、KB-003、KB-010、PKB-V4-048）；已在正式副本中修正，未解决冲突为 0。
- 本轮未构建/重建索引，未修改 `agent_runtime.py`、`agent_tools.py`、`backend.py` 或任何业务数据。

## 2. 真实实现核对

- 正式 PRD 要求新建必填 `item, quantity, unit_price, supplier_name, purpose`；品类仍是单条目，咨询不得触发写入。
- 后端以 `<10000` 路由 M01、`>=10000` 路由 M02；高额凭证在人工批准前核验。
- 预算在提交时校验，仅首次成功创建 PO 时原子扣减；黑名单在提交时阻断，新供应商进入人工准入。
- 已有商品目录读 API，价格是模拟参考价；历史价格只来自已创建 PO 的真实模拟记录。
- 当前知识咨询是 `procurement_rules_v1.json` 的本地关键词匹配；依赖仅有 OpenAI SDK，未找到中文 Embedding、切块器、向量库、索引构建器或 RAG Retriever。

## 3. 重复内容处理

- 保留 A 类“企业规则原文层”和 FAQ “快速查询层”；FAQ 新增原文指引。
- KB-007 只保留商品目录真实能力，通用品类选型改指向 B 类，避免与 V3 重复。
- V2 保留通用方法论层，压缩重复的来源/边界段落。
- V4 保留具体情景层，将 20 篇重复的三条使用限制合并为一个完整段落，未删除整篇。

## 4. 逐篇台账

| ID | 标题 | 分类 | 审校结果 | 修改/暂缓原因 | 冲突 | 允许入库 |
|---|---|---|---|---|---|---|
{chr(10).join(rows)}

## 5. 证据路径

- 逐篇机器可读台账：`{AUDIT / '逐篇审校台账.csv'}`
- 原件哈希校验：`{AUDIT / '原件哈希核验.csv'}`
- 外部来源核验：`{AUDIT / '来源核验记录.md'}`
- RAG 接入准备：`{AUDIT / 'RAG集成准备.md'}`
- 正式知识 manifest：`{FORMAL / 'manifest.csv'}`
"""


def main() -> None:
    rows = list(csv.DictReader((CANDIDATE / "manifest.csv").open(encoding="utf-8-sig", newline="")))
    if len(rows) != 65:
        raise SystemExit(f"预期 65 篇，实际 {len(rows)} 篇")

    (FORMAL / "knowledge" / "A").mkdir(parents=True, exist_ok=True)
    (FORMAL / "knowledge" / "B").mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    reviewed_manifest: list[dict[str, object]] = []
    original_hashes: list[dict[str, str]] = []

    for row in rows:
        src = CANDIDATE / row["path"]
        actual_hash = sha256(src)
        if actual_hash != row["sha256"]:
            raise SystemExit(f"原件哈希与候选 manifest 不一致：{src.name}")
        original_hashes.append({"id": row["id"], "path": row["path"], "sha256": actual_hash, "verified": "PASS"})
        review = decision(row)
        source = source_assessment(row, str(review["class"]))
        record: dict[str, object] = {
            "id": row["id"], "title": row["title"], "version": row["version"],
            "original_path": row["path"], "class": review["class"], "result": review["result"],
            "reason": review["reason"], "conflict": review["conflict"],
            "source_assessment": source, "dedup_treatment": review["dedup"],
            "index_allowed": review["index_allowed"], "reviewed_path": "",
        }
        if review["index_allowed"]:
            text = src.read_text(encoding="utf-8")
            text = revise_body(row["id"], row["version"], text)
            text = add_review_frontmatter(text, review, source)
            dst = FORMAL / "knowledge" / str(review["class"]) / src.name
            dst.write_text(text, encoding="utf-8", newline="\n")
            rel = dst.relative_to(FORMAL).as_posix()
            record["reviewed_path"] = rel
            reviewed_manifest.append({
                "id": row["id"], "title": row["title"], "class": review["class"],
                "path": rel, "source_url": row["source_url"],
                "original_sha256": actual_hash, "reviewed_sha256": sha256(dst),
                "reviewed_at": REVIEWED_AT, "index_allowed": True,
            })
        records.append(record)

    if sum(bool(item["index_allowed"]) for item in records) != 52:
        raise SystemExit("放行数量不是预期的 52")

    def write_csv(path: Path, data: list[dict[str, object]]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(data[0]))
            writer.writeheader()
            writer.writerows(data)

    write_csv(AUDIT / "逐篇审校台账.csv", records)
    write_csv(AUDIT / "原件哈希核验.csv", original_hashes)
    write_csv(FORMAL / "manifest.csv", reviewed_manifest)
    (AUDIT / "逐篇审校台账.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )
    (FORMAL / "manifest.json").write_text(
        json.dumps({"total": 52, "A": 11, "B": 41, "documents": reviewed_manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8", newline="\n",
    )
    (AUDIT / "审校报告.md").write_text(render_report(records), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
