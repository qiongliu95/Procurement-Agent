"""重建隔离的本地演示数据库；不会接受或删除任意数据库路径。"""
from pathlib import Path

from backend import Store


DEMO_DB = Path(__file__).resolve().with_name('demo_test.sqlite')


def reset_demo():
    # 固定文件名与目录，避免把“重置演示”误用于其他运行数据库。
    if DEMO_DB.name != 'demo_test.sqlite' or DEMO_DB.parent != Path(__file__).resolve().parent:
        raise RuntimeError('UNSAFE_DEMO_DATABASE_PATH')
    if DEMO_DB.exists():
        DEMO_DB.unlink()

    store = Store(DEMO_DB)
    requester = ('U01', 'requester')
    evaluator = ('EVAL', 'evaluator')
    fixtures = [
        ('DEMO-DRAFT', '键盘', 5, '120.00', '启明办公', '新员工办公使用', False),
        ('DEMO-M01', '显示器', 2, '800.00', '启明办公', '市场部办公使用', True),
        ('DEMO-HIGH', '工作站', 1, '12000.00', '启明办公', '设计工作使用', True),
        ('DEMO-NEW', '会议摄像头', 1, '1300.00', '新锐科技', '远程会议使用', True),
    ]
    for rid,item,quantity,price,supplier,purpose,should_submit in fixtures:
        store.create({'request_id':rid,'item':item,'quantity':quantity,'unit_price':price,
                      'supplier_name':supplier,'purpose':purpose},requester)
        if rid=='DEMO-HIGH':
            store.eval_seed_budget(rid,'B_HIGH',evaluator)
        if should_submit:
            store.submit(rid,requester)
    return DEMO_DB


if __name__ == '__main__':
    path=reset_demo()
    print(f'隔离演示数据已重建：{path}')
    print(f'启动命令：py -3.11 backend.py "{path}"')
