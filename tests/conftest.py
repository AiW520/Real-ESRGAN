import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import CACHE

import pytest
from backend.app.jobs import JobStore
from backend.app.main import app
import backend.app.main as main_module
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    local = JobStore(tmp_path / 'api-data')
    monkeypatch.setattr(main_module, 'store', local)
    with TestClient(app) as instance:
        yield instance, local
