import os
from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("端口", "8500"))
    # 显式开线程：并发双请求才能真正打到同一把事件锁/单飞合并
    app.run(host="0.0.0.0", port=port, threaded=True)
