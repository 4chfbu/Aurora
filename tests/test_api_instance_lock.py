from pathlib import Path

import pytest

from aurora.services.api_instance_lock import acquire_api_instance_lock, api_instance_lock_path


def test_api_instance_lock_rejects_a_second_process_for_the_same_database(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'aurora.db'}"

    with acquire_api_instance_lock(database_url, lock_dir=tmp_path) as lock_path:
        assert lock_path == api_instance_lock_path(database_url, lock_dir=tmp_path)
        with pytest.raises(RuntimeError, match="another Aurora API instance"):
            with acquire_api_instance_lock(database_url, lock_dir=tmp_path):
                pass

    with acquire_api_instance_lock(database_url, lock_dir=tmp_path):
        pass
