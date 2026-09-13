import esphome.codegen as cg
from esphome.components import i2c, sensor
from esphome import pins
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_I2C_ID

DEPENDENCIES = ["i2c"]

phase_power_ns = cg.esphome_ns.namespace("phase_power")
PhasePower = phase_power_ns.class_("PhasePower", cg.Component)

CONF_REAL_POWER = "real_power"
CONF_APPARENT_POWER = "apparent_power"
CONF_POWER_FACTOR = "power_factor"
CONF_RMS_VOLTAGE = "rms_voltage"
CONF_RMS_CURRENT = "rms_current"
CONF_INVERT_CURRENT = "invert_current"
CONF_CURRENT_CHANNEL_2 = "current_channel_2"


def _real_power_schema():
    return sensor.sensor_schema(
        unit_of_measurement="W", device_class="power", state_class="measurement"
    )


def _apparent_power_schema():
    return sensor.sensor_schema(
        unit_of_measurement="VA", device_class="apparent_power", state_class="measurement"
    )


def _power_factor_schema():
    return sensor.sensor_schema(
        device_class="power_factor", state_class="measurement"
    )


def _rms_current_schema():
    return sensor.sensor_schema(
        unit_of_measurement="A", device_class="current", state_class="measurement"
    )


# Second current transformer on the same ADS1115, read through mux A2-A3.
# The mux is moved once per completed batch, so this channel is sampled at the
# full 860 SPS but its result refreshes every other batch.
CHANNEL_2_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_REAL_POWER): _real_power_schema(),
        cv.Required(CONF_APPARENT_POWER): _apparent_power_schema(),
        cv.Required(CONF_POWER_FACTOR): _power_factor_schema(),
        cv.Optional(CONF_RMS_CURRENT): _rms_current_schema(),
        cv.Optional(CONF_INVERT_CURRENT, default=False): cv.boolean,
        # Defaults to the global current_calibration. Set it only to trim this
        # clamp against channel 1 after measuring both on the same conductor.
        cv.Optional("current_calibration"): cv.float_,
    }
)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(PhasePower),
        cv.GenerateID(CONF_I2C_ID): cv.use_id(i2c.I2CBus),
        cv.Optional("current_address", default=0x48): cv.hex_int_range(0x08, 0x77),
        cv.Optional("voltage_address", default=0x49): cv.hex_int_range(0x08, 0x77),
        cv.Required("voltage_calibration"): cv.float_,
        cv.Required("current_calibration"): cv.float_,
        cv.Optional(CONF_INVERT_CURRENT, default=False): cv.boolean,
        cv.Optional("minimum_current", default=0.05): cv.float_range(min=0.0),
        cv.Optional("voltage_lag_correction_us", default=0): cv.int_range(min=-5000, max=5000),
        cv.Required("current_rdy_pin"): pins.internal_gpio_input_pin_schema,
        cv.Required("voltage_rdy_pin"): pins.internal_gpio_input_pin_schema,
        cv.Optional("samples_per_result", default=800): cv.int_range(min=100, max=2000),
        cv.Optional(CONF_RMS_VOLTAGE): sensor.sensor_schema(
            unit_of_measurement="V", device_class="voltage", state_class="measurement"
        ),
        cv.Optional(CONF_RMS_CURRENT): _rms_current_schema(),
        cv.Required(CONF_REAL_POWER): _real_power_schema(),
        cv.Required(CONF_APPARENT_POWER): _apparent_power_schema(),
        cv.Required(CONF_POWER_FACTOR): _power_factor_schema(),
        cv.Optional(CONF_CURRENT_CHANNEL_2): CHANNEL_2_SCHEMA,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    bus = await cg.get_variable(config[CONF_I2C_ID])
    cg.add(var.set_i2c_bus(bus))
    cg.add(var.set_addresses(config["current_address"], config["voltage_address"]))
    cg.add(var.set_calibration(config["voltage_calibration"], config["current_calibration"]))
    cg.add(var.set_invert_current(config[CONF_INVERT_CURRENT]))
    cg.add(var.set_minimum_current(config["minimum_current"]))
    cg.add(var.set_voltage_lag_correction_us(config["voltage_lag_correction_us"]))
    current_rdy_pin = await cg.gpio_pin_expression(config["current_rdy_pin"])
    voltage_rdy_pin = await cg.gpio_pin_expression(config["voltage_rdy_pin"])
    cg.add(var.set_current_rdy_pin(current_rdy_pin))
    cg.add(var.set_voltage_rdy_pin(voltage_rdy_pin))
    cg.add(var.set_samples_per_result(config["samples_per_result"]))

    real_power = await sensor.new_sensor(config[CONF_REAL_POWER])
    apparent_power = await sensor.new_sensor(config[CONF_APPARENT_POWER])
    power_factor = await sensor.new_sensor(config[CONF_POWER_FACTOR])
    cg.add(var.set_real_power_sensor(real_power))
    cg.add(var.set_apparent_power_sensor(apparent_power))
    cg.add(var.set_power_factor_sensor(power_factor))
    if CONF_RMS_VOLTAGE in config:
        rms_voltage = await sensor.new_sensor(config[CONF_RMS_VOLTAGE])
        cg.add(var.set_rms_voltage_sensor(rms_voltage))
    if CONF_RMS_CURRENT in config:
        rms_current = await sensor.new_sensor(config[CONF_RMS_CURRENT])
        cg.add(var.set_rms_current_sensor(rms_current))

    if CONF_CURRENT_CHANNEL_2 in config:
        channel_2 = config[CONF_CURRENT_CHANNEL_2]
        cg.add(var.enable_channel_2())
        cg.add(var.set_channel_2_invert_current(channel_2[CONF_INVERT_CURRENT]))
        if "current_calibration" in channel_2:
            cg.add(var.set_channel_2_current_calibration(channel_2["current_calibration"]))
        real_power_2 = await sensor.new_sensor(channel_2[CONF_REAL_POWER])
        apparent_power_2 = await sensor.new_sensor(channel_2[CONF_APPARENT_POWER])
        power_factor_2 = await sensor.new_sensor(channel_2[CONF_POWER_FACTOR])
        cg.add(var.set_channel_2_real_power_sensor(real_power_2))
        cg.add(var.set_channel_2_apparent_power_sensor(apparent_power_2))
        cg.add(var.set_channel_2_power_factor_sensor(power_factor_2))
        if CONF_RMS_CURRENT in channel_2:
            rms_current_2 = await sensor.new_sensor(channel_2[CONF_RMS_CURRENT])
            cg.add(var.set_channel_2_rms_current_sensor(rms_current_2))
