# Printer heater support
#
# Copyright (C) 2016-2018  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, threading


######################################################################
# Heater
######################################################################

KELVIN_TO_CELCIUS = -273.15
MAX_HEAT_TIME = 5.0
AMBIENT_TEMP = 25.
PID_PARAM_BASE = 255.

class error(Exception):
    pass

class Heater:
    error = error
    def __init__(self, config, sensor):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.name = config.get_name().split()[-1]
        # Setup sensor
        self.sensor = sensor
        self.min_temp = config.getfloat('min_temp', minval=KELVIN_TO_CELCIUS)
        self.max_temp = config.getfloat('max_temp', above=self.min_temp)
        self.sensor.setup_minmax(self.min_temp, self.max_temp)
        self.sensor.setup_callback(self.temperature_callback)
        self.pwm_delay = self.sensor.get_report_time_delta()
        # Setup temperature checks
        self.min_extrude_temp = config.getfloat(
            'min_extrude_temp', 170.,
            minval=self.min_temp, maxval=self.max_temp)
        is_fileoutput = (self.printer.get_start_args().get('debugoutput')
                         is not None)
        self.can_extrude = self.min_extrude_temp <= 0. or is_fileoutput
        self.max_power = config.getfloat('max_power', 1., above=0., maxval=1.)
        self.smooth_time = config.getfloat('smooth_time', 2., above=0.)
        self.inv_smooth_time = 1. / self.smooth_time
        self.lock = threading.Lock()
        self.last_temp = self.smoothed_temp = self.target_temp = 0.
        self.last_temp_time = 0.
        # pwm caching
        self.next_pwm_time = 0.
        self.last_pwm_value = 0.
        # Setup control algorithm sub-class
        algos = {'watermark': ControlBangBang, 'pid': ControlPID}
        algo = config.getchoice('control', algos)
        self.control = algo(self, config)
        # Setup output heater pin
        heater_pin = config.get('heater_pin')
        ppins = self.printer.lookup_object('pins')
        if algo is ControlBangBang and self.max_power == 1.:
            self.mcu_pwm = ppins.setup_pin('digital_out', heater_pin)
        else:
            self.mcu_pwm = ppins.setup_pin('pwm', heater_pin)
            pwm_cycle_time = config.getfloat(
                'pwm_cycle_time', 0.100, above=0., maxval=self.pwm_delay)
            self.mcu_pwm.setup_cycle_time(pwm_cycle_time)
        self.mcu_pwm.setup_max_duration(MAX_HEAT_TIME)
        # Load additional modules
        self.printer.try_load_module(config, "verify_heater %s" % (self.name,))
        self.printer.try_load_module(config, "pid_calibrate")
        self.gcode.register_mux_command(
            "SET_HEATER_TEMPERATURE", "HEATER", self.name,
            self.cmd_SET_HEATER_TEMPERATURE,
            desc=self.cmd_SET_HEATER_TEMPERATURE_help)
    def set_pwm(self, read_time, value):
        if self.target_temp <= 0.:
            value = 0.
        if ((read_time < self.next_pwm_time or not self.last_pwm_value)
            and abs(value - self.last_pwm_value) < 0.05):
            # No significant change in value - can suppress update
            return
        pwm_time = read_time + self.pwm_delay
        self.next_pwm_time = pwm_time + 0.75 * MAX_HEAT_TIME
        self.last_pwm_value = value
        logging.debug("%s: pwm=%.3f@%.3f (from %.3f@%.3f [%.3f])",
                      self.name, value, pwm_time,
                      self.last_temp, self.last_temp_time, self.target_temp)
        self.mcu_pwm.set_pwm(pwm_time, value)
    def temperature_callback(self, read_time, temp):
        with self.lock:
            time_diff = read_time - self.last_temp_time
            self.last_temp = temp
            self.last_temp_time = read_time
            self.control.temperature_update(read_time, temp, self.target_temp)
            temp_diff = temp - self.smoothed_temp
            adj_time = min(time_diff * self.inv_smooth_time, 1.)
            self.smoothed_temp += temp_diff * adj_time
            self.can_extrude = (self.smoothed_temp >= self.min_extrude_temp)
        #logging.debug("temp: %.3f %f = %f", read_time, temp)
    # External commands
    def get_pwm_delay(self):
        return self.pwm_delay
    def get_max_power(self):
        return self.max_power
    def get_smooth_time(self):
        return self.smooth_time
    def set_temp(self, print_time, degrees):
        if degrees and (degrees < self.min_temp or degrees > self.max_temp):
            raise error("Requested temperature (%.1f) out of range (%.1f:%.1f)"
                        % (degrees, self.min_temp, self.max_temp))
        with self.lock:
            self.target_temp = degrees
    def get_temp(self, eventtime):
        print_time = self.mcu_pwm.get_mcu().estimated_print_time(eventtime) - 5.
        with self.lock:
            if self.last_temp_time < print_time:
                return 0., self.target_temp
            return self.smoothed_temp, self.target_temp
    def check_busy(self, eventtime):
        with self.lock:
            return self.control.check_busy(
                eventtime, self.smoothed_temp, self.target_temp)
    def set_control(self, control):
        with self.lock:
            old_control = self.control
            self.control = control
            self.target_temp = 0.
        return old_control
    def alter_target(self, target_temp):
        if target_temp:
            target_temp = max(self.min_temp, min(self.max_temp, target_temp))
        self.target_temp = target_temp
    def stats(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            last_temp = self.last_temp
            last_pwm_value = self.last_pwm_value
        is_active = target_temp or last_temp > 50.
        return is_active, '%s: target=%.0f temp=%.1f pwm=%.3f' % (
            self.name, target_temp, last_temp, last_pwm_value)
    def get_status(self, eventtime):
        with self.lock:
            target_temp = self.target_temp
            smoothed_temp = self.smoothed_temp
        return {'temperature': smoothed_temp, 'target': target_temp}
    cmd_SET_HEATER_TEMPERATURE_help = "Sets a heater temperature"
    def cmd_SET_HEATER_TEMPERATURE(self, params):
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        temp = self.gcode.get_float('TARGET', params, 0.)
        self.set_temp(print_time, temp)


######################################################################
# Bang-bang control algo
######################################################################

class ControlBangBang:
    def __init__(self, heater, config):
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.max_delta = config.getfloat('max_delta', 2.0, above=0.)
        self.heating = False
    def temperature_update(self, read_time, temp, target_temp):
        if self.heating and temp >= target_temp+self.max_delta:
            self.heating = False
        elif not self.heating and temp <= target_temp-self.max_delta:
            self.heating = True
        if self.heating:
            self.heater.set_pwm(read_time, self.heater_max_power)
        else:
            self.heater.set_pwm(read_time, 0.)
    def check_busy(self, eventtime, smoothed_temp, target_temp):
        return smoothed_temp < target_temp-self.max_delta


######################################################################
# Proportional Integral Derivative (PID) control algo
######################################################################

PID_SETTLE_DELTA = 1.
PID_SETTLE_SLOPE = .1

class ControlPID:
    def __init__(self, heater, config):
        self.heater = heater
        self.heater_max_power = heater.get_max_power()
        self.printer = config.get_printer()
        self.Kp_nofan = config.getfloat('pid_Kp') / PID_PARAM_BASE
        self.Ki_nofan = config.getfloat('pid_Ki') / PID_PARAM_BASE
        self.Kd_nofan = config.getfloat('pid_Kd') / PID_PARAM_BASE
        self.fan_lerp = not config.getfloat('pid_Kp_fan', None) is None
        if self.fan_lerp:
            self.Kp_fan = config.getfloat('pid_Kp_fan') / PID_PARAM_BASE
            self.Ki_fan = config.getfloat('pid_Ki_fan') / PID_PARAM_BASE
            self.Kd_fan = config.getfloat('pid_Kd_fan') / PID_PARAM_BASE
        self.min_deriv_time = heater.get_smooth_time()
        self.imax = config.getfloat('pid_integral_max', self.heater_max_power,
                               minval=0.)
        self.prev_temp = AMBIENT_TEMP
        self.prev_temp_time = 0.
        self.prev_temp_deriv = 0.
        self.prev_temp_integ = 0.

    def current_pid_params(self):
        # The presence of a fan blowing air past the nozzle changes the
        # thermal behavior of the heater. We deal with this by having two
        # sets of PID values, one calibrated with, and one w/out
        # fan. We simply interpolate between these based on current fan strength.
        # Ideally, we'd find out how the ideal parameters vary based on airflow
        # (it's probably not linear), but this is better than nothing
        if not self.fan_lerp:
            return self.Kp_nofan, self.Ki_nofan, self.Kd_nofan
        # linear interpolation factor
        lfB = self._printer.lookup_object('fan').get_status(None)['speed']
        lfA = 1 - lfB
        return (lfB * self.Kp_fan + lfA * self.Kp_nofan,
                lfB * self.Ki_fan + lfA * self.Ki_nofan,
                lfB * self.Kd_fan + lfA * self.Kd_nofan)

    def temperature_update(self, read_time, temp, target_temp):
        kp, ki, kd = self.current_pid_params()
        time_diff = read_time - self.prev_temp_time
        # Calculate change of temperature
        temp_diff = temp - self.prev_temp
        if time_diff >= self.min_deriv_time:
            temp_deriv = temp_diff / time_diff
        else:
            temp_deriv = (self.prev_temp_deriv * (self.min_deriv_time-time_diff)
                          + temp_diff) / self.min_deriv_time
        # Calculate accumulated temperature "error"
        temp_err = target_temp - temp
        temp_integ = self.prev_temp_integ + temp_err * time_diff
        temp_integ = max(0., min(self.imax / ki, temp_integ))
        # Calculate output
        co = kp*temp_err + ki*temp_integ - kd*temp_deriv
        #logging.debug("pid: %f@%.3f -> diff=%f deriv=%f err=%f integ=%f co=%d",
        #    temp, read_time, temp_diff, temp_deriv, temp_err, temp_integ, co)
        bounded_co = max(0., min(self.heater_max_power, co))
        self.heater.set_pwm(read_time, bounded_co)
        # Store state for next measurement
        self.prev_temp = temp
        self.prev_temp_time = read_time
        self.prev_temp_deriv = temp_deriv
        if co == bounded_co:
            self.prev_temp_integ = temp_integ
    def check_busy(self, eventtime, smoothed_temp, target_temp):
        temp_diff = target_temp - smoothed_temp
        return (abs(temp_diff) > PID_SETTLE_DELTA
                or abs(self.prev_temp_deriv) > PID_SETTLE_SLOPE)


######################################################################
# Sensor and heater lookup
######################################################################

class PrinterHeaters:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.sensor_factories = {}
        self.heaters = {}
        self.gcode_id_to_sensor = {}
        self.printer.register_event_handler("gcode:request_restart",
                                            self.turn_off_all_heaters)
        # Register TURN_OFF_HEATERS command
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command("TURN_OFF_HEATERS", self.cmd_TURN_OFF_HEATERS,
                               desc=self.cmd_TURN_OFF_HEATERS_help)
    def add_sensor_factory(self, sensor_type, sensor_factory):
        self.sensor_factories[sensor_type] = sensor_factory
    def setup_heater(self, config, gcode_id=None):
        heater_name = config.get_name().split()[-1]
        if heater_name == 'extruder':
            heater_name = 'extruder0'
        if heater_name in self.heaters:
            raise config.error("Heater %s already registered" % (heater_name,))
        # Setup sensor
        sensor = self.setup_sensor(config)
        # Create heater
        self.heaters[heater_name] = heater = Heater(config, sensor)
        self.register_sensor(config, heater, gcode_id)
        return heater
    def lookup_heater(self, heater_name):
        if heater_name == 'extruder':
            heater_name = 'extruder0'
        if heater_name not in self.heaters:
            raise self.printer.config_error(
                "Unknown heater '%s'" % (heater_name,))
        return self.heaters[heater_name]
    def setup_sensor(self, config):
        self.printer.try_load_module(config, "thermistor")
        self.printer.try_load_module(config, "adc_temperature")
        self.printer.try_load_module(config, "spi_temperature")
        sensor_type = config.get('sensor_type')
        if sensor_type not in self.sensor_factories:
            raise self.printer.config_error(
                "Unknown temperature sensor '%s'" % (sensor_type,))
        return self.sensor_factories[sensor_type](config)
    def get_gcode_sensors(self):
        return self.gcode_id_to_sensor.items()
    def register_sensor(self, config, psensor, gcode_id=None):
        if gcode_id is None:
            gcode_id = config.get('gcode_id', None)
            if gcode_id is None:
                return
        if gcode_id in self.gcode_id_to_sensor:
            raise self.printer.config_error(
                "G-Code sensor id %s already registered" % (gcode_id,))
        self.gcode_id_to_sensor[gcode_id] = psensor
    def turn_off_all_heaters(self, print_time):
        for heater in self.heaters.values():
            heater.set_temp(print_time, 0.)
    cmd_TURN_OFF_HEATERS_help = "Turn off all heaters"
    def cmd_TURN_OFF_HEATERS(self, params):
        print_time = self.printer.lookup_object('toolhead').get_last_move_time()
        self.turn_off_all_heaters(print_time)

def add_printer_objects(config):
    config.get_printer().add_object('heater', PrinterHeaters(config))
