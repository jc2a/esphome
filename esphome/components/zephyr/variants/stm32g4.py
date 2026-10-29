import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import (
    CONF_ADVANCED,
    CONF_BOARD,
    CONF_FRAMEWORK,
    CONF_SOURCE,
    ThreadModel,
    Toolchain,
)
from esphome.types import ConfigType

from ..const import (
    ADVANCED_SCHEMA,
    BOOTLOADER_MCUBOOT,
    BOOTLOADER_NONE,
    CONF_BOOTLOADER,
    CONF_RUNNER,
    KEY_BOOTLOADER,
    ZEPHYR_VARIANT_STM32G4,
)
from ..partitions import BootLayout
from . import (
    MAINLINE,
    ZephyrVariant,
    qualify_board,
    resolve_framework_version,
    set_core_data,
)

# Bare name; no SoC/qualifier segment needed -- upstream's board.yml declares a single
# SoC for this board (stm32g474xx), so qualify_board() is a no-op here (soc= is left
# unset below).
_DEFAULT_BOARD = "nucleo_g474re"

# The stock board links the application after MCUboot at offset 0xc000.
_ADVANCED_SCHEMA = ADVANCED_SCHEMA.extend(
    {
        cv.Optional(CONF_BOOTLOADER, default=BOOTLOADER_MCUBOOT): cv.one_of(
            BOOTLOADER_NONE, BOOTLOADER_MCUBOOT, lower=True
        ),
    }
)

# Registry entries — collected by variants/__init__.py
VARIANT_NAME = ZEPHYR_VARIANT_STM32G4
VARIANT = ZephyrVariant(
    boot=BootLayout(0x0),
    sdk=MAINLINE,
    sdk_name="zephyr",
    family="stm32",
    valid_toolchains=(Toolchain.SDK_ZEPHYR,),
    toolchain="arm-zephyr-eabi",
    transports=frozenset(),
    # nucleo_g474re's stock layout has no scratch_partition. Its 228K primary and
    # 230K secondary slots suit "offset"; "move" needs a different layout with
    # equal slots or a larger primary slot, supplied via a devicetree overlay.
    swap_methods=frozenset({"move", "offset"}),
    # STM32 GPIO controllers are 16 pins/port (dts/bindings/gpio/st,stm32-gpio.yaml's
    # ngpios defaults to 16), labeled gpioa..gpiog in devicetree -- same scheme as
    # stm32f1. The G4 family's shared SoC dtsi (dts/arm/st/g4/stm32g4.dtsi) wires up
    # the full a-g range, even though a given package may not expose every pin.
    gpio_port_width=16,
    gpio_port_labels=("a", "b", "c", "d", "e", "f", "g"),
    # wdt_iwdg_stm32.c's IWDG_PRESCALER_MAX is 256 on G4 (stm32g4xx_ll_iwdg.h has no
    # LL_IWDG_PRESCALER_1024), reload max 4095, LSI 32kHz (stm32g4xx_hal_conf.h):
    # (4095 + 1) x 256 / 32000 = 32.768s nominal ceiling, below the generic 60s schema max.
    watchdog_max_timeout_ms=32000,
    uart_node_labels={},
)


def config_schema(config: ConfigType) -> ConfigType:
    config = dict(config)
    if CONF_BOARD not in config:
        config[CONF_BOARD] = _DEFAULT_BOARD
    config[CONF_ADVANCED] = _ADVANCED_SCHEMA(config.get(CONF_ADVANCED, {}))
    bootloader = config[CONF_ADVANCED][CONF_BOOTLOADER]
    config[CONF_BOARD] = qualify_board(VARIANT, config[CONF_BOARD])
    _, framework_ver, sdk_name, _ = resolve_framework_version(
        VARIANT, "stm32", config, "mainline STM32 support"
    )
    set_core_data(
        VARIANT_NAME,
        config[CONF_BOARD],
        bootloader if bootloader == BOOTLOADER_MCUBOOT else "",
        framework_ver,
        config,
        framework_type=sdk_name,
        sdk_source=config[CONF_FRAMEWORK].get(CONF_SOURCE),
        runner=config[CONF_ADVANCED].get(CONF_RUNNER),
    )
    return config


async def to_code(config: ConfigType) -> None:
    from .. import (
        zephyr_add_overlay,
        zephyr_add_prj_conf,
        zephyr_add_sysbuild_conf,
        zephyr_data,
        zephyr_setup_preferences,
    )
    from ..dts_lookup import get_rng_node_label

    cg.add_build_flag("-DUSE_ZEPHYR_VARIANT_STM32G4")
    cg.add_define("ESPHOME_BOARD", config[CONF_BOARD])
    cg.add_define("ESPHOME_VARIANT", "STM32G4")
    cg.add_define(ThreadModel.SINGLE)
    zephyr_setup_preferences()
    zephyr_add_prj_conf("REBOOT", True)
    # get_mac_address_raw() (zephyr/core.cpp) derives the MAC from the unique ID via hwinfo.
    zephyr_add_prj_conf("HWINFO", True)

    # The STM32G4 SoC dtsi declares a hardware RNG but leaves it disabled
    # (dts/arm/st/g4/stm32g4.dtsi's rng@50060800), so it has to be enabled explicitly.
    # nucleo_g474re enables it in its own DTS, but a custom board may not. Resolve the
    # node from the board's DTS rather than assuming its label, same as stm32u5. If no
    # RNG node is found, Zephyr's test generator is used and the CSPRNG choice can fall
    # back to the insecure TEST_CSPRNG_GENERATOR.
    rng_label = get_rng_node_label(config[CONF_BOARD])
    if rng_label is not None:
        zephyr_add_prj_conf("ENTROPY_GENERATOR", True)
        zephyr_add_overlay(f'&{rng_label} {{ status = "okay"; }};')
    else:
        zephyr_add_prj_conf("TEST_RANDOM_GENERATOR", True)

    if zephyr_data()[KEY_BOOTLOADER] == BOOTLOADER_MCUBOOT:
        zephyr_add_sysbuild_conf("BOOTLOADER_MCUBOOT", True)
        # sysbuild's own BOOT_SIGNATURE_TYPE choice overrides a per-image setting.
        zephyr_add_sysbuild_conf("BOOT_SIGNATURE_TYPE_ECDSA_P256", True)
