#include "phase_power.h"

#include <cmath>

#include "esphome/core/log.h"

namespace esphome::phase_power {

static const char *const TAG = "phase_power";
static const uint8_t ADS1115_REGISTER_CONVERSION = 0x00;
static const uint8_t ADS1115_REGISTER_CONFIG = 0x01;
static const uint8_t ADS1115_REGISTER_LOW_THRESHOLD = 0x02;
static const uint8_t ADS1115_REGISTER_HIGH_THRESHOLD = 0x03;
// Same settings throughout, only the MUX field differs. Both current channels
// read against A3, not A0-A1 and A2-A3 as the schematic suggests: on this board
// A1 is not wired, so MUX 000 measured A0 against a floating pin. A floating
// input is pulled part-way along with the signal by the PGA's input structure,
// which cancelled about half the difference -- channel 1 read exactly half,
// with no phase shift, and swapping the clamp did not help (2026-09-13).
// A3 carries VBIAS and both CT return legs, so 001 (A0-A3) and 011 (A2-A3)
// share a reference that actually exists.
// A0-A1 (mux 000) for CT #1, A2-A3 (mux 011) for CT #2.
// +/-2.048 V, continuous, 860 SPS, comparator queue = assert after 1 conversion.
static const uint16_t ADS1115_CURRENT_CONFIG_CH1 = 0x14E0;
static const uint16_t ADS1115_CURRENT_CONFIG_CH2 = 0x34E0;
static const uint16_t ADS1115_VOLTAGE_CONFIG = 0x44E0;  // A0-GND, +/-2.048 V, continuous, 860 SPS, RDY enabled
// VBIAS sits at ~1.65 V, so the differential swing clips against the 3.3 V rail
// well before the +/-2.048 V PGA limit. Warn a little below that.
static const float CURRENT_SATURATION_V = 1.55f;

void IRAM_ATTR PhasePower::current_rdy_isr_(PhasePower *arg) {
  arg->current_rdy_count_++;
  arg->current_rdy_us_ = micros();
  arg->current_rdy_pending_ = true;
}

void IRAM_ATTR PhasePower::voltage_rdy_isr_(PhasePower *arg) {
  arg->voltage_rdy_count_++;
  arg->voltage_rdy_us_ = micros();
  arg->voltage_rdy_pending_ = true;
}

void PhasePower::set_i2c_bus(i2c::I2CBus *bus) {
  this->current_adc_.set_i2c_bus(bus);
  this->voltage_adc_.set_i2c_bus(bus);
}

void PhasePower::set_addresses(uint8_t current_address, uint8_t voltage_address) {
  this->current_adc_.set_i2c_address(current_address);
  this->voltage_adc_.set_i2c_address(voltage_address);
}

void PhasePower::set_calibration(float voltage_calibration, float current_calibration) {
  this->voltage_calibration_ = voltage_calibration;
  this->current_calibration_ = current_calibration;
  // Codegen calls this before any per-channel override, so both channels start
  // from the global value and channel 2 may replace it afterwards.
  for (auto &channel : this->channels_)
    channel.calibration = current_calibration;
}

bool PhasePower::configure_ads_(i2c::I2CDevice &adc, uint16_t config) {
  if (!adc.write_byte_16(ADS1115_REGISTER_CONFIG, config)) {
    ESP_LOGW(TAG, "Could not configure ADS1115 at 0x%02X", adc.get_i2c_address());
    return false;
  }
  return true;
}

bool PhasePower::configure_rdy_(i2c::I2CDevice &adc) {
  // ADS1115 conversion-ready mode: comparator thresholds force ALERT/RDY low
  // at every completed conversion. ALERT/RDY is an open-drain active-low pin.
  return adc.write_byte_16(ADS1115_REGISTER_LOW_THRESHOLD, 0x0000) &&
         adc.write_byte_16(ADS1115_REGISTER_HIGH_THRESHOLD, 0x8000);
}

bool PhasePower::read_ads_(i2c::I2CDevice &adc, float *value) {
  uint16_t raw;
  if (!adc.read_byte_16(ADS1115_REGISTER_CONVERSION, &raw))
    return false;
  *value = static_cast<int16_t>(raw) * (2.048f / 32768.0f);
  return true;
}

void PhasePower::setup() {
  this->channels_[0].config = ADS1115_CURRENT_CONFIG_CH1;
  this->channels_[1].config = ADS1115_CURRENT_CONFIG_CH2;
  if (!this->configure_ads_(this->current_adc_, this->channels_[0].config) ||
      !this->configure_ads_(this->voltage_adc_, ADS1115_VOLTAGE_CONFIG) ||
      !this->configure_rdy_(this->current_adc_) || !this->configure_rdy_(this->voltage_adc_)) {
    this->mark_failed();
    return;
  }
  this->current_rdy_pin_->setup();
  this->voltage_rdy_pin_->setup();
  this->current_rdy_pin_->attach_interrupt(PhasePower::current_rdy_isr_, this, gpio::INTERRUPT_FALLING_EDGE);
  this->voltage_rdy_pin_->attach_interrupt(PhasePower::voltage_rdy_isr_, this, gpio::INTERRUPT_FALLING_EDGE);
  this->high_freq_.start();
  this->set_interval("rdy_diagnostic", 5000, [this]() {
    const uint32_t current_count = this->current_rdy_count_;
    const uint32_t voltage_count = this->voltage_rdy_count_;
    const uint32_t current_serviced = this->current_serviced_;
    const uint32_t voltage_serviced = this->voltage_serviced_;
    this->current_rdy_count_ = 0;
    this->voltage_rdy_count_ = 0;
    this->current_serviced_ = 0;
    this->voltage_serviced_ = 0;
    ESP_LOGD(TAG, "RDY / 5 s: IRQ I=%lu V=%lu; read I=%lu V=%lu; paired=%lu dropped-I=%lu",
             current_count, voltage_count, current_serviced, voltage_serviced, this->paired_samples_,
             this->dropped_current_samples_);
    this->paired_samples_ = 0;
    this->dropped_current_samples_ = 0;
  });
  this->reset_accumulators_();
}

void PhasePower::enqueue_current_(float value, uint32_t timestamp_us) {
  if (this->current_queue_count_ == CURRENT_QUEUE_SIZE) {
    this->current_queue_tail_ = (this->current_queue_tail_ + 1) % CURRENT_QUEUE_SIZE;
    this->current_queue_count_--;
    this->dropped_current_samples_++;
  }
  this->current_queue_[this->current_queue_head_] = value;
  this->current_time_queue_[this->current_queue_head_] = timestamp_us;
  this->current_queue_head_ = (this->current_queue_head_ + 1) % CURRENT_QUEUE_SIZE;
  this->current_queue_count_++;
}

bool PhasePower::dequeue_current_(float *value, uint32_t *timestamp_us) {
  if (this->current_queue_count_ == 0)
    return false;
  *value = this->current_queue_[this->current_queue_tail_];
  *timestamp_us = this->current_time_queue_[this->current_queue_tail_];
  this->current_queue_tail_ = (this->current_queue_tail_ + 1) % CURRENT_QUEUE_SIZE;
  this->current_queue_count_--;
  return true;
}

void PhasePower::add_aligned_sample_(float current, float voltage) {
  this->status_clear_warning();
  this->sum_current_ += current;
  this->sum_voltage_ += voltage;
  this->sum_current_sq_ += current * current;
  this->sum_voltage_sq_ += voltage * voltage;
  this->sum_product_ += current * voltage;
  const float magnitude = std::fabs(current);
  if (magnitude > this->peak_current_abs_)
    this->peak_current_abs_ = magnitude;
  this->sample_count_++;
  this->paired_samples_++;
  if (this->sample_count_ >= this->samples_per_result_)
    this->publish_result_();
}

void PhasePower::add_voltage_sample_(float value, uint32_t timestamp_us) {
  if (this->voltage_history_count_ < VOLTAGE_HISTORY_SIZE) {
    const uint8_t index = this->voltage_history_count_++;
    this->voltage_history_[index] = value;
    this->voltage_time_history_[index] = timestamp_us;
    return;
  }
  for (uint8_t index = 1; index < VOLTAGE_HISTORY_SIZE; index++) {
    this->voltage_history_[index - 1] = this->voltage_history_[index];
    this->voltage_time_history_[index - 1] = this->voltage_time_history_[index];
  }
  this->voltage_history_[VOLTAGE_HISTORY_SIZE - 1] = value;
  this->voltage_time_history_[VOLTAGE_HISTORY_SIZE - 1] = timestamp_us;
}

void PhasePower::loop() {
  if (this->is_failed())
    return;
  // ALERT/RDY gives the exact completion time. The conversion register is read
  // immediately afterwards; voltage is linearly interpolated to the current
  // conversion timestamp before it enters the RMS/power batch.
  if (this->current_rdy_pending_) {
    const uint32_t sample_us = this->current_rdy_us_;
    this->current_rdy_pending_ = false;
    float current;
    if (this->read_ads_(this->current_adc_, &current)) {
      this->current_serviced_++;
      if (this->settle_remaining_ > 0) {
        // Conversion was already running when the mux moved: it still holds the
        // previous channel, so it must not enter this channel's batch.
        this->settle_remaining_--;
      } else {
        if (this->channels_[this->active_channel_].invert)
          current = -current;
        this->enqueue_current_(current, sample_us);
      }
    }
  }
  if (this->voltage_rdy_pending_) {
    const uint32_t sample_us = this->voltage_rdy_us_;
    this->voltage_rdy_pending_ = false;
    float voltage;
    if (this->read_ads_(this->voltage_adc_, &voltage)) {
      this->voltage_serviced_++;
      this->add_voltage_sample_(voltage, sample_us);
    }
  }

  if (this->voltage_history_count_ < 2 || this->current_queue_count_ == 0)
    return;
  while (this->current_queue_count_ > 0) {
    const uint32_t current_us = this->current_time_queue_[this->current_queue_tail_];
    const uint32_t target_us = current_us + this->voltage_lag_correction_us_;
    const int32_t from_oldest_us = static_cast<int32_t>(target_us - this->voltage_time_history_[0]);
    const int32_t from_latest_us = static_cast<int32_t>(target_us - this->voltage_time_history_[this->voltage_history_count_ - 1]);
    if (from_latest_us > 0)
      return;  // this current sample belongs to the next voltage interval
    float current;
    uint32_t ignored_timestamp;
    this->dequeue_current_(&current, &ignored_timestamp);
    if (from_oldest_us < 0) {
      this->dropped_current_samples_++;
      continue;
    }
    // Guarded by from_latest_us <= 0 above: the loop always breaks at or before
    // interval == voltage_history_count_ - 2, so [interval + 1] stays in range.
    uint8_t interval = 0;
    for (; interval + 1 < this->voltage_history_count_; interval++) {
      if (static_cast<int32_t>(target_us - this->voltage_time_history_[interval + 1]) <= 0)
        break;
    }
    const int32_t span_us = static_cast<int32_t>(this->voltage_time_history_[interval + 1] - this->voltage_time_history_[interval]);
    if (span_us <= 0) {
      this->dropped_current_samples_++;
      continue;
    }
    const int32_t from_previous_us = static_cast<int32_t>(target_us - this->voltage_time_history_[interval]);
    const float fraction = static_cast<float>(from_previous_us) / static_cast<float>(span_us);
    const float voltage = this->voltage_history_[interval] + fraction * (this->voltage_history_[interval + 1] - this->voltage_history_[interval]);
    this->add_aligned_sample_(current, voltage);
    if (this->sample_count_ == 0)
      return;  // batch just published and the mux may have moved; restart cleanly
  }
}

void PhasePower::advance_channel_() {
  if (this->channel_count_ < 2)
    return;
  this->active_channel_ = (this->active_channel_ + 1) % this->channel_count_;
  this->configure_ads_(this->current_adc_, this->channels_[this->active_channel_].config);
  // Writing the config register restarts conversion. Anything already queued
  // was sampled from the previous input pair, so discard it.
  this->current_queue_head_ = 0;
  this->current_queue_tail_ = 0;
  this->current_queue_count_ = 0;
  this->current_rdy_pending_ = false;
  this->settle_remaining_ = CHANNEL_SETTLE_SAMPLES;
}

void PhasePower::publish_result_() {
  const double n = this->sample_count_;
  const double mean_current = this->sum_current_ / n;
  const double mean_voltage = this->sum_voltage_ / n;
  const double current_variance = std::max(0.0, this->sum_current_sq_ / n - mean_current * mean_current);
  const double voltage_variance = std::max(0.0, this->sum_voltage_sq_ / n - mean_voltage * mean_voltage);
  const CurrentChannel &channel = this->channels_[this->active_channel_];
  const float current_rms = std::sqrt(current_variance) * channel.calibration;
  const float voltage_rms = std::sqrt(voltage_variance) * this->voltage_calibration_;

  if (this->peak_current_abs_ >= CURRENT_SATURATION_V) {
    ESP_LOGW(TAG, "Channel %u current input near full scale (%.2f V): readings are clipped, lower the burden resistor",
             this->active_channel_ + 1, this->peak_current_abs_);
    this->status_set_warning();
  }

  // Mains voltage does not depend on load, so it is published on every batch
  // even when no current flows. Otherwise the entity would stay unknown until
  // something is switched on.
  if (this->rms_voltage_sensor_ != nullptr)
    this->rms_voltage_sensor_->publish_state(voltage_rms < 10.0f ? 0.0f : voltage_rms);

  const bool below_threshold = current_rms < this->minimum_current_;
  const float published_current = below_threshold ? 0.0f : current_rms;
  const float apparent_power = below_threshold ? 0.0f : voltage_rms * current_rms;
  const float real_power =
      below_threshold
          ? 0.0f
          : (this->sum_product_ / n - mean_current * mean_voltage) * this->voltage_calibration_ * channel.calibration;
  const float power_factor = apparent_power > 0.5f ? real_power / apparent_power : 0.0f;

  if (channel.rms_current != nullptr)
    channel.rms_current->publish_state(published_current);
  if (channel.real_power != nullptr)
    channel.real_power->publish_state(real_power);
  if (channel.apparent_power != nullptr)
    channel.apparent_power->publish_state(apparent_power);
  if (channel.power_factor != nullptr)
    channel.power_factor->publish_state(power_factor);

  if (!below_threshold) {
    ESP_LOGD(TAG,
             "Synchronized batch: ch=%u, n=%u, correction=%d us, Vrms=%.2f V, Irms=%.3f A, S=%.1f VA, P=%.1f W, PF=%.3f",
             this->active_channel_ + 1, this->sample_count_, this->voltage_lag_correction_us_, voltage_rms,
             current_rms, apparent_power, real_power, power_factor);
  }

  this->reset_accumulators_();
  this->advance_channel_();
}

void PhasePower::reset_accumulators_() {
  this->sample_count_ = 0;
  this->sum_voltage_ = 0.0;
  this->sum_current_ = 0.0;
  this->sum_voltage_sq_ = 0.0;
  this->sum_current_sq_ = 0.0;
  this->sum_product_ = 0.0;
  this->peak_current_abs_ = 0.0f;
}

void PhasePower::dump_config() {
  ESP_LOGCONFIG(TAG, "Phase Power:");
  ESP_LOGCONFIG(TAG, "  Current ADS1115: 0x%02X", this->current_adc_.get_i2c_address());
  ESP_LOGCONFIG(TAG, "  Voltage ADS1115: 0x%02X", this->voltage_adc_.get_i2c_address());
  ESP_LOGCONFIG(TAG, "  Current channels: %u%s", this->channel_count_,
                this->channel_count_ > 1 ? " (A0-A1 and A2-A3, alternating per batch)" : " (A0-A1)");
  ESP_LOGCONFIG(TAG, "  Voltage lag correction: %d us", this->voltage_lag_correction_us_);
  ESP_LOGCONFIG(TAG, "  Samples per result: %u", this->samples_per_result_);
}

}  // namespace esphome::phase_power
