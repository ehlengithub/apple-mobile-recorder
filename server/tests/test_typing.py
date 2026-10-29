import asyncio

import pytest

from hidagent import clipboard
from hidagent import phone as phone_module
from hidagent.phone import Phone


class Device:
    links = 0

    def __init__(self, mac):
        self.sent, self.mac, self.pasted = [], mac, []

    async def send(self, line):
        self.sent.append(line)
        self.pasted.append(self.mac.get("text"))  # what the phone would receive at the moment of the keystroke
        return "ok"


@pytest.fixture
def mac(monkeypatch):
    """An in-memory Mac clipboard; no waiting for Universal Clipboard."""
    board = {"text": "the user's own copy"}
    monkeypatch.setattr(clipboard, "supported", lambda: True)
    monkeypatch.setattr(clipboard, "write", lambda text: board.update(text=text))
    monkeypatch.setattr(phone_module, "HANDOFF_WAIT", 0)
    return board


def test_ascii_is_typed_key_by_key(tmp_path, mac):
    device = Device(mac)
    asyncio.run(Phone(device, tmp_path / "c.json").type("Calendar"))
    assert device.sent == ["type Calendar"]
    assert mac["text"] == "the user's own copy"


def test_chinese_is_pasted_and_stays_on_the_clipboard(tmp_path, mac):
    device = Device(mac)
    asyncio.run(Phone(device, tmp_path / "c.json").type("打开设置 ⚙️"))
    assert device.sent == ["key cmd+v"] and device.pasted == ["打开设置 ⚙️"]
    assert mac["text"] == "打开设置 ⚙️"  # never swapped back while an "Allow Paste" prompt may be open


def test_non_ascii_needs_a_mac(tmp_path, mac, monkeypatch):
    monkeypatch.setattr(clipboard, "supported", lambda: False)
    device = Device(mac)
    with pytest.raises(RuntimeError, match="Mac"):
        asyncio.run(Phone(device, tmp_path / "c.json").type("你好"))
    assert device.sent == []
