import pytest


def pytest_addoption(parser):
    parser.addoption("--device", action="store_true", help="run tests/device against a connected adb device")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--device"):
        return
    skip = pytest.mark.skip(reason="needs a real device: pytest tests/device --device")
    for item in items:
        if "device" in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "device: needs a connected Android device over adb")

