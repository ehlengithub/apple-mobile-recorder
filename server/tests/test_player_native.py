"""The real Python transport talks to the firmware's C++ parser/store via pipes."""

import select
import shutil
import subprocess
from pathlib import Path

import pytest
from test_player import MAC, plan

from hidagent.player import Player, encode_recording

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", params=["usb-s3", "ble-esp32"])
def native(tmp_path_factory, request):
    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler needed for firmware integration")
    executable = tmp_path_factory.mktemp("native-player") / "wire"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-DAMR_HOST_BLE=" + str(int(request.param == "ble-esp32")),
            "-I" + str(ROOT / "hardware/player"),
            str(ROOT / "tests/player_wire_host.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable, request.param


class Pipe:
    def __init__(self, executable):
        self.executable = executable

    def open(self):
        self.process = subprocess.Popen([str(self.executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def write(self, data):
        self.process.stdin.write(data)

    def flush(self):
        self.process.stdin.flush()

    def read_until(self, *_):
        if not select.select([self.process.stdout], [], [], 2)[0]:
            raise RuntimeError("native firmware response timed out")
        return self.process.stdout.readline(1024)

    def close(self):
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.wait(timeout=3)


@pytest.mark.parametrize("count", [1, 256])
def test_real_python_to_cpp_upload_and_mode_switch(native, count):
    executable, controller = native
    pipe = Pipe(executable)
    player = Player("native-no-hardware", MAC, serial_factory=lambda **_: pipe, controller=controller)
    try:
        first = encode_recording(plan(steps=plan().steps * count))
        meta = player.upload(first)
        assert player.read_recording() == first
        assert player.control("arm", meta["crc"])["armed"]
        if controller == "ble-esp32":
            with pytest.raises(ValueError, match="维护"):
                player.upload(first)
            # Native simulation of the board's physical BOOT maintenance event.
            player.request("maintenance")
        assert player.control("desktop")["mode"] == "desktop"
        if controller == "ble-esp32":
            player.request("maintenance")
        second = encode_recording(plan(steps=plan().steps * count, loops=3, startup_ms=5000))
        assert second != first
        player.upload(second)
        assert player.read_recording() == second
        assert not player.request("info")["armed"]
    finally:
        player.close()
