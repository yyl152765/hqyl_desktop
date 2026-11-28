"""ERP connection resources stay private and explicit overrides fail closed."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from backend.services import erp_sku_earliest_store_repository as repository
from scripts.builtin_ads_database import release_erp_sku_connection_data


FIXTURE = {"host": "database.invalid", "database": "fixture", "username": "reader",
           "password": "fixture-secret", "port": 5432, "sslmode": "disable"}


def test_private_resource_retains_explicit_connection_policy_and_hides_password(tmp_path):
    resource = tmp_path / "erp_sku_database.json"
    resource.write_text(json.dumps(FIXTURE), encoding="utf-8")
    settings = repository.builtin_database_settings(resource)
    assert settings.host == FIXTURE["host"] and settings.password == FIXTURE["password"]
    assert settings.sslmode == "disable" and settings.port == 5432
    assert FIXTURE["password"] not in repr(settings)
    with patch.object(repository, "builtin_database_settings", return_value=settings):
        assert repository.DatabaseSettings.from_mapping({}) == settings
        with pytest.raises(repository.SkuQueryError, match="ERP_SKU_DB_DATABASE"):
            repository.DatabaseSettings.from_mapping({"ERP_SKU_DB_HOST": "override.invalid"})


@pytest.mark.parametrize("value", [[], {}, {**FIXTURE, "password": ""}, {**FIXTURE, "port": True},
                                  {**FIXTURE, "sslmode": "prefer"}, {**FIXTURE, "extra": "fixture-secret"}])
def test_invalid_private_resource_fails_without_echoing_contents(tmp_path, value):
    resource = tmp_path / "erp_sku_database.json"
    resource.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(repository.SkuQueryError) as error:
        repository.builtin_database_settings(resource)
    assert "fixture-secret" not in str(error.value)


def test_missing_resource_does_not_fall_back_to_embedded_credentials(tmp_path):
    assert repository.builtin_database_settings(tmp_path / "missing.json") is None
    with patch.object(repository, "builtin_database_settings", return_value=None):
        with pytest.raises(repository.SkuQueryError, match="未配置 ERP"):
            repository.DatabaseSettings.from_mapping({})


def test_packaging_accepts_only_external_untracked_erp_resource():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        export = root / "export"
        export.mkdir()
        private = root / "private"
        private.mkdir()
        resource = private / "erp_sku_database.json"
        resource.write_text(json.dumps(FIXTURE), encoding="utf-8")
        assert release_erp_sku_connection_data("", export) == []
        assert release_erp_sku_connection_data(str(resource), export) == [(str(resource), "backend/resources")]
        with pytest.raises(ValueError, match="outside"):
            release_erp_sku_connection_data(str(resource), private)
        with pytest.raises(ValueError, match="absolute"):
            release_erp_sku_connection_data(resource.name, export)
        subprocess.run(["git", "init", "--quiet", str(private)], check=True)
        subprocess.run(["git", "-C", str(private), "add", "--", resource.name], check=True)
        with pytest.raises(ValueError, match="tracked"):
            release_erp_sku_connection_data(str(resource), export)
