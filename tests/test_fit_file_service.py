import pytest
from services.fit_file_service import FitFileService

def test_fit_file_service_init():
    fs = FitFileService()
    assert isinstance(fs, FitFileService)


from fit_tool.fit_file import FitFile
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.profile_type import FileType


def _device(index, serial):
    d = DeviceInfoMessage()
    d.device_index = index
    d.manufacturer = 255
    d.product = 1
    d.software_version = 1.0
    if serial is not None:
        d.serial_number = serial
    return d


def _make_fit(path, with_serial=True):
    builder = FitFileBuilder(auto_define=True)
    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = 255
    file_id.product = 1
    if with_serial:
        file_id.serial_number = 111
    file_id.time_created = 1700000000000
    builder.add(file_id)
    builder.add(_device(0, 111 if with_serial else None))
    builder.add(_device(1, 222))
    builder.build().to_file(str(path))


def _read(path):
    msgs = [r.message for r in FitFile.from_file(str(path)).records]
    file_id = next(m for m in msgs if isinstance(m, FileIdMessage))
    devices = {m.device_index: m for m in msgs if isinstance(m, DeviceInfoMessage)}
    return file_id, devices


def test_serial_number_written(tmp_path):
    src = tmp_path / "a.fit"
    _make_fit(src)
    fs = FitFileService(serial_number=3412345678)
    out = fs.modify_device_info(str(src))
    try:
        file_id, devices = _read(out)
        assert file_id.serial_number == 3412345678
        assert devices[0].serial_number == 3412345678
        assert devices[1].serial_number == 222  # sensor untouched
    finally:
        fs.cleanup_file(out)


def test_serial_number_added_when_missing_in_source(tmp_path):
    src = tmp_path / "a.fit"
    _make_fit(src, with_serial=False)
    fs = FitFileService(serial_number=3412345678)
    out = fs.modify_device_info(str(src))
    try:
        file_id, devices = _read(out)
        assert file_id.serial_number == 3412345678
        assert devices[0].serial_number == 3412345678
        assert devices[1].serial_number == 222  # later message of same type still decodes
        assert devices[1].manufacturer == 1
        assert devices[1].product == 4575
    finally:
        fs.cleanup_file(out)


def test_serial_number_argument_overrides_default(tmp_path):
    src = tmp_path / "a.fit"
    _make_fit(src)
    fs = FitFileService(serial_number=5)
    out = fs.modify_device_info(str(src), serial_number=7)
    try:
        file_id, _ = _read(out)
        assert file_id.serial_number == 7
    finally:
        fs.cleanup_file(out)


def test_serial_number_kept_when_unset(tmp_path):
    src = tmp_path / "a.fit"
    _make_fit(src)
    fs = FitFileService()
    out = fs.modify_device_info(str(src))
    try:
        file_id, devices = _read(out)
        assert file_id.serial_number == 111
        assert devices[0].serial_number == 111
    finally:
        fs.cleanup_file(out)


@pytest.mark.parametrize("bad", [0, -1, 2**32])
def test_invalid_serial_number(bad):
    with pytest.raises(ValueError):
        FitFileService(serial_number=bad)
