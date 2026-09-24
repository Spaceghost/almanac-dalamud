import subprocess
from pathlib import Path
from types import SimpleNamespace

from almanac.gpu import Gpu, amd_gpus, inference_gpu, nvidia_gpus


def fake_run(stdout: str):
    def run(*args, **kwargs):
        return SimpleNamespace(stdout=stdout)
    return run


def test_nvidia_gpus_parse_and_skip_unreadable() -> None:
    out = "GPU-a, Quadro P4000, 8192, 2374, 5818\nGPU-b, Odd Card, [N/A], [N/A], [N/A]\nnot, a, row\n"
    assert nvidia_gpus(fake_run(out)) == [Gpu("Quadro P4000", "nvidia", "GPU-a", 8192, 2374, 5818, "nvidia-smi")]


def test_nvidia_gpus_absent() -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi")
    assert nvidia_gpus(missing) == []

    def failing(*args, **kwargs):
        raise subprocess.CalledProcessError(9, "nvidia-smi")
    assert nvidia_gpus(failing) == []


def test_amd_gpus_from_sysfs(tmp_path: Path) -> None:
    card = tmp_path / "card1" / "device"
    card.mkdir(parents=True)
    (card / "mem_info_vram_total").write_text(str(16 * 1024 * 1024 * 1024))
    (card / "mem_info_vram_used").write_text(str(4 * 1024 * 1024 * 1024))
    (card / "product_name").write_text("Radeon RX 7800 XT\n")
    (tmp_path / "card1-DP-1").mkdir()  # a connector, not a card
    (tmp_path / "card0" / "device").mkdir(parents=True)  # a card without VRAM counters (iGPU)
    gpus = amd_gpus(tmp_path)
    assert gpus == [Gpu("Radeon RX 7800 XT", "amd", "card1", 16384, 4096, 12288, "sysfs")]


def test_inference_gpu_by_uuid_or_largest() -> None:
    small = Gpu("RTX 3070", "nvidia", "GPU-3070", 8192, 3000, 5192, "nvidia-smi")
    big = Gpu("RTX 4060 Ti", "nvidia", "GPU-4060", 16384, 100, 16284, "nvidia-smi")
    assert inference_gpu([small, big]) == big
    assert inference_gpu([small, big], "GPU-3070") == small
    assert inference_gpu([small, big], "GPU-gone") is None
    assert inference_gpu([]) is None
