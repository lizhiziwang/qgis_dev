"""测试 FastAPI server（不依赖 QGIS 的部分）。"""

# 必须在最顶部插入 sys.path（早于所有 import）
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi", reason="需要 fastapi")
pytest.importorskip("httpx", reason="需要 httpx for TestClient")


def test_health_route():
    import sys
    import os
    print(f"[test] cwd={os.getcwd()} sys.path[:5]={sys.path[:5]}")
    print(f"[test] app in sys.modules: {'app' in sys.modules}")
    try:
        import app
        print(f"[test] app at: {app.__file__}")
    except Exception as e:
        print(f"[test] app import: {e}")
    try:
        import app.server
        print(f"[test] app.server at: {app.server.__file__}")
    except Exception as e:
        print(f"[test] app.server import: {e}")
    from fastapi.testclient import TestClient
    from app.server.app import create_app
    app = create_app()
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "qgis_dev"}
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "qgis_dev"}


def test_ready_route():
    from fastapi.testclient import TestClient
    from app.server.app import create_app

    app = create_app()
    client = TestClient(app)
    r = client.get("/ready")
    assert r.status_code == 200
    assert r.json() == {"ready": True}


def test_tiles_test_route():
    from fastapi.testclient import TestClient
    from app.server.app import create_app

    app = create_app()
    client = TestClient(app)
    r = client.get("/getmap/test")
    assert r.status_code == 200
    # 不验证具体 text（编码 / Client chunked），仅确认可达
    assert len(r.text) > 0


def test_tiles_missing_config_404():
    from fastapi.testclient import TestClient
    from app.server.app import create_app

    app = create_app()
    client = TestClient(app)
    r = client.get("/getmap/map/99999/0/0/0")
    assert r.status_code == 404


def test_check_types_route():
    pytest.importorskip("qgis", reason="CheckService 内部走 BaseCheck.run，需要 qgis.core")
    from fastapi.testclient import TestClient
    from app.server.app import create_app

    app = create_app()
    client = TestClient(app)
    r = client.get("/check/types")
    assert r.status_code == 200
    types = r.json()
    # 至少类型是 list
    assert isinstance(types, list)
