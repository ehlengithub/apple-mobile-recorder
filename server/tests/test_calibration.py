import asyncio
from types import SimpleNamespace

from hidagent import phone as phone_module
from hidagent.phone import Phone

TRUE_SPAN = (460, 1000)  # mouse units across the real screen


def landing(units):
    """iOS pointer motion that is not proportional: short of the target low on the screen, past it high up."""
    x, y = units[0] / TRUE_SPAN[0], units[1] / TRUE_SPAN[1]
    return 0.96 * x + 0.045, 0.93 * y + 0.03


def seen_with_noise():
    """Where the pointer is found on screen: the landing, read with a few pixels of error (alternating sign)."""
    readings = 0

    def find(units):
        nonlocal readings
        readings += 1
        x, y = landing(units)
        e = 0.006 if readings % 2 else -0.006
        return x + e, y + e
    return find


class Device:
    links = 0

    def __init__(self):
        self.pos = [0, 0]

    async def send(self, line):
        cmd, *args = line.split()
        if cmd == "home":
            self.pos = [0, 0]
        elif cmd == "move":
            self.pos = [self.pos[0] + int(args[0]), self.pos[1] + int(args[1])]
        return "ok"


def test_grid_calibration_corrects_moves_across_the_whole_screen(tmp_path, monkeypatch):
    device = Device()
    phone = Phone(device, tmp_path / "calibration.json", screen=SimpleNamespace(live=True))
    find = seen_with_noise()
    monkeypatch.setattr(phone, "_find_pointer", lambda: asyncio.sleep(0, find(device.pos)))

    assert asyncio.run(phone.start_calibration())
    for target in [(0.2, 0.1), (0.5, 0.5), (0.85, 0.9), (0.3, 0.97)]:
        x, y = landing((phone._dx(target[0]), phone._dy(target[1])))
        assert abs(x - target[0]) < 0.015 and abs(y - target[1]) < 0.015, (target, (x, y))  # ~15 px
    assert len(phone._seen["y"]) == 2 + len(phone_module.CALIBRATION_GRID)
    assert phone._pos is None  # the next gesture homes first
