"""GPU environment forwarding: what survives the sudo hop into Eden.

sudo's env_reset wipes the container environment, so a renderer setting the
operator put in docker-compose only reaches Eden if the broker names it on the
`env` command line. Everything unnamed is silently dropped, which reads from
the outside like the container ignoring its own configuration.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "root" / "root"))
import broker  # noqa: E402


def test_forwards_vendor_graphics_variables(monkeypatch):
    monkeypatch.setenv("VK_DRIVER_FILES", "/usr/share/vulkan/icd.d/nvidia_icd.json")
    monkeypatch.setenv("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
    monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", "all")
    monkeypatch.setenv("MESA_VK_DEVICE_SELECT", "10de:0000")
    env = broker._gpu_env()
    assert env["VK_DRIVER_FILES"] == "/usr/share/vulkan/icd.d/nvidia_icd.json"
    assert env["__GLX_VENDOR_LIBRARY_NAME"] == "nvidia"
    assert env["NVIDIA_DRIVER_CAPABILITIES"] == "all"
    assert env["MESA_VK_DEVICE_SELECT"] == "10de:0000"


def test_forwards_the_base_image_render_node_selector(monkeypatch):
    """DRINODE has no DRI_ prefix, so it needs naming explicitly."""
    monkeypatch.setenv("DRINODE", "/dev/dri/renderD130")
    assert broker._gpu_env()["DRINODE"] == "/dev/dri/renderD130"


def test_ignores_unrelated_variables(monkeypatch):
    monkeypatch.setenv("BROKER_SECRET", "hunter2")
    env = broker._gpu_env()
    assert "BROKER_SECRET" not in env
    assert "PATH" not in env


def test_unset_render_nodes_are_not_forwarded_as_empty(monkeypatch):
    """Regression: DRI_NODE and DRINODE were forwarded unconditionally with an
    "" default, so every launch carried `DRI_NODE= DRINODE=` even when the
    operator had never set them. `env VAR=` is set-but-empty, not unset, and
    consumers disagree on what that means."""
    monkeypatch.setenv("DRI_NODE", "")
    monkeypatch.setenv("DRINODE", "")
    env = broker._gpu_env()
    assert "DRI_NODE" not in env
    assert "DRINODE" not in env
    assert "DRI_NODE" not in broker.ENV
    assert "DRINODE" not in broker.ENV


def test_computed_display_wins_over_an_inherited_one():
    """DISPLAY is probed from live sockets because Xwayland can land on :1;
    a stale inherited value must not shadow the detected one."""
    assert broker.ENV["DISPLAY"] == broker._detect_display()


def test_fake_libudev_stays_out_of_ld_preload():
    """libudev.so.1.0.0-fake intercepts Mesa/DRI udev calls and black-screens
    Eden. It is excluded on purpose, and the GPU passthrough must not be a back
    door that reintroduces it."""
    assert "libudev" not in broker.ENV["LD_PRELOAD"]
