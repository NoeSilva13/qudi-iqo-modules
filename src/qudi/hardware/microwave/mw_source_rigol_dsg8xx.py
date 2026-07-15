# -*- coding: utf-8 -*-

"""
This file contains the qudi hardware module to control a Rigol DSG800 series RF signal generator
(tested with a DSG836) as a microwave source for ODMR/pulsed measurements.

Only externally-triggered equidistant frequency sweeps ("Step Sweep" in Rigol terminology) are
supported for scanning, since the DSG800 "List Sweep" mode requires loading a pre-formatted CSV
file from a USB flash drive and cannot be programmed with arbitrary frequency lists over SCPI.

Copyright (c) 2024, the qudi developers. See the AUTHORS.md file at the top-level directory of
this distribution and on <https://github.com/Ulm-IQO/qudi-iqo-modules/>.

This file is part of qudi.

Qudi is free software: you can redistribute it and/or modify it under the terms of
the GNU Lesser General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.

Qudi is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License along with qudi.
If not, see <https://www.gnu.org/licenses/>.
"""

try:
    import pyvisa as visa
except ImportError:
    import visa

from qudi.util.mutex import Mutex
from qudi.core.configoption import ConfigOption
from qudi.interface.microwave_interface import MicrowaveInterface, MicrowaveConstraints
from qudi.util.enums import SamplingOutputMode


class MicrowaveRigolDsg8xx(MicrowaveInterface):
    """ Hardware control class for a Rigol DSG800 series RF signal generator (e.g. DSG836).

    Only CW and externally-triggered equidistant sweep ("Step Sweep") are supported. Each
    rising (or falling) edge on the rear panel [TRIGGER IN] connector advances the sweep by one
    point; wire the same clock/trigger signal that also gates your counting hardware (e.g. a
    Swabian TimeTagger CountBetweenMarkers measurement) to this connector for synchronized ODMR.

    Example config for copy-paste:

    mw_source_rigol:
        module.Class: 'microwave.mw_source_rigol_dsg8xx.MicrowaveRigolDsg8xx'
        options:
            visa_address: 'USB0::0x1AB1::0x0645::DSG8xxxxxxxxxxx::INSTR'
            comm_timeout: 10  # in seconds
            rising_edge_trigger: True  # optional, polarity of the external trigger input
            frequency_min: 9e3  # optional, in Hz
            frequency_max: 3e9  # optional, in Hz
            power_min: -110  # optional, in dBm
            power_max: 20  # optional, in dBm
    """

    _visa_address = ConfigOption('visa_address', missing='error')
    _comm_timeout = ConfigOption('comm_timeout', default=10, missing='warn')
    _rising_edge_trigger = ConfigOption('rising_edge_trigger', default=True, missing='info')
    _config_freq_min = ConfigOption('frequency_min', default=9e3)
    _config_freq_max = ConfigOption('frequency_max', default=3e9)
    _config_power_min = ConfigOption('power_min', default=-110.0)
    _config_power_max = ConfigOption('power_max', default=20.0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._thread_lock = Mutex()
        self._rm = None
        self._device = None
        self._model = ''
        self._constraints = None

        self._is_scanning = False
        self._scan_power = -20.0
        self._scan_mode = SamplingOutputMode.EQUIDISTANT_SWEEP
        self._scan_frequencies = None
        self._scan_sample_rate = 0.0

    def on_activate(self):
        """ Initialisation performed during activation of the module. """
        self._rm = visa.ResourceManager()
        self._device = self._rm.open_resource(
            self._visa_address, timeout=int(self._comm_timeout * 1000)
        )
        # DSG800 firmware silently ignores :SWE:STAT <arg> when the SCPI line is
        # terminated with the PyVISA default "\r\n". Use LF-only termination.
        self._device.write_termination = '\n'
        self._device.read_termination = '\n'
        idn = self._device.query('*IDN?').strip()
        try:
            self._model = idn.split(',')[1].strip()
        except IndexError:
            self._model = idn
            self.log.warning(f'Unable to parse model from *IDN? response "{idn}".')

        self._constraints = MicrowaveConstraints(
            power_limits=(float(self._config_power_min), float(self._config_power_max)),
            frequency_limits=(float(self._config_freq_min), float(self._config_freq_max)),
            scan_size_limits=(2, 65535),  # DSG800 [:SOURce]:SWEep:STEP:POINts range
            sample_rate_limits=(0.1, 1e6),  # hint only; actual rate set by the external trigger
            scan_modes=(SamplingOutputMode.EQUIDISTANT_SWEEP,)
        )

        # Make sure we start from a known, safe state: RF off, sweep off.
        self._device.write(':OUTP OFF')
        self._device.write(':SWE:STAT OFF')

        self._is_scanning = False
        self._scan_power = self._constraints.power.default
        self._scan_frequencies = None
        self._scan_mode = SamplingOutputMode.EQUIDISTANT_SWEEP
        self._scan_sample_rate = self._constraints.max_sample_rate

        slope = 'POS' if self._rising_edge_trigger else 'NEG'
        self._device.write(f':INP:TRIG:SLOP {slope}')
        self._device.write(':SWE:SWE:TRIG:TYPE AUTO')
        self._device.write(':SWE:POIN:TRIG:TYPE EXT')
        self._device.write(':SWE:TYPE STEP')
        self._device.write(':SWE:STEP:SPAC LIN')
        self._device.write(':SWE:DIR FWD')
        self._device.write(':SWE:MODE CONT')

        self.log.info(f'Connected to Rigol RF source model "{self._model}".')

    def on_deactivate(self):
        """ Cleanup performed during deactivation of the module. """
        if self._device is not None:
            try:
                self._device.write(':OUTP OFF')
                self._device.write(':SWE:STAT OFF')
            finally:
                self._device.close()
        if self._rm is not None:
            self._rm.close()
        self._device = None
        self._rm = None

    @property
    def constraints(self):
        return self._constraints

    @property
    def is_scanning(self):
        with self._thread_lock:
            return self._is_scanning

    @property
    def cw_power(self):
        with self._thread_lock:
            return float(self._device.query(':LEV?'))

    @property
    def cw_frequency(self):
        with self._thread_lock:
            return float(self._device.query(':FREQ?'))

    @property
    def scan_power(self):
        with self._thread_lock:
            return self._scan_power

    @property
    def scan_frequencies(self):
        with self._thread_lock:
            return self._scan_frequencies

    @property
    def scan_mode(self):
        with self._thread_lock:
            return self._scan_mode

    @property
    def scan_sample_rate(self):
        with self._thread_lock:
            return self._scan_sample_rate

    def off(self):
        """ Switches off any microwave output (both scan and CW). """
        with self._thread_lock:
            if self.module_state() != 'idle':
                self._device.write(':OUTP OFF')
                self._device.write(':SWE:STAT OFF')
                self._is_scanning = False
                self.module_state.unlock()

    def set_cw(self, frequency, power):
        """ Configure the CW microwave output. Does not start physical signal output. """
        with self._thread_lock:
            if self.module_state() != 'idle':
                raise RuntimeError('Unable to set CW parameters. Microwave output active.')
            self._assert_cw_parameters_args(frequency, power)
            self._device.write(':SWE:STAT OFF')
            self._device.write(f':FREQ {frequency:.9f}Hz')
            self._device.write(f':LEV {power:.3f}dBm')

    def cw_on(self):
        """ Switches on preconfigured cw microwave output. """
        with self._thread_lock:
            if self.module_state() != 'idle':
                if not self._is_scanning:
                    return
                raise RuntimeError(
                    'Unable to start CW microwave output. Frequency scanning in progress.'
                )
            self._device.write(':SWE:STAT OFF')
            self._device.write(':OUTP ON')
            self._is_scanning = False
            self.module_state.lock()

    def configure_scan(self, power, frequencies, mode, sample_rate):
        """ Configure a frequency scan (equidistant sweep only). """
        with self._thread_lock:
            if self.module_state() != 'idle':
                raise RuntimeError('Unable to configure frequency scan. Microwave output active.')
            self._assert_scan_configuration_args(power, frequencies, mode, sample_rate)
            if mode != SamplingOutputMode.EQUIDISTANT_SWEEP:
                raise ValueError(
                    'MicrowaveRigolDsg8xx only supports SamplingOutputMode.EQUIDISTANT_SWEEP. '
                    'Arbitrary jump lists would require loading a CSV file via USB and are not '
                    'supported over SCPI.'
                )

            start_freq, stop_freq, num_points = frequencies
            self._scan_power = float(power)
            self._scan_mode = mode
            self._scan_sample_rate = float(sample_rate)
            self._scan_frequencies = (float(start_freq), float(stop_freq), int(num_points))

            self._device.write(f':LEV {power:.3f}dBm')
            self._device.write(':SWE:TYPE STEP')
            self._device.write(':SWE:STEP:SPAC LIN')
            self._device.write(':SWE:DIR FWD')
            self._device.write(f':SWE:STEP:STAR:FREQ {start_freq:.9f}Hz')
            self._device.write(f':SWE:STEP:STOP:FREQ {stop_freq:.9f}Hz')
            self._device.write(f':SWE:STEP:POIN {int(num_points):d}')
            self._device.write(':SWE:STEP:DWEL 20ms')
            self._device.write(':SWE:MODE CONT')
            self._device.write(':SWE:SWE:TRIG:TYPE AUTO')
            self._device.write(':SWE:POIN:TRIG:TYPE EXT')
            slope = 'POS' if self._rising_edge_trigger else 'NEG'
            self._device.write(f':INP:TRIG:SLOP {slope}')
            # Engage the frequency sweep. RF output itself is switched on in start_scan().
            self._device.write(':SWE:STAT FREQ')

    def start_scan(self):
        """ Switches on the preconfigured microwave scanning. """
        with self._thread_lock:
            if self.module_state() != 'idle':
                if self._is_scanning:
                    return
                raise RuntimeError('Unable to start frequency scan. CW microwave output is active.')
            assert self._scan_frequencies is not None, \
                'No scan_frequencies set. Unable to start scan.'

            self._device.write(':SWE:STAT FREQ')
            self._device.write(':SWE:RES:ALL')
            self._device.write(':OUTP ON')
            self._is_scanning = True
            self.module_state.lock()

    def reset_scan(self):
        """ Reset currently running scan and return to start frequency. """
        with self._thread_lock:
            if not self._is_scanning:
                return
            self._device.write(':SWE:RES:ALL')
