"""Configuration and code generation for Zephyr CAN and external FD controllers."""

import asyncio
from collections.abc import Callable
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch

import pytest

from esphome.components.zephyr import zephyr_data
from esphome.components.zephyr.const import KEY_OVERLAY, KEY_PRJ_CONF
from esphome.components.zephyr_can.canbus import (
    CONFIG_SCHEMA,
    _validate_instance,
    to_code,
)
import esphome.config_validation as cv
from esphome.const import PlatformFramework
from esphome.cpp_generator import MockObj
from esphome.types import ConfigType
from tests.component_tests.types import SetCoreConfigCallable
from tests.unit_tests.components.zephyr.zephyr_state import empty_zephyr_data


def _zephyr(set_core_config: SetCoreConfigCallable, variant: str = "ESP32") -> None:
    set_core_config(
        PlatformFramework.ZEPHYR_ZEPHYR,
        platform_data=empty_zephyr_data(variant=variant),
    )


@pytest.mark.parametrize("label", ["can1", "fdcan1", "mcp2518fd", "&MCP2518FD"])
def test_controller_label(label: str) -> None:
    assert _validate_instance(label) == label.removeprefix("&").lower()


@pytest.mark.parametrize("label", ["0can", "can@0", "can; bad", "", "can/path"])
def test_invalid_controller_label(label: str) -> None:
    with pytest.raises(cv.Invalid, match="devicetree node label"):
        _validate_instance(label)


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"mode": "FD"}, "requires 'data_bit_rate'"),
        ({"data_bit_rate": "2000KBPS"}, "requires FD mode"),
        ({"default_fd": True}, "default_fd requires FD mode"),
        (
            {
                "mode": "FD",
                "data_bit_rate": "2000KBPS",
                "default_fd": False,
                "default_bit_rate_switch": True,
            },
            "requires default_fd: true",
        ),
        ({"mode": []}, "length of value must be at least 1"),
        ({"mode": "INVALID"}, "Unknown value"),
        ({"tx_pin": 4, "rx_pin": 5}, "only supported on STM32"),
    ],
)
def test_invalid_configuration(
    set_core_config: SetCoreConfigCallable, options: ConfigType, error: str
) -> None:
    _zephyr(set_core_config)
    with pytest.raises(cv.Invalid, match=error):
        CONFIG_SCHEMA({"can_id": 0x123, "instance": "mcp2518fd", **options})


def test_non_stm32_requires_controller(set_core_config: SetCoreConfigCallable) -> None:
    _zephyr(set_core_config)
    with pytest.raises(cv.Invalid, match="Set 'instance'"):
        CONFIG_SCHEMA({"can_id": 0x123})


def test_stm32_keeps_classic_defaults(set_core_config: SetCoreConfigCallable) -> None:
    _zephyr(set_core_config, "STM32L4")
    config = CONFIG_SCHEMA({"can_id": 0x123})
    assert config["mode"] == ["NORMAL"]
    assert config["default_fd"] is False
    assert config["default_bit_rate_switch"] is False


@pytest.mark.parametrize(
    ("mode", "expected", "fd"),
    [
        ("NORMAL", "CAN_MODE_NORMAL", False),
        ("LOOPBACK", "CAN_MODE_LOOPBACK", False),
        ("LISTENONLY", "CAN_MODE_LISTENONLY", False),
        (["normal", "listenonly"], "CAN_MODE_NORMAL | CAN_MODE_LISTENONLY", False),
        (["NORMAL", "LOOPBACK"], "CAN_MODE_NORMAL | CAN_MODE_LOOPBACK", False),
        ("FD", "CAN_MODE_FD", True),
        (["FD", "LISTENONLY"], "CAN_MODE_FD | CAN_MODE_LISTENONLY", True),
        (["FD", "LOOPBACK"], "CAN_MODE_FD | CAN_MODE_LOOPBACK", True),
        (["NORMAL", "FD"], "CAN_MODE_NORMAL | CAN_MODE_FD", True),
        (["LOOPBACK", "LISTENONLY"], "CAN_MODE_LOOPBACK | CAN_MODE_LISTENONLY", False),
        (
            ["FD", "LOOPBACK", "LISTENONLY"],
            "CAN_MODE_FD | CAN_MODE_LOOPBACK | CAN_MODE_LISTENONLY",
            True,
        ),
    ],
)
def test_external_controller_codegen(
    set_core_config: SetCoreConfigCallable,
    mode: str | list[str],
    expected: str,
    fd: bool,
) -> None:
    _zephyr(set_core_config)
    config = CONFIG_SCHEMA(
        {
            "can_id": 0x123,
            "instance": "mcp2518fd",
            "mode": mode,
            **({"data_bit_rate": "2000KBPS"} if fd else {}),
        }
    )
    with (
        patch(
            "esphome.components.zephyr_can.canbus.validate_dts_label_exists",
            return_value=False,
        ),
        patch("esphome.components.zephyr_can.canbus.register_canbus", new=AsyncMock()),
        patch("esphome.codegen.new_Pvariable", return_value=MockObj("bus")) as new_var,
    ):
        asyncio.run(to_code(config))

    args = new_var.call_args.args
    assert "DT_NODELABEL(mcp2518fd)" in str(args[1])
    assert str(args[3]) == expected
    assert args[5] == (2000000 if fd else 0)
    assert config["default_fd"] == fd
    prj_conf = zephyr_data()[KEY_PRJ_CONF][""]
    assert ("CONFIG_CAN_FD_MODE" in prj_conf) == fd
    # The external node is declared later in the user's app overlay.
    assert not zephyr_data()[KEY_OVERLAY][""]


@pytest.mark.parametrize(
    ("filename", "instance", "rx_pin", "tx_pin", "mode"),
    [
        ("esp32_mcp251xfd.yaml", "mcp2518fd", None, None, "CAN_MODE_FD"),
        ("nucleo_g474re.yaml", "fdcan1", "pa11", "pa12", "CAN_MODE_FD"),
        (
            "nucleo_u575zi_q.yaml",
            "fdcan1",
            "pd0",
            "pd1",
            "CAN_MODE_FD | CAN_MODE_LOOPBACK",
        ),
    ],
)
def test_fd_example_codegen(
    generate_main: Callable[[str | Path], str],
    component_config_path: Callable[[str], Path],
    filename: str,
    instance: str,
    rx_pin: str | None,
    tx_pin: str | None,
    mode: str,
) -> None:
    with (
        patch("esphome.components.zephyr.dts_fetch.fetch_board_dts", new=AsyncMock()),
        patch(
            "esphome.components.zephyr_can.canbus.validate_dts_label_exists",
            return_value=instance == "fdcan1",
        ),
        patch(
            "esphome.components.zephyr.dts_lookup.get_rng_node_label",
            return_value="rng",
        ),
    ):
        main = generate_main(component_config_path(filename))
    assert f"DEVICE_DT_GET(DT_NODELABEL({instance}))" in main
    if rx_pin is not None:
        overlay = zephyr_data()[KEY_OVERLAY][""]
        assert f"&fdcan1_rx_{rx_pin} &fdcan1_tx_{tx_pin}" in overlay
    assert f"{mode}, 500000, 2000000" in main
    assert "set_default_fd(true)" in main
    assert "set_default_bit_rate_switch(true)" in main
    assert re.search(r"set_fd\(\[\]\(\) -> bool \{\s*return false;", main)
    assert re.search(r"set_bit_rate_switch\(\[\]\(\) -> bool \{\s*return false;", main)
    assert re.search(r"set_pad_data\(\[\]\(\) -> bool \{\s*return true;", main)
    assert zephyr_data()[KEY_PRJ_CONF][""]["CONFIG_CAN_FD_MODE"][0] is True
