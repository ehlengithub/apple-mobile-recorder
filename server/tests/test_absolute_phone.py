import asyncio
import time

import pytest

from hidagent.phone import Phone, PhoneUnavailable


class Device:
    absolute = True
    connected = True
    links = 1

    def __init__(self):
        self.commands = []

    async def send(self, command):
        self.commands.append(command)


class Screen:
    live = True
    updated = time.time()
    aspect = .7
    frame = b'frame'

    async def settled(self, **kwargs):
        return True

    async def fresh(self, *args):
        return self.frame


def test_absolute_tap_uses_native_coordinates_and_confirms_return_before_click(tmp_path, monkeypatch):
    device = Device()
    phone = Phone(device, tmp_path/'unused.json', Screen())
    monkeypatch.setattr('hidagent.phone.vision.absolute_pointer', lambda *a: (.595, .5))
    result = asyncio.run(phone.tap(.5, .5, aim=True, strict=True))
    assert phone.precision_calibrated and result[:2] == pytest.approx((.5, .5))
    assert device.commands == ['release', 'abs 18350 16384', 'abs 19496 16384', 'abs 16384 16384', 'click']
    assert not (tmp_path/'unused.json').exists()


@pytest.mark.parametrize('found', [None, (.65, .5)])
def test_absolute_uncertain_pointer_returns_to_target_without_click(tmp_path, monkeypatch, found):
    device = Device()
    phone = Phone(device, tmp_path/'unused.json', Screen())
    monkeypatch.setattr('hidagent.phone.vision.absolute_pointer', lambda *a: found)
    with pytest.raises(PhoneUnavailable, match='未点击'):
        asyncio.run(phone.tap(.5, .5, aim=True, strict=True))
    assert 'click' not in device.commands and device.commands[-1] == 'abs 16384 16384'


def test_absolute_offline_screen_does_not_move_or_click(tmp_path):
    device = Device()
    screen = Screen()
    screen.live = False
    phone = Phone(device, tmp_path/'unused.json', screen)
    with pytest.raises(PhoneUnavailable):
        asyncio.run(phone.tap(.5, .5, aim=True, strict=True))
    assert not device.commands


def test_absolute_pointer_handles_hidden_baseline_and_rejects_static_or_colored_changes():
    import io

    from PIL import Image, ImageDraw

    from hidagent.vision import absolute_pointer

    def frame(pointer=None):
        image = Image.new('RGB', (320, 400), 'white')
        if pointer:
            ImageDraw.Draw(image).ellipse((154, 194, 166, 206), fill=pointer)
        stream = io.BytesIO()
        image.save(stream, 'PNG')
        return stream.getvalue()

    blank, cursor = frame(), frame('gray')
    assert absolute_pointer(blank, cursor, (.5, .5)) == pytest.approx((.5, .5))
    assert absolute_pointer(cursor, cursor, (.5, .5)) is None
    assert absolute_pointer(blank, frame('red'), (.5, .5)) is None
