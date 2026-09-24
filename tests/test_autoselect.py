from pathlib import Path

from almanac.autoselect import AutoSelector, Footprints, Reservations
from almanac.gpu import MB, Gpu

GB = 1024 * MB
LADDER = {"auto_models": ["big:9b", "mid:4b", "small:2b"]}
INSTALLED = {"big:9b": int(6.0 * GB), "mid:4b": int(3.0 * GB), "small:2b": int(2.0 * GB)}


def card(free_mb: int, total_mb: int = 8192) -> Gpu:
    return Gpu("Test GPU", "nvidia", "GPU-t", total_mb, total_mb - free_mb, free_mb, "nvidia-smi")


def selector(free_mb: int | None, tmp_path: Path | None = None, **settings) -> AutoSelector:
    return AutoSelector({**LADDER, **settings}, lambda: None if free_mb is None else card(free_mb), Footprints(tmp_path))


def test_the_largest_that_fits_wins() -> None:
    # 9b estimated at 6 GB * 1.2 = 7373 MB; 8 GB card, 819 MB headroom.
    assert selector(8192).choose(INSTALLED, {}).model == "big:9b"
    choice = selector(5818).choose(INSTALLED, {})  # a game holds 2.4 GB
    assert choice.model == "mid:4b" and choice.budget_mb == 5818 - 819 and choice.need_mb == 3687
    assert selector(3000).choose(INSTALLED, {}).model == "small:2b"


def test_our_resident_models_count_as_free() -> None:
    # 1 GB free, but the resident 9b holds 6.7 GB that switching gives back.
    assert selector(1024).choose(INSTALLED, {"big:9b": int(6.7 * GB)}).model == "big:9b"
    # Someone else's model does not count.
    assert selector(1024).choose(INSTALLED, {"other:7b": int(6.7 * GB)}).model == "small:2b"


def test_headroom_default_and_explicit() -> None:
    assert selector(0).headroom_mb(8192) == 819
    assert selector(0).headroom_mb(2048) == 512
    assert selector(0, auto_headroom_mb=2000).headroom_mb(8192) == 2000
    assert selector(0, auto_headroom_mb="").headroom_mb(24576) == 2457


def test_measured_footprint_is_remembered_and_preferred(tmp_path: Path) -> None:
    s = selector(8192, tmp_path)
    s.choose(INSTALLED, {"big:9b": 6886 * MB})
    assert s.need_mb("big:9b", INSTALLED["big:9b"]) == 6886
    again = selector(8192, tmp_path)  # a restart reads it back
    assert again.footprints.mb == {"big:9b": 6886}
    # Measured 6886 MB fits the 8192 - 819 = 7373 MB budget.
    assert again.choose(INSTALLED, {}).model == "big:9b"


def test_unreadable_gpu_uses_the_fallback() -> None:
    choice = selector(None).choose(INSTALLED, {})
    assert choice.model == "small:2b" and choice.budget_mb is None and "cannot read" in choice.reason
    assert selector(None, auto_fallback="mid:4b").choose(INSTALLED, {}).model == "mid:4b"


def test_nothing_fits_runs_the_smallest_partly_on_cpu() -> None:
    choice = selector(1024).choose(INSTALLED, {})
    assert choice.model == "small:2b" and "CPU" in choice.reason


def test_only_installed_models_are_candidates() -> None:
    assert selector(8192).choose({"mid:4b": int(3 * GB)}, {}).model == "mid:4b"
    # Nothing installed (or /api/tags failed): the ladder as configured; unknown size never counts as fitting.
    assert selector(8192).choose({}, {}).model == "small:2b"


def test_reservations_cap_the_budget_and_lapse(tmp_path: Path) -> None:
    now = [1000.0]
    res = Reservations(tmp_path, clock=lambda: now[0])
    s = AutoSelector(LADDER, lambda: card(8192), Footprints(), res)
    assert s.choose(INSTALLED, {}).model == "big:9b"  # a quiet card
    res.hold("ffxiv", 3500, ttl_s=60)  # a game that has not allocated yet
    choice = s.choose(INSTALLED, {})
    assert choice.model == "mid:4b" and choice.budget_mb == 8192 - 3500 - 819
    assert Reservations(tmp_path, clock=lambda: now[0]).current() == {"ffxiv": 3500}  # kept across a restart
    now[0] += 61  # the holder stopped refreshing
    assert res.current() == {} and s.choose(INSTALLED, {}).model == "big:9b"


def test_reservation_release_and_no_ttl(tmp_path: Path) -> None:
    res = Reservations(tmp_path)
    res.hold("a", 1000)
    res.hold("b", 500)
    assert res.total_mb() == 1500
    assert res.release("a") and not res.release("a")
    assert res.current() == {"b": 500}
