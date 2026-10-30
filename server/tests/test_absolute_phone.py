import asyncio
import json
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


def test_legacy_relative_calibration_does_not_block_absolute_device_or_get_overwritten(tmp_path, monkeypatch):
    path = tmp_path / 'calibration.json'
    original = json.dumps({'w': 400, 'h': 850, 'grid_points': 9, 'frame_aspect': .46})
    path.write_text(original)
    device, screen = Device(), Screen()
    phone = Phone(device, path, screen)
    monkeypatch.setattr('hidagent.phone.vision.absolute_pointer', lambda *_: (.595, .5))

    async def run():
        await phone.tap(.5, .5, aim=True, strict=True)
        assert await phone.start_calibration()

    asyncio.run(run())
    assert device.commands[-1] == 'click'
    assert path.read_text() == original and phone.frame_aspect == .46
    # Returning to a relative device must retain its original geometry protection.
    device.absolute = False
    with pytest.raises(PhoneUnavailable, match='比例'):
        phone._check_geometry()


def test_absolute_geometry_change_requires_explicit_rebind_or_new_hid_session(tmp_path):
    phone = Phone(Device(), tmp_path / 'unused.json', Screen())
    phone._check_geometry()
    phone.screen.aspect = 1.4
    with pytest.raises(PhoneUnavailable, match='比例'):
        asyncio.run(phone.tap(.5, .5))
    assert not phone.device.commands
    assert asyncio.run(phone.start_calibration())
    phone._check_geometry()
    phone.screen.aspect = .8
    with pytest.raises(PhoneUnavailable):
        phone._check_geometry()
    phone.device.links += 1
    phone._check_geometry()
    phone.screen.live = False
    with pytest.raises(PhoneUnavailable, match='投屏'):
        asyncio.run(phone.start_calibration())
