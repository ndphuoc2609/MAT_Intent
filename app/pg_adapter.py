"""PostgreSQL adapter — NOT_IMPLEMENTED.

Trạng thái thật: KHÔNG CÓ adapter PostgreSQL nào trong repo này. `app/state.py`
chỉ dùng SQLite (BEGIN IMMEDIATE + optimistic version). `compose.yaml` chỉ
provision container postgres:16; service `app` trong compose vẫn chạy server ở
chế độ SQLite (xem SANDBOX_DB_PATH) vì chưa có adapter để nối DATABASE_URL.

Đừng mô tả tình trạng này là "chỉ thiếu môi trường kiểm thử": chạy suite trên
PostgreSQL đang BLOCKED ở việc viết adapter (mapping transaction/lock/version
sang PG), không phải chỉ ở việc có Docker hay không. Mọi kết quả kèm theo repo
đều là SQLite cho tới khi adapter này được triển khai và suite chạy lại trên PG.
"""

class PostgresNotImplemented(NotImplementedError):
    """Ném ra khi có code nào đó cố dùng PostgreSQL trước khi adapter tồn tại."""


def connect(*args, **kwargs):
    raise PostgresNotImplemented(
        "PostgreSQL adapter is NOT_IMPLEMENTED: app/state.py is SQLite-only. "
        "Do not claim PostgreSQL semantics are verified."
    )
