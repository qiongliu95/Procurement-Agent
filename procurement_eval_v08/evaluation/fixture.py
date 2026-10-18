"""为单条 Case 建立隔离 SQLite 与 loopback 采购模拟器。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend import AUTH, App, BusinessError, Store

from .case_loader import LoadedCase


SUPPORTED_CASES = {
    "E01", "E02A", "E02B", "A02_BC03", "C04_BC06", "E03", "A06_R04", "A07",
    "N-01", "N-02", "N-03", "N-04", "N-05", "N-07", "E-01", "E-02", "E-03",
    "E-04", "E-05", "E-06", "E-07", "E-08", "E-11", "E-12", "B-04", "B-05",
    "B-06", "B-08", "P-01", "P-02", "P-06", "P-07", "P-08",
}
SNAPSHOT_TABLES = (
    "budgets",
    "suppliers",
    "requests",
    "approvals",
    "vendor_reviews",
    "pos",
    "idem",
    "events",
)


class FixtureNotReady(RuntimeError):
    """当前 Fixture Builder 尚不能可靠准备该环境。"""


class ReadFailureStore(Store):
    """E-08 只注入指定查询故障，其余真实业务行为保持不变。"""

    def get_status(self, rid, auth):
        if rid == "TEST-QUERY-01":
            raise BusinessError(503, "UPSTREAM_UNAVAILABLE")
        return super().get_status(rid, auth)


def _json_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    return value


def snapshot_database(db_path: str | Path) -> dict[str, Any]:
    """以确定顺序读取 Checker 所需业务表，不依赖 Agent 自述。"""
    path = Path(db_path)
    tables: dict[str, list[dict[str, Any]]] = {}
    # sqlite3.Connection 的 with 只管理事务，不会关闭句柄；Windows 下必须显式关闭。
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        for table in SNAPSHOT_TABLES:
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            order = columns[0] if columns else "rowid"
            rows = []
            for row in db.execute(f"SELECT * FROM {table} ORDER BY {order}"):
                item = {key: _json_cell(row[key]) for key in row.keys()}
                if table == "events" and isinstance(item.get("detail"), str):
                    try:
                        item["detail"] = json.loads(item["detail"])
                    except json.JSONDecodeError:
                        pass
                rows.append(item)
            tables[table] = rows
    return {
        "database_path": str(path.resolve()),
        "tables": tables,
        "counts": {name: len(rows) for name, rows in tables.items()},
    }


def database_sha256(db_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(db_path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class FixtureEnvironment:
    loaded_case: LoadedCase
    db_path: Path
    store: Store | None = None
    app: App | None = None
    thread: threading.Thread | None = None
    initial_state: dict[str, Any] | None = None

    def prepare(self) -> "FixtureEnvironment":
        case_id = self.loaded_case.case.case_id
        if case_id not in SUPPORTED_CASES:
            raise FixtureNotReady(f"本轮尚未实现 {case_id} 的 Fixture")
        requirements = self.loaded_case.fixture.requirements
        if requirements.get("db_isolation") != "one_case_one_snapshot":
            raise FixtureNotReady(f"{case_id} 未声明 one_case_one_snapshot")
        actor = requirements.get("agent_actor", "U01")
        if actor != "U01":
            raise FixtureNotReady(f"当前 Agent Runtime 仅支持固定身份 U01，Fixture 要求 {actor}")
        if self.db_path.exists():
            raise FixtureNotReady(f"隔离数据库已存在，拒绝覆盖：{self.db_path}")

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        store_type = ReadFailureStore if case_id == "E-08" else Store
        self.store = store_type(self.db_path)
        self._seed_case()
        self._validate_seed_state()
        self.initial_state = snapshot_database(self.db_path)
        self.initial_state["sha256"] = database_sha256(self.db_path)
        self.app = App(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(
            target=self.app.serve_forever,
            name=f"eval-backend-{case_id}",
            daemon=True,
        )
        self.thread.start()
        return self

    @property
    def base_url(self) -> str:
        if self.app is None:
            raise FixtureNotReady("Fixture 尚未启动")
        return f"http://127.0.0.1:{self.app.server_port}"

    def _validate_seed_state(self) -> None:
        assert self.store is not None
        with self.store.connect() as db:
            suppliers = {
                row["name"]: row["status"]
                for row in db.execute("SELECT name,status FROM suppliers")
            }
            budgets = int(db.execute("SELECT COUNT(*) FROM budgets").fetchone()[0])
        if suppliers.get("启明办公") != "active":
            raise FixtureNotReady("Fixture 缺少 active 的启明办公供应商")
        if budgets == 0:
            raise FixtureNotReady("Fixture 缺少预算主数据")

    def _create(self, rid: str, *, quantity: int = 3, price: str = "50.00", supplier: str = "启明办公") -> None:
        assert self.store is not None
        self.store.create({
            "request_id": rid, "item": "办公用品", "quantity": quantity,
            "unit_price": price, "supplier_name": supplier, "purpose": "业务评估",
        }, AUTH["demo-u01"])

    def _seed_submitted(self, rid: str, *, quantity: int = 3, price: str = "50.00", budget: str | None = None) -> None:
        assert self.store is not None
        self._create(rid, quantity=quantity, price=price)
        if budget:
            self.store.eval_seed_budget(rid, budget, AUTH["demo-eval"])
        self.store.submit(rid, AUTH["demo-u01"])

    def _seed_case(self) -> None:
        """仅准备正式 Fixture 明确要求的业务状态。"""
        assert self.store is not None
        cid = self.loaded_case.case.case_id
        if cid in {"N-02", "N-07", "E-08"}:
            self._seed_submitted("TEST-QUERY-01")
        elif cid == "N-03":
            self._create("TEST-EDIT-01", quantity=2, price="50.00")
        elif cid == "N-05":
            with self.store.connect() as db:
                db.execute("UPDATE suppliers SET status='active' WHERE id='S_NEW'")
        elif cid == "E-04":
            with self.store.connect() as db:
                db.execute("INSERT INTO suppliers(id,name,status) VALUES (?,?,?)", ("S_ACTIVE_2", "启明科技", "active"))
        elif cid == "E-06":
            with self.store.connect() as db:
                db.execute("UPDATE budgets SET balance=485000 WHERE id='B_MKT'")
        elif cid == "E-11":
            self._create("TEST-VENDOR", supplier="新锐科技")
            self.store.submit("TEST-VENDOR", AUTH["demo-u01"])
        elif cid == "B-08":
            self._seed_submitted("TEST-PO-DONE")
            self.store.approve("TEST-PO-DONE", AUTH["demo-m01"], {})
            self.store.po("TEST-PO-DONE", AUTH["demo-u01"], "idem-TEST-PO-DONE")
        elif cid in {"A07", "P-01"}:
            self._seed_submitted("AGT-TEST-M01" if cid == "A07" else "TEST-M01")
        elif cid == "P-02":
            self._seed_submitted("TEST-HIGH", quantity=1, price="12000.00", budget="B_HIGH")
        elif cid == "P-07":
            self._create("TEST-OTHER-USER")
            with self.store.connect() as db:
                db.execute("UPDATE requests SET applicant='U02' WHERE id='TEST-OTHER-USER'")

    def capture_final_state(self) -> dict[str, Any]:
        final = snapshot_database(self.db_path)
        final["sha256"] = database_sha256(self.db_path)
        return final

    def close(self) -> None:
        if self.app is not None:
            self.app.shutdown()
            self.app.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def __enter__(self) -> "FixtureEnvironment":
        return self.prepare()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
