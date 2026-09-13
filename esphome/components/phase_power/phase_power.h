#pragma once

#include "esphome/components/i2c/i2c.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"
#include "esphome/core/gpio.h"
#include "esphome/core/helpers.h"

namespace esphome::phase_power {

// One current transformer. Channel 0 is read through the ADS1115 mux setting
// A0-A1, channel 1 through A2-A3. Only one is converted at a time; the mux is
// moved once per completed batch so each channel still gets the full 860 SPS.
struct CurrentChannel {
  uint16_t config{0};
  bool invert{false};
  // A-per-volt for this channel. Defaults to the global current_calibration and
  // can be trimmed per channel: burden tolerance and unit-to-unit spread between
  // two SCT-013 clamps is typically a few percent.
  float calibration{1.0f};
  sensor::Sensor *rms_current{nullptr};
  sensor::Sensor *real_power{nullptr};
  sensor::Sensor *apparent_power{nullptr};
  sensor::Sensor *power_factor{nullptr};
};

class PhasePower : public Component {
 public:
  void set_i2c_bus(i2c::I2CBus *bus);
  void set_addresses(uint8_t current_address, uint8_t voltage_address);
  void set_calibration(float voltage_calibration, float current_calibration);
  void set_minimum_current(float minimum_current) { this->minimum_current_ = minimum_current; }
  void set_voltage_lag_correction_us(int16_t correction_us) { this->voltage_lag_correction_us_ = correction_us; }
  void set_current_rdy_pin(InternalGPIOPin *pin) { this->current_rdy_pin_ = pin; }
  void set_voltage_rdy_pin(InternalGPIOPin *pin) { this->voltage_rdy_pin_ = pin; }
  void set_samples_per_result(uint16_t samples) { this->samples_per_result_ = samples; }
  void set_rms_voltage_sensor(sensor::Sensor *sensor) { this->rms_voltage_sensor_ = sensor; }

  void set_invert_current(bool invert) { this->channels_[0].invert = invert; }
  void set_real_power_sensor(sensor::Sensor *s) { this->channels_[0].real_power = s; }
  void set_apparent_power_sensor(sensor::Sensor *s) { this->channels_[0].apparent_power = s; }
  void set_power_factor_sensor(sensor::Sensor *s) { this->channels_[0].power_factor = s; }
  void set_rms_current_sensor(sensor::Sensor *s) { this->channels_[0].rms_current = s; }

  void enable_channel_2() { this->channel_count_ = 2; }
  void set_channel_2_invert_current(bool invert) { this->channels_[1].invert = invert; }
  void set_channel_2_current_calibration(float calibration) { this->channels_[1].calibration = calibration; }
  void set_channel_2_real_power_sensor(sensor::Sensor *s) { this->channels_[1].real_power = s; }
  void set_channel_2_apparent_power_sensor(sensor::Sensor *s) { this->channels_[1].apparent_power = s; }
  void set_channel_2_power_factor_sensor(sensor::Sensor *s) { this->channels_[1].power_factor = s; }
  void set_channel_2_rms_current_sensor(sensor::Sensor *s) { this->channels_[1].rms_current = s; }

  void setup() override;
  void loop() override;
  void dump_config() override;

 protected:
  bool configure_ads_(i2c::I2CDevice &adc, uint16_t config);
  bool configure_rdy_(i2c::I2CDevice &adc);
  bool read_ads_(i2c::I2CDevice &adc, float *value);
  void enqueue_current_(float value, uint32_t timestamp_us);
  bool dequeue_current_(float *value, uint32_t *timestamp_us);
  void add_aligned_sample_(float current, float voltage);
  void add_voltage_sample_(float value, uint32_t timestamp_us);
  void reset_accumulators_();
  void publish_result_();
  void advance_channel_();

  i2c::I2CDevice current_adc_;
  i2c::I2CDevice voltage_adc_;
  InternalGPIOPin *current_rdy_pin_{nullptr};
  InternalGPIOPin *voltage_rdy_pin_{nullptr};
  volatile uint32_t current_rdy_count_{0};
  volatile uint32_t voltage_rdy_count_{0};
  volatile bool current_rdy_pending_{false};
  volatile bool voltage_rdy_pending_{false};
  volatile uint32_t current_rdy_us_{0};
  volatile uint32_t voltage_rdy_us_{0};
  uint32_t current_serviced_{0};
  uint32_t voltage_serviced_{0};
  static const uint8_t CURRENT_QUEUE_SIZE = 32;
  float current_queue_[CURRENT_QUEUE_SIZE]{};
  uint32_t current_time_queue_[CURRENT_QUEUE_SIZE]{};
  uint8_t current_queue_head_{0};
  uint8_t current_queue_tail_{0};
  uint8_t current_queue_count_{0};
  static const uint8_t VOLTAGE_HISTORY_SIZE = 4;
  float voltage_history_[VOLTAGE_HISTORY_SIZE]{};
  uint32_t voltage_time_history_[VOLTAGE_HISTORY_SIZE]{};
  uint8_t voltage_history_count_{0};
  static const uint8_t MAX_CURRENT_CHANNELS = 2;
  // Conversions still in flight when the mux moved belong to the old channel.
  static const uint8_t CHANNEL_SETTLE_SAMPLES = 2;
  CurrentChannel channels_[MAX_CURRENT_CHANNELS];
  uint8_t channel_count_{1};
  uint8_t active_channel_{0};
  uint8_t settle_remaining_{0};
  float voltage_calibration_{1.0f};
  float current_calibration_{1.0f};
  float minimum_current_{0.05f};
  uint16_t samples_per_result_{800};
  uint16_t sample_count_{0};
  int16_t voltage_lag_correction_us_{0};
  uint32_t paired_samples_{0};
  uint32_t dropped_current_samples_{0};
  double sum_voltage_{0.0};
  double sum_current_{0.0};
  double sum_voltage_sq_{0.0};
  double sum_current_sq_{0.0};
  double sum_product_{0.0};
  float peak_current_abs_{0.0f};
  sensor::Sensor *rms_voltage_sensor_{nullptr};
  HighFrequencyLoopRequester high_freq_;

  // Definirane u .cpp-u, ne ovdje: inline IRAM rutina u headeru razbija
  // literal pool pri linkanju ("dangerous relocation: l32r").
  static void IRAM_ATTR current_rdy_isr_(PhasePower *arg);
  static void IRAM_ATTR voltage_rdy_isr_(PhasePower *arg);
};

}  // namespace esphome::phase_power
