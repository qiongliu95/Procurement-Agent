"""一次性修复旧路由；默认只预览，不执行审批，也不改预算或既有批准记录。"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import uuid


def repair_routes(db, *, apply=False):
    db.row_factory = sqlite3.Row
    db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
    result = {'repaired': [], 'eligible': [], 'skipped': []}
    try:
        rows = db.execute("SELECT * FROM requests WHERE state='PENDING_HIGH_APPROVAL' AND amount>=1000000").fetchall()
        for row in rows:
            rid = row['id']
            # 任何版本的人审记录、结果标记或PO/幂等痕迹都必须人工核实，不抹除历史。
            unsafe = (row['approved_by'] is not None or row['approved_version'] is not None
                      or db.execute('SELECT 1 FROM approvals WHERE request_id=?', (rid,)).fetchone()
                      or db.execute('SELECT 1 FROM pos WHERE request_id=?', (rid,)).fetchone()
                      or db.execute('SELECT 1 FROM idem WHERE request_id=?', (rid,)).fetchone()
                      or db.execute("SELECT 1 FROM events WHERE request_id=? AND type IN ('HUMAN_APPROVED','PO_COMMITTED','PO_CREATED','BUDGET_DEBITED')", (rid,)).fetchone())
            if unsafe:
                result['skipped'].append(rid)
                continue
            result['eligible'].append(rid)
            if not apply:
                continue
            db.execute("UPDATE requests SET state='SUBMITTED' WHERE id=? AND version=? AND state='PENDING_HIGH_APPROVAL'", (rid, row['version']))
            db.execute('''INSERT INTO events(type,request_id,version,actor_id,actor_role,detail,
                          run_id,tool_call_id,occurred_at,evidence_source) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                       ('APPROVAL_ROUTE_REPAIRED', rid, row['version'], 'SYSTEM', 'maintenance',
                        json.dumps({'from_state': 'PENDING_HIGH_APPROVAL', 'to_state': 'SUBMITTED',
                                    'assigned_approver': 'M01', 'reason': 'sequential_approval_rule_20261016'}),
                        'approval-repair-' + uuid.uuid4().hex, 'maintenance-' + uuid.uuid4().hex,
                        datetime.now(timezone.utc).isoformat(), 'backend_maintenance'))
            result['repaired'].append(rid)
        if apply:
            db.commit()
        else:
            db.rollback()
        return result
    except Exception:
        db.rollback()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--apply', action='store_true', help='须先停止旧Backend；创建可恢复备份后执行')
    args = parser.parse_args()
    path = Path(args.db).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True)
    try:
        if args.apply:
            # 调用方须在维护窗口运行，避免旧进程继续按错误规则接单。
            backup = path.parent / 'runtime_data' / 'backups' / (
                path.stem + '.pre_two_stage_approval_' + datetime.now().strftime('%Y%m%d-%H%M%S')
                + '-' + uuid.uuid4().hex[:8] + '.sqlite')
            backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup) as target:
                db.backup(target)
            print(json.dumps({'backup': str(backup)}))
        print(json.dumps(repair_routes(db, apply=args.apply), ensure_ascii=True))
    finally:
        db.close()


if __name__ == '__main__':
    main()
