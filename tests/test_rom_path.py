"""ROM path handling: the boundary between RomM's input and Eden's argv.

Exercises path validation plus the folder-to-file resolution /launch depends on.
"""

import time

import broker
from conftest import request, rom


# ── Path validation ───────────────────────────────────────────────────────────


def test_validate_accepts_path_inside_root(rom_root):
    target = rom_root / "switch" / "game.nsp"
    target.write_bytes(b"cartridge")
    assert broker._validate_rom_path(str(target)) == target


def test_validate_rejects_traversal_outside_root(rom_root):
    raw = str(rom_root / "switch" / ".." / ".." / "etc" / "passwd")
    assert broker._validate_rom_path(raw) is None


def test_validate_rejects_absolute_path_outside_root(rom_root):
    assert broker._validate_rom_path("/etc/passwd") is None


def test_validate_rejects_symlink_escaping_root(rom_root, tmp_path):
    outside = tmp_path / "outside.nsp"
    outside.write_bytes(b"cartridge")
    link = rom_root / "escape.nsp"
    link.symlink_to(outside)
    assert broker._validate_rom_path(str(link)) is None


# ── Folder-organized ROMs ─────────────────────────────────────────────────────
#
# RomM addresses a folder-organized game by its folder: `Rom.full_path` is
# `fs_path/fs_name`, and for a multi-file ROM `fs_name` is the directory rather
# than the title file inside it, so /launch receives a path Eden cannot boot.


def test_resolve_rom_file_passes_a_plain_file_through(rom_root):
    nsp = rom(rom_root, "switch/game.nsp")
    assert broker._resolve_rom_file(nsp) == nsp


def test_resolve_rom_file_finds_the_title_inside_a_game_folder(rom_root):
    nsp = rom(rom_root, "switch/Metroid Dread/Metroid Dread.nsp")
    assert broker._resolve_rom_file(rom_root / "switch" / "Metroid Dread") == nsp


def test_resolve_rom_file_returns_none_for_a_folder_with_no_title(rom_root):
    rom(rom_root, "switch/Metroid Dread/cover.png")
    rom(rom_root, "switch/Metroid Dread/notes.txt")
    assert broker._resolve_rom_file(rom_root / "switch" / "Metroid Dread") is None


def test_resolve_rom_file_prefers_a_cartridge_dump_over_an_eshop_package(rom_root):
    xci = rom(rom_root, "switch/Game/Game.xci")
    rom(rom_root, "switch/Game/Game.nsp")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") == xci


def test_resolve_rom_file_prefers_a_title_over_a_homebrew_executable(rom_root):
    nsp = rom(rom_root, "switch/Game/Game.nsp")
    rom(rom_root, "switch/Game/hbmenu.nro")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") == nsp


def test_resolve_rom_file_prefers_format_over_depth_across_levels(rom_root):
    """Format rank outranks depth: a nested cartridge dump beats a top-level
    homebrew stub, which a level-at-a-time search would have picked first."""
    xci = rom(rom_root, "switch/Game/Base/Game.xci")
    rom(rom_root, "switch/Game/hbmenu.nro")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") == xci


def test_resolve_rom_file_looks_one_level_into_subfolders(rom_root):
    nsp = rom(rom_root, "switch/Game/Base/Game.nsp")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") == nsp


def test_resolve_rom_file_ranks_a_nested_title_above_a_top_level_homebrew(rom_root):
    """Format preference has to outrank shallowness. A homebrew .nro loose in
    the game folder must not win just for sitting a level above the real title,
    which folder-organized sets keep in a Base/ subfolder."""
    xci = rom(rom_root, "switch/Game/Base/Game.xci")
    rom(rom_root, "switch/Game/hbmenu.nro")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") == xci


def test_resolve_rom_file_prefers_the_top_level_title_over_a_nested_one(rom_root):
    """Depth breaks a tie between equal formats — the base game sits at the top
    level and its update sits in a subfolder."""
    top = rom(rom_root, "switch/Game/Game.nsp")
    rom(rom_root, "switch/Game/updates/Game (v1.1).nsp")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") == top


def test_resolve_rom_file_is_deterministic_among_equal_candidates(rom_root):
    """Same format, same depth — name order decides, so repeated launches of the
    same folder always boot the same file."""
    rom(rom_root, "switch/Game/b.nsp")
    rom(rom_root, "switch/Game/a.nsp")
    picked = broker._resolve_rom_file(rom_root / "switch" / "Game")
    assert picked.name == "a.nsp"


def test_resolve_rom_file_does_not_descend_past_the_second_level(rom_root):
    rom(rom_root, "switch/Game/a/b/deep.nsp")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") is None


def test_resolve_rom_file_ignores_hidden_files(rom_root):
    rom(rom_root, "switch/Game/._Game.nsp")
    assert broker._resolve_rom_file(rom_root / "switch" / "Game") is None


def test_resolve_rom_file_refuses_a_symlink_escaping_rom_root(rom_root, tmp_path):
    outside = tmp_path / "outside.nsp"
    outside.write_bytes(b"cartridge")
    folder = rom_root / "switch" / "Game"
    folder.mkdir(parents=True)
    (folder / "link.nsp").symlink_to(outside)
    assert broker._resolve_rom_file(folder) is None


def test_resolve_rom_file_returns_none_for_a_missing_path(rom_root):
    assert broker._resolve_rom_file(rom_root / "switch" / "nope") is None


# ── The /launch contract ──────────────────────────────────────────────────────


def _wait(launched, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not launched:
        time.sleep(0.02)
    assert launched, "launch thread never ran"
    return launched


def test_launch_boots_the_title_inside_a_game_folder(client, rom_root):
    base, launched = client
    nsp = rom(rom_root, "switch/Metroid Dread/Metroid Dread.nsp")
    code, body, _ = request(
        base, "/launch", "POST", {"rom_path": str(rom_root / "switch" / "Metroid Dread")}
    )
    assert code == 200
    assert body["rom_path"] == str(nsp)
    assert _wait(launched)[-1] == str(nsp)


def test_launch_reports_a_folder_with_no_title_distinctly(client, rom_root):
    base, launched = client
    rom(rom_root, "switch/Metroid Dread/cover.png")
    code, body, _ = request(
        base, "/launch", "POST", {"rom_path": str(rom_root / "switch" / "Metroid Dread")}
    )
    assert code == 422
    assert "no bootable ROM file" in body["error"]
    assert ".nsp" in body["extensions"]
    assert launched == []


def test_launch_still_reports_a_missing_path_as_missing(client, rom_root):
    base, _launched = client
    code, body, _ = request(
        base, "/launch", "POST", {"rom_path": str(rom_root / "switch" / "nope.nsp")}
    )
    assert code == 422
    assert body["error"] == "rom_path does not exist"
