import pytest
from quantdev.db import database
from quantdev.services.market import market_data_service


@pytest.fixture(autouse=True)
def isolated_database(tmp_path):
    original_path = database.path
    database.path = tmp_path / "quantdev-test.db"
    database.migrate()
    market_data_service.bootstrap_demo_data(force=True)
    yield
    database.path = original_path

