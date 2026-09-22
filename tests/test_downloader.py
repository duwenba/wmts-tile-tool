"""downloader：失败列表解析 / 鉴权检测（不联网）。"""

from wmts.core.downloader import AsyncTileDownloader, AuthError
from wmts.core.paths import tile_path


def test_load_failed_coords(config, tmp_path):
    config.failed_file = str(tmp_path / "failed.txt")
    config.failed_file and open(config.failed_file, "w", encoding="utf-8").write(
        "[53248,10558] HTTP 404\n"
        "坏行\n"
        "[53300,10600] timeout\n")
    dl = AsyncTileDownloader(config)
    assert dl.load_failed_coords() == [(53248, 10558), (53300, 10600)]


def test_load_failed_coords_missing_file(config, tmp_path):
    config.failed_file = str(tmp_path / "none.txt")
    assert AsyncTileDownloader(config).load_failed_coords() == []


def test_looks_like_auth_error(tmp_path):
    config_file = tmp_path / "a.png"
    config_file.write_bytes('{"error":"操作权限不足","status":405}'.encode())
    assert AsyncTileDownloader._looks_like_auth_error(config_file) is True

    png = tmp_path / "b.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n....")
    assert AsyncTileDownloader._looks_like_auth_error(png) is False


def test_auth_error_is_autherror():
    assert issubclass(AuthError, RuntimeError)


def test_counter_init(config):
    dl = AsyncTileDownloader(config)
    assert dl.success_count == 0 and dl.fail_count == 0
    assert dl.failed_coords == []
    assert dl.auth_error is None


def test_tile_path_used_for_saving(config, tmp_path):
    """保存路径应为 v2 分级结构。"""
    p = tile_path(config.tile_matrix, 11, 20, config.output_dir)
    assert p.startswith(config.output_dir)
    assert p.endswith(".png")


def test_run_tasks_processes_every_tile(config, monkeypatch):
    """回归：队列满超时时不得静默丢片（曾导致 done < total）。"""
    import asyncio

    dl = AsyncTileDownloader(config)
    dl.max_workers = 1  # 队列极小（2），强制触发"队列满等待"分支
    processed = []

    async def fake_download_tile(client, m, c, r, max_retries=3):
        await asyncio.sleep(0.005)
        processed.append((c, r))
        return "success"

    monkeypatch.setattr(dl, "download_tile", fake_download_tile)
    tasks = [(config.tile_matrix, c, r) for c in range(20) for r in range(10)]
    done = asyncio.run(dl.run_tasks(object(), iter(tasks), len(tasks), emit=False))
    assert done == len(tasks) == 200
    assert len(processed) == 200
    assert len(set(processed)) == 200  # 无重复、无遗漏
