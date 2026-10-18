"""采购知识库的本地加载、切块、向量索引与检索。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


MODEL_ID = "BAAI/bge-small-zh-v1.5"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
EXPECTED_DOCUMENTS = 52
EXPECTED_CLASSES = {"A": 11, "B": 41}


class KnowledgeIndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeChunk:
    chunk_id: str
    doc_id: str
    title: str
    heading: str
    review_class: str
    source: str
    source_url: str
    text: str
    document_path: str
    document_sha256: str

    def embedding_text(self) -> str:
        return f"{self.title}\n{self.heading}\n{self.text}"

    def public(self, score: float) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "title": self.title,
            "heading": self.heading,
            "class": self.review_class,
            "source": self.source,
            "source_url": self.source_url or None,
            "text": self.text,
            "score": round(float(score), 6),
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise KnowledgeIndexError("KNOWLEDGE_FRONTMATTER_REQUIRED")
    end = text.find("\n---", 4)
    if end < 0:
        raise KnowledgeIndexError("KNOWLEDGE_FRONTMATTER_INVALID")
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("'\"")
    return metadata, text[end + 4 :].strip()


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    """优先在段落边界切分；只有单段过长时才使用字符窗口。"""
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                parts.append(current)
                current = ""
            step = max(1, max_chars - overlap_chars)
            for start in range(0, len(paragraph), step):
                piece = paragraph[start : start + max_chars].strip()
                if piece:
                    parts.append(piece)
                if start + max_chars >= len(paragraph):
                    break
            continue
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            parts.append(current)
            tail = current[-overlap_chars:].strip() if overlap_chars else ""
            current = (tail + "\n\n" + paragraph).strip() if tail else paragraph
    if current:
        parts.append(current)
    return parts


def _section_chunks(body: str, max_chars: int, overlap_chars: int) -> list[tuple[str, str]]:
    title = ""
    heading = "概要"
    content: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        text = "\n".join(content).strip()
        if text:
            sections.append((heading, text))

    for line in body.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            content.append(line)
            continue
        level, value = len(match.group(1)), match.group(2).strip()
        if level == 1:
            title = value
            continue
        flush()
        content = []
        heading = value
    flush()
    if not sections and body.strip():
        sections.append((title or "概要", body.strip()))
    result: list[tuple[str, str]] = []
    for section_heading, section_text in sections:
        for part in _split_long_text(section_text, max_chars, overlap_chars):
            result.append((section_heading, part))
    return result


class SentenceTransformerEncoder:
    def __init__(self, model_id: str = MODEL_ID, device: str = "cpu"):
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self.model = SentenceTransformer(model_id, device=device)
        dimension_method = getattr(
            self.model, "get_embedding_dimension", self.model.get_sentence_embedding_dimension
        )
        dimension = dimension_method()
        if dimension != 512:
            raise KnowledgeIndexError(f"EMBEDDING_DIMENSION_MISMATCH:{dimension}")
        self.dimension = dimension
        self.lock = threading.Lock()

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        with self.lock:
            result = self.model.encode(
                texts, normalize_embeddings=True, convert_to_numpy=True,
                show_progress_bar=False, batch_size=16,
            )
        return np.asarray(result, dtype=np.float32)

    def encode_query(self, query: str) -> np.ndarray:
        with self.lock:
            result = self.model.encode(
                [QUERY_INSTRUCTION + query], normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False,
            )
        return np.asarray(result[0], dtype=np.float32)


class KnowledgeIndex:
    def __init__(
        self,
        knowledge_dir: str | Path,
        index_dir: str | Path,
        *,
        model_id: str = MODEL_ID,
        max_chars: int = 700,
        overlap_chars: int = 80,
        min_score: float = 0.62,
        encoder: Any | None = None,
    ):
        self.knowledge_dir = Path(knowledge_dir).resolve()
        self.index_dir = Path(index_dir).resolve()
        self.model_id = model_id
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars
        self.min_score = min_score
        self._encoder = encoder
        self._vectors: np.ndarray | None = None
        self._chunks: list[KnowledgeChunk] = []
        self._meta: dict[str, Any] = {}
        self._lock = threading.RLock()

    @property
    def encoder(self) -> Any:
        if self._encoder is None:
            self._encoder = SentenceTransformerEncoder(self.model_id)
        return self._encoder

    def warmup(self) -> dict[str, int]:
        """在服务就绪前加载编码器，避免首个用户查询承担模型初始化耗时。"""
        started = time.perf_counter()
        vector = self.encoder.encode_query("采购知识检索")
        if vector.shape != (512,) or not np.isfinite(vector).all():
            raise KnowledgeIndexError("KNOWLEDGE_QUERY_VECTOR_INVALID")
        return {"encoder_warmup_ms": int((time.perf_counter() - started) * 1000)}

    def _manifest(self) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        path = self.knowledge_dir / "manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KnowledgeIndexError("KNOWLEDGE_MANIFEST_UNAVAILABLE") from exc
        documents = manifest.get("documents")
        if (
            manifest.get("total") != EXPECTED_DOCUMENTS
            or manifest.get("A") != EXPECTED_CLASSES["A"]
            or manifest.get("B") != EXPECTED_CLASSES["B"]
            or not isinstance(documents, list)
            or len(documents) != EXPECTED_DOCUMENTS
        ):
            raise KnowledgeIndexError("KNOWLEDGE_MANIFEST_COUNT_MISMATCH")
        fingerprint_rows: list[dict[str, str]] = []
        for item in documents:
            if item.get("class") not in EXPECTED_CLASSES or item.get("index_allowed") is not True:
                raise KnowledgeIndexError("KNOWLEDGE_DOCUMENT_NOT_APPROVED")
            relative = Path(str(item.get("path", "")))
            full = (self.knowledge_dir / relative).resolve()
            if self.knowledge_dir not in full.parents or not full.is_file():
                raise KnowledgeIndexError("KNOWLEDGE_DOCUMENT_PATH_INVALID")
            actual = _sha256(full)
            if actual != item.get("reviewed_sha256"):
                raise KnowledgeIndexError(f"KNOWLEDGE_DOCUMENT_HASH_MISMATCH:{item.get('id')}")
            fingerprint_rows.append({"id": str(item.get("id")), "sha256": actual})
        payload = {
            "model_id": self.model_id,
            "max_chars": self.max_chars,
            "overlap_chars": self.overlap_chars,
            "documents": fingerprint_rows,
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return manifest, documents, fingerprint

    def _load_chunks(self, documents: list[dict[str, Any]]) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        for item in documents:
            path = (self.knowledge_dir / item["path"]).resolve()
            metadata, body = _frontmatter(path.read_text(encoding="utf-8"))
            doc_id = str(item["id"])
            if metadata.get("review_class") != item["class"] or metadata.get("index_allowed") != "true":
                raise KnowledgeIndexError(f"KNOWLEDGE_REVIEW_METADATA_MISMATCH:{doc_id}")
            source = metadata.get("source_ref") or metadata.get("source_url") or "正式审校知识库"
            for number, (heading, text) in enumerate(
                _section_chunks(body, self.max_chars, self.overlap_chars), start=1
            ):
                chunks.append(KnowledgeChunk(
                    chunk_id=f"{doc_id}#{number:03d}", doc_id=doc_id,
                    title=str(item["title"]), heading=heading,
                    review_class=str(item["class"]), source=source,
                    source_url=str(item.get("source_url") or metadata.get("source_url") or ""),
                    text=text, document_path=str(item["path"]),
                    document_sha256=str(item["reviewed_sha256"]),
                ))
        if not chunks:
            raise KnowledgeIndexError("KNOWLEDGE_CHUNKS_EMPTY")
        return chunks

    def ensure_index(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            _, documents, fingerprint = self._manifest()
            vector_path = self.index_dir / "vectors.npy"
            chunks_path = self.index_dir / "chunks.json"
            meta_path = self.index_dir / "index_meta.json"
            if not force and vector_path.is_file() and chunks_path.is_file() and meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta.get("corpus_fingerprint") == fingerprint:
                        vectors = np.load(vector_path, allow_pickle=False)
                        raw_chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
                        chunks = [KnowledgeChunk(**item) for item in raw_chunks]
                        self._validate_index(vectors, chunks)
                        self._vectors, self._chunks, self._meta = vectors, chunks, meta
                        return {**meta, "rebuilt": False}
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass

            chunks = self._load_chunks(documents)
            vectors = self.encoder.encode_documents([item.embedding_text() for item in chunks])
            self._validate_index(vectors, chunks)
            self.index_dir.mkdir(parents=True, exist_ok=True)
            with (self.index_dir / "vectors.npy.tmp").open("wb") as handle:
                np.save(handle, vectors, allow_pickle=False)
            (self.index_dir / "vectors.npy.tmp").replace(vector_path)
            raw_chunks = [item.__dict__ for item in chunks]
            temp_chunks = self.index_dir / "chunks.json.tmp"
            temp_chunks.write_text(json.dumps(raw_chunks, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_chunks.replace(chunks_path)
            meta = {
                "format_version": 1,
                "model_id": self.model_id,
                "embedding_dimension": int(vectors.shape[1]),
                "document_count": EXPECTED_DOCUMENTS,
                "class_counts": EXPECTED_CLASSES,
                "chunk_count": len(chunks),
                "max_chars": self.max_chars,
                "overlap_chars": self.overlap_chars,
                "corpus_fingerprint": fingerprint,
            }
            temp_meta = self.index_dir / "index_meta.json.tmp"
            temp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_meta.replace(meta_path)
            self._vectors, self._chunks, self._meta = vectors, chunks, meta
            return {**meta, "rebuilt": True}

    @staticmethod
    def _validate_index(vectors: np.ndarray, chunks: list[KnowledgeChunk]) -> None:
        if vectors.ndim != 2 or vectors.shape != (len(chunks), 512):
            raise KnowledgeIndexError("KNOWLEDGE_INDEX_SHAPE_INVALID")
        if not np.isfinite(vectors).all():
            raise KnowledgeIndexError("KNOWLEDGE_INDEX_NON_FINITE")
        norms = np.linalg.norm(vectors, axis=1)
        if not np.allclose(norms, 1.0, atol=1e-4):
            raise KnowledgeIndexError("KNOWLEDGE_INDEX_NOT_NORMALIZED")

    @staticmethod
    def _core_rule_sections(query: str) -> set[tuple[str, str]]:
        """只定位已确认核心制度的原文节；金额、权限和扣减结论仍以审校文档为准。"""
        sections: set[tuple[str, str]] = set()
        approval = (
            re.search(r"审批|谁.*批|找.*批", query)
            and re.search(r"金额|一万|万元|万块|门槛|分界|谁", query)
        ) or (
            re.search(r"一万|万元|10000", query)
            and re.search(r"审批|门槛|要求|流程", query)
        )
        if approval:
            sections.update({("KB-003", "审批金额分界"), ("KB-003", "审批权限与动作")})
        if re.search(
            r"(?:报价|凭证|附件).*(?:核验|审核|真假|真实性|OCR)"
            r"|报价材料|报价附件|可信报价凭证|(?:高金额|大额|万元).*报价"
            r"|报价单.*(?:审批|直接|通过)", query, re.I,
        ):
            # “高金额”是凭证条款的前提；一并提供金额分界，避免有材料规则却无法判断适用范围。
            sections.update({("KB-003", "审批金额分界"), ("KB-003", "报价凭证的含义")})
        if "预算" in query and re.search(r"扣|减|钱|重复|失败", query):
            sections.add(("KB-004", "预算在什么阶段扣减"))
        return sections

    def search(self, query: str, top_k: int = 4) -> dict[str, Any]:
        clean = str(query or "").strip()
        if not clean:
            raise ValueError("KNOWLEDGE_QUERY_REQUIRED")
        if type(top_k) is not int or not 1 <= top_k <= 8:
            raise ValueError("KNOWLEDGE_TOP_K_INVALID")
        with self._lock:
            total_started = time.perf_counter()
            index_started = time.perf_counter()
            meta = self.ensure_index()
            index_ms = int((time.perf_counter() - index_started) * 1000)
            encoder_started = time.perf_counter()
            encoder = self.encoder
            encoder_init_ms = int((time.perf_counter() - encoder_started) * 1000)
            query_started = time.perf_counter()
            query_vector = encoder.encode_query(clean)
            query_encode_ms = int((time.perf_counter() - query_started) * 1000)
            if query_vector.shape != (512,) or not np.isfinite(query_vector).all():
                raise KnowledgeIndexError("KNOWLEDGE_QUERY_VECTOR_INVALID")
            similarity_started = time.perf_counter()
            scores = self._vectors @ query_vector  # type: ignore[operator]
            order = np.argsort(-scores, kind="stable")[:top_k].tolist()
            sections = self._core_rule_sections(clean)
            core_indices = [
                i for i, chunk in enumerate(self._chunks)
                if chunk.review_class == "A" and (chunk.doc_id, chunk.heading) in sections
            ][:8]
            # 口语前缀和复合问题会挤掉原始条款；保留向量候选并补证，不重写问题或规则事实。
            order = list(dict.fromkeys([*order, *core_indices]))
            if len(order) > 8:
                remaining = [i for i in order if i not in core_indices][:8 - len(core_indices)]
                order = [i for i in order if i in core_indices or i in remaining]
            hits = [self._chunks[int(i)].public(float(scores[int(i)])) for i in order]
            similarity_ms = int((time.perf_counter() - similarity_started) * 1000)
            best = float(scores[int(order[0])]) if len(order) else -1.0
            core_complete = bool(sections) and sections <= {
                (self._chunks[i].doc_id, self._chunks[i].heading) for i in core_indices
            }
            return {
                "query": clean,
                "model_id": self.model_id,
                "index_fingerprint": meta["corpus_fingerprint"],
                "evidence_sufficient": core_complete if sections else best >= self.min_score,
                "min_score": self.min_score,
                "core_rule_chunk_ids": [self._chunks[i].chunk_id for i in core_indices],
                "hits": hits,
                "timings_ms": {
                    "index_check": index_ms,
                    "encoder_init": encoder_init_ms,
                    "query_encode": query_encode_ms,
                    "similarity": similarity_ms,
                    "total": int((time.perf_counter() - total_started) * 1000),
                },
            }


def default_knowledge_dir() -> Path:
    configured = os.getenv("AGENT_KNOWLEDGE_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "采购Agent_正式知识库_v1"


def default_index_dir() -> Path:
    configured = os.getenv("AGENT_KNOWLEDGE_INDEX_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent / "runtime_data" / "knowledge_index"


def build_default_index(*, force: bool = False) -> tuple[KnowledgeIndex, dict[str, Any]]:
    index = KnowledgeIndex(
        default_knowledge_dir(), default_index_dir(),
        model_id=os.getenv("AGENT_EMBEDDING_MODEL", MODEL_ID),
        max_chars=int(os.getenv("AGENT_RAG_CHUNK_CHARS", "700")),
        overlap_chars=int(os.getenv("AGENT_RAG_CHUNK_OVERLAP", "80")),
        min_score=float(os.getenv("AGENT_RAG_MIN_SCORE", "0.62")),
    )
    return index, index.ensure_index(force=force)
