# -*- coding: utf-8 -*-

"""
Interfuse for a confocal scanner that uses NI X-series hardware for positioning (analog output
and exported sample clock) and a Swabian Instruments TimeTagger for photon counting using
`CountBetweenMarkers`. The NI sample clock is exported on a PFI line and wired into a TimeTagger
input channel so that each pixel of the scan corresponds to one TimeTagger bin between two
consecutive clock edges.

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

import time
from dataclasses import asdict
from typing import Dict, List, Optional

import numpy as np
from PySide6 import QtCore
from PySide6.QtGui import QGuiApplication

from qudi.core.configoption import ConfigOption
from qudi.core.connector import Connector
from qudi.hardware.interfuse.ni_scanning_probe_interfuse import RawDataContainer
from qudi.interface.finite_sampling_input_interface import FiniteSamplingInputInterface
from qudi.interface.finite_sampling_io_interface import FiniteSamplingIOInterface
from qudi.interface.process_control_interface import ProcessSetpointInterface
from qudi.interface.scanning_probe_interface import (
    BackScanCapability,
    CoordinateTransformMixin,
    ScanConstraints,
    ScanData,
    ScanSettings,
    ScannerAxis,
    ScannerChannel,
    ScanningProbeInterface,
)
from qudi.util.constraints import ScalarConstraint
from qudi.util.enums import SamplingOutputMode
from qudi.util.helpers import in_range
from qudi.util.mutex import Mutex


class NiTimeTaggerScanningProbeInterfuseBare(ScanningProbeInterface):
    """ Combines NI X-series finite sampling IO (analog output + exported clock), an NI analog
    output module for software-timed cursor moves and a Swabian TimeTagger
    `CountBetweenMarkers` counter into a scanning probe.

    The NI device drives the scanner voltages with hardware timing and exports its sample clock
    on a PFI line via the `sample_clock_output` option of `NIXSeriesFiniteSamplingIO`. The
    TimeTagger counts photons between consecutive edges of that clock, providing one count value
    per pixel that is fully synchronised with the scanner position.

    Example config for copy-paste:

    ni_timetagger_scanner:
        module.Class: 'interfuse.ni_timetagger_scanning_probe_interfuse.NiTimeTaggerScanningProbeInterfuse'
        # to use without tilt correction
        # module.Class: 'interfuse.ni_timetagger_scanning_probe_interfuse.NiTimeTaggerScanningProbeInterfuseBare'
        connect:
            scan_output: 'ni_finite_sampling_io'
            analog_output: 'ni_ao'
            counter: 'timetagger_finite_counter'
        options:
            ni_channel_mapping:
                x: 'ao0'
                y: 'ao1'
                z: 'ao2'
            position_ranges: # in m
                x: [-100e-6, 100e-6]
                y: [0, 200e-6]
                z: [-100e-6, 100e-6]
            frequency_ranges:
                x: [1, 5000]
                y: [1, 5000]
                z: [1, 1000]
            resolution_ranges:
                x: [1, 10000]
                y: [1, 10000]
                z: [2, 1000]
            input_channel_units:
                APD1: 'c/s'
            maximum_move_velocity: 400e-6 # m/s
            default_backward_resolution: 50
    """

    _ni_finite_sampling_io = Connector(name='scan_output', interface=FiniteSamplingIOInterface)
    _ni_ao = Connector(name='analog_output', interface=ProcessSetpointInterface)
    _timetagger = Connector(name='counter', interface=FiniteSamplingInputInterface)

    _ni_channel_mapping: Dict[str, str] = ConfigOption(name='ni_channel_mapping', missing='error')
    _position_ranges: Dict[str, List[float]] = ConfigOption(name='position_ranges', missing='error')
    _frequency_ranges: Dict[str, List[float]] = ConfigOption(name='frequency_ranges', missing='error')
    _resolution_ranges: Dict[str, List[float]] = ConfigOption(name='resolution_ranges', missing='error')
    _input_channel_units: Dict[str, str] = ConfigOption(name='input_channel_units', missing='error')

    __max_move_velocity: float = ConfigOption(name='maximum_move_velocity', default=400e-6)
    __default_backward_resolution: int = ConfigOption(name='default_backward_resolution', default=50)
    # Settling time between arming the TimeTagger and starting the NI sample clock.
    _tt_arm_delay: float = ConfigOption(name='timetagger_arm_delay', default=0.2)

    _threaded = True

    sigNextDataChunk = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._scan_data: Optional[ScanData] = None
        self._back_scan_data: Optional[ScanData] = None
        self.raw_data_container: Optional[RawDataContainer] = None

        self._constraints: Optional[ScanConstraints] = None

        self._target_pos: Dict[str, float] = dict()
        self._stored_target_pos: Dict[str, float] = dict()
        self._start_scan_after_cursor = False
        self._abort_cursor_move = False

        self.__ni_ao_write_timer = None
        self._min_step_interval = 1e-3
        self._scanner_distance_atol = 1e-9

        self._thread_lock_cursor = Mutex()
        self._thread_lock_data = Mutex()

        self.__t_last_follow = None
        self._t_last_move = 0.0
        self._follow_velocity = self.__max_move_velocity

        self.bare_scanner = NiTimeTaggerScanningProbeInterfuseBare

    def on_activate(self):
        # Position/frequency/resolution must agree
        assert set(self._position_ranges) == set(self._frequency_ranges) == set(self._resolution_ranges), \
            'Channels in position_ranges, frequency_ranges and resolution_ranges do not coincide'

        # All scanner axes must have a NI AO channel mapping. Logical input channels (counts) are
        # served by the TimeTagger and must NOT be required in `ni_channel_mapping`.
        assert set(self._position_ranges).issubset(set(self._ni_channel_mapping)), \
            'Each scanner axis must be mapped to an NI AO channel via ni_channel_mapping.'

        # AO channels in the mapping must exist as outputs of the NI finite sampling IO module.
        ni_output_channels = set(
            ch.lower() for ch in self._ni_finite_sampling_io().constraints.output_channel_units
        )
        mapped_ao = set(self._ni_channel_mapping[ax].lower() for ax in self._position_ranges)
        assert mapped_ao.issubset(ni_output_channels), (
            f'Mapped AO channels {mapped_ao} not present in NI finite sampling IO outputs '
            f'{ni_output_channels}.'
        )

        # All declared input channels must exist on the TimeTagger module.
        tt_channels = set(self._timetagger().constraints.channel_names)
        assert set(self._input_channel_units).issubset(tt_channels), (
            f'Input channels {set(self._input_channel_units)} are not all exposed by the '
            f'TimeTagger module (available: {tt_channels}).'
        )

        # Build scanning probe constraints
        axes = list()
        for axis in self._position_ranges:
            position_range = tuple(self._position_ranges[axis])
            resolution_range = tuple(self._resolution_ranges[axis])
            res_default = 50
            if not resolution_range[0] <= res_default <= resolution_range[1]:
                res_default = resolution_range[0]
            frequency_range = tuple(self._frequency_ranges[axis])
            freq_default = 500
            if not frequency_range[0] <= freq_default <= frequency_range[1]:
                freq_default = frequency_range[0]
            max_step = abs(position_range[1] - position_range[0])

            position = ScalarConstraint(default=min(position_range), bounds=position_range)
            resolution = ScalarConstraint(default=res_default, bounds=resolution_range, enforce_int=True)
            frequency = ScalarConstraint(default=freq_default, bounds=frequency_range)
            step = ScalarConstraint(default=0, bounds=(0, max_step))

            axes.append(ScannerAxis(name=axis,
                                    unit='m',
                                    position=position,
                                    step=step,
                                    resolution=resolution,
                                    frequency=frequency))

        channels = [
            ScannerChannel(name=channel, unit=unit, dtype='float64')
            for channel, unit in self._input_channel_units.items()
        ]

        back_scan_capability = (
            BackScanCapability.AVAILABLE | BackScanCapability.RESOLUTION_CONFIGURABLE
        )
        self._constraints = ScanConstraints(axis_objects=tuple(axes),
                                            channel_objects=tuple(channels),
                                            back_scan_capability=back_scan_capability,
                                            has_position_feedback=False,
                                            square_px_only=False)

        self._target_pos = self.bare_scanner.get_position(self)
        self._toggle_ao_setpoint_channels(False)
        self._t_last_move = time.perf_counter()
        self.__init_ao_timer()
        self.__t_last_follow = None

        self.sigNextDataChunk.connect(self._fetch_data_chunk,
                                      QtCore.Qt.ConnectionType.QueuedConnection)

    def on_deactivate(self):
        self._abort_cursor_movement()
        try:
            self._timetagger().stop_buffered_acquisition()
        except Exception:
            self.log.exception('Failed to stop TimeTagger acquisition on deactivate.')
        if self._ni_finite_sampling_io().is_running:
            self._ni_finite_sampling_io().stop_buffered_frame()

    def _toggle_ao_setpoint_channels(self, enable: bool) -> None:
        ni_ao = self._ni_ao()
        for channel in ni_ao.constraints.setpoint_channels:
            ni_ao.set_activity_state(channel, enable)

    @property
    def _ao_setpoint_channels_active(self) -> bool:
        mapped_channels = set(self._ni_channel_mapping.values())
        return all(
            state for ch, state in self._ni_ao().activity_states.items() if ch in mapped_channels
        )

    @property
    def constraints(self) -> ScanConstraints:
        return self._constraints

    def reset(self):
        pass

    @property
    def scan_settings(self) -> Optional[ScanSettings]:
        if self._scan_data:
            return self._scan_data.settings
        return None

    @property
    def back_scan_settings(self) -> Optional[ScanSettings]:
        if self._back_scan_data:
            return self._back_scan_data.settings
        return None

    def configure_scan(self, settings: ScanSettings) -> None:
        if self.is_scan_running:
            raise RuntimeError('Unable to configure scan parameters while scan is running. '
                               'Stop scanning and try again.')

        self.constraints.check_settings(settings)
        self.log.debug('Scan settings fulfill constraints.')

        with self._thread_lock_data:
            settings = self._clip_ranges(settings)
            self._scan_data = ScanData.from_constraints(settings, self._constraints)

            if len(settings.axes) == 1:
                back_resolution = (self.__default_backward_resolution,)
            else:
                back_resolution = (self.__default_backward_resolution, settings.resolution[1])
            back_scan_settings = ScanSettings(
                channels=settings.channels,
                axes=settings.axes,
                range=settings.range,
                resolution=back_resolution,
                frequency=settings.frequency,
            )
            self._back_scan_data = ScanData.from_constraints(back_scan_settings, self._constraints)

            self.raw_data_container = RawDataContainer(
                settings.channels,
                settings.resolution[1] if settings.scan_dimension == 2 else 1,
                settings.resolution[0],
                back_scan_settings.resolution[0],
            )

        self._configure_hw(settings, back_scan_settings)

    def configure_back_scan(self, settings: ScanSettings) -> None:
        if self.is_scan_running:
            raise RuntimeError('Unable to configure scan parameters while scan is running. '
                               'Stop scanning and try again.')

        forward_settings = self.scan_settings
        self.constraints.check_back_scan_settings(settings, forward_settings)
        self.log.debug('Back scan settings fulfill constraints.')

        with self._thread_lock_data:
            self._back_scan_data = ScanData.from_constraints(settings, self._constraints)
            self.raw_data_container = RawDataContainer(
                forward_settings.channels,
                forward_settings.resolution[1] if forward_settings.scan_dimension == 2 else 1,
                forward_settings.resolution[0],
                settings.resolution[0],
            )

        self._configure_hw(forward_settings, settings)

    def _configure_hw(self, forward_settings: ScanSettings, back_scan_settings: ScanSettings) -> None:
        """ Push the AO frame to NI and configure the TimeTagger for the same total frame size."""
        ni = self._ni_finite_sampling_io()
        ni.set_sample_rate(forward_settings.frequency)
        ni.set_active_channels(
            input_channels=(),   # NI handles AO + clock only; counting is done by TimeTagger
            output_channels=(self._ni_channel_mapping[ax] for ax in self.constraints.axes.keys()),
        )
        ni.set_output_mode(SamplingOutputMode.JUMP_LIST)

        ni_scan_dict = self._init_ni_scan_arrays(forward_settings, back_scan_settings)
        ni.set_frame_data(ni_scan_dict)

        n_lines = forward_settings.resolution[1] if forward_settings.scan_dimension == 2 else 1
        frame_total = n_lines * (forward_settings.resolution[0] + back_scan_settings.resolution[0])

        tt = self._timetagger()
        tt.set_sample_rate(forward_settings.frequency)
        tt.set_active_channels(tuple(self._input_channel_units.keys()))
        tt.set_frame_size(frame_total)

    def move_absolute(self, position, velocity=None, blocking=False):
        if self.is_scan_running:
            self.log.error('Cannot move the scanner while a scan is running.')
            return self.bare_scanner.get_target(self)

        if not set(position).issubset(self.constraints.axes):
            self.log.error('Invalid axes name in position')
            return self.bare_scanner.get_target(self)

        try:
            self._prepare_movement(position, velocity=velocity)
            self.__start_ao_write_timer()
            if blocking:
                self.__wait_on_move_done()
            self._t_last_move = time.perf_counter()
            return self.bare_scanner.get_target(self)
        except Exception:
            self.log.exception("Couldn't move:")

    def __wait_on_move_done(self):
        try:
            t_start = time.perf_counter()
            while self.is_move_running:
                self.log.debug(
                    f"Waiting for move done: {self.is_move_running}, "
                    f"{1e3 * (time.perf_counter() - t_start)} ms"
                )
                QGuiApplication.processEvents()
                time.sleep(self._min_step_interval)
        except Exception:
            self.log.exception("")

    def move_relative(self, distance, velocity=None, blocking=False):
        current_position = self.bare_scanner.get_position(self)
        end_pos = {ax: current_position[ax] + distance[ax] for ax in distance}
        self.move_absolute(end_pos, velocity=velocity, blocking=blocking)
        return end_pos

    def get_target(self):
        if self.is_scan_running:
            return self._stored_target_pos
        return self._target_pos

    def get_position(self):
        with self._thread_lock_cursor:
            if not self._ao_setpoint_channels_active:
                self._toggle_ao_setpoint_channels(True)
            pos = self._voltage_dict_to_position_dict(self._ni_ao().setpoints)
            return pos

    def start_scan(self):
        try:
            if self.thread() is not QtCore.QThread.currentThread():
                QtCore.QMetaObject.invokeMethod(
                    self, '_start_scan',
                    QtCore.Qt.ConnectionType.BlockingQueuedConnection
                )
            else:
                self._start_scan()
        except Exception:
            self.log.exception("")

    @QtCore.Slot()
    def _start_scan(self):
        try:
            if self._scan_data is None:
                self.log.error('Scan Data is None. Scan settings must be configured first.')

            if self.is_scan_running:
                self.log.error('Cannot start a scan while scanning probe is already running')

            with self._thread_lock_data:
                self._scan_data.new_scan()
                self._back_scan_data.new_scan()
                self._stored_target_pos = self.bare_scanner.get_target(self).copy()
                self.log.debug(f"Target pos at scan start: {self._stored_target_pos}")
                self._scan_data.scanner_target_at_start = self._stored_target_pos
                self._back_scan_data.scanner_target_at_start = self._stored_target_pos

            self.module_state.lock()

            first_scan_position = {
                ax: pos[0] for ax, pos
                in zip(self.scan_settings.axes, self.scan_settings.range)
            }
            self._move_to_and_start_scan(first_scan_position)

        except Exception as e:
            self.module_state.unlock()
            self.log.exception("Starting scan failed.", exc_info=e)

    def stop_scan(self):
        if self.thread() is not QtCore.QThread.currentThread():
            QtCore.QMetaObject.invokeMethod(
                self, '_stop_scan',
                QtCore.Qt.ConnectionType.BlockingQueuedConnection
            )
        else:
            self._stop_scan()

    @QtCore.Slot()
    def _stop_scan(self):
        if not self.is_scan_running:
            self.log.error('No scan in progress. Cannot stop scan.')

        self._start_scan_after_cursor = False
        if self._ao_setpoint_channels_active:
            self._abort_cursor_movement()

        try:
            self._timetagger().stop_buffered_acquisition()
        except Exception:
            self.log.exception('Failed to stop TimeTagger acquisition.')

        if self._ni_finite_sampling_io().is_running:
            self._ni_finite_sampling_io().stop_buffered_frame()

        self.module_state.unlock()

        self.log.debug(f"Finished scan, move to stored target: {self._stored_target_pos}")
        self.bare_scanner.move_absolute(self, self._stored_target_pos)
        self._stored_target_pos = dict()

    def get_scan_data(self) -> Optional[ScanData]:
        if self._scan_data is None:
            return None
        with self._thread_lock_data:
            return self._scan_data.copy()

    def get_back_scan_data(self) -> Optional[ScanData]:
        if self._scan_data is None:
            return None
        with self._thread_lock_data:
            return self._back_scan_data.copy()

    def emergency_stop(self):
        pass

    @property
    def is_scan_running(self) -> bool:
        return self.module_state() == 'locked'

    @property
    def is_move_running(self) -> bool:
        with self._thread_lock_cursor:
            return self.__t_last_follow is not None

    def _check_scan_end_reached(self) -> bool:
        return self.raw_data_container.is_full

    def _fetch_data_chunk(self):
        try:
            chunk_size = 10
            tt = self._timetagger()
            try:
                if tt.samples_in_buffer < chunk_size:
                    samples_dict = tt.get_buffered_samples(chunk_size)
                else:
                    samples_dict = tt.get_buffered_samples()
            except ValueError:
                samples_dict = tt.get_buffered_samples()

            new_data = {key: samples for key, samples in samples_dict.items()
                        if key in self._input_channel_units}

            do_stop = False
            with self._thread_lock_data:
                if new_data:
                    self.raw_data_container.fill_container(new_data)
                    self._scan_data.data = self.raw_data_container.forwards_data()
                    self._back_scan_data.data = self.raw_data_container.backwards_data()

                if self._check_scan_end_reached():
                    do_stop = True
                elif not self.is_scan_running:
                    return
                else:
                    self.sigNextDataChunk.emit()

            if do_stop:
                self.stop_scan()

        except Exception as e:
            self.log.error("Error while fetching data chunk.", exc_info=e)
            self.stop_scan()

    def _position_to_voltage(self, axis, positions):
        ni_channel = self._ni_channel_mapping[axis]
        voltage_range = self._ni_finite_sampling_io().constraints.output_channel_limits[ni_channel]
        position_range = self.constraints.axes[axis].position.bounds

        slope = np.diff(voltage_range) / np.diff(position_range)
        intercept = voltage_range[1] - position_range[1] * slope

        converted = np.clip(positions * slope + intercept, min(voltage_range), max(voltage_range))

        try:
            voltage_data = converted.item()
        except ValueError:
            voltage_data = converted

        return voltage_data

    def _pos_dict_to_vec(self, position):
        pos_list = [el[1] for el in sorted(position.items())]
        return np.asarray(pos_list)

    def _pos_vec_to_dict(self, position_vec):
        if isinstance(position_vec, dict):
            raise ValueError("Position can't be provided as dict.")
        axes = sorted(self.constraints.axes.keys())
        return {axes[idx]: pos for idx, pos in enumerate(position_vec)}

    def _voltage_dict_to_position_dict(self, voltages):
        reverse_routing = {val.lower(): key for key, val in self._ni_channel_mapping.items()}

        positions_data = dict()
        for ni_channel in voltages:
            try:
                axis = reverse_routing[ni_channel]
                voltage_range = self._ni_finite_sampling_io().constraints.output_channel_limits[ni_channel]
                position_range = self.constraints.axes[axis].position.bounds

                slope = np.diff(position_range) / np.diff(voltage_range)
                intercept = position_range[1] - voltage_range[1] * slope

                converted = voltages[ni_channel] * slope + intercept
                converted = np.around(converted, 10)
            except KeyError:
                continue

            try:
                positions_data[axis] = converted.item()
            except ValueError:
                positions_data[axis] = converted

        return positions_data

    def _get_scan_lines(self, settings: ScanSettings, back_settings: ScanSettings) -> Dict[str, np.ndarray]:
        if settings.scan_dimension == 1:
            axis = settings.axes[0]

            horizontal = np.linspace(settings.range[0][0], settings.range[0][1],
                                     settings.resolution[0])
            horizontal_return_line = np.linspace(settings.range[0][1], settings.range[0][0],
                                                 back_settings.resolution[0])

            horizontal_single_line = np.concatenate((horizontal, horizontal_return_line))
            coord_dict = {axis: horizontal_single_line}

        elif settings.scan_dimension == 2:
            horizontal_resolution = settings.resolution[0]
            horizontal_back_resolution = back_settings.resolution[0]
            vertical_resolution = settings.resolution[1]

            horizontal_axis = settings.axes[0]
            horizontal = np.linspace(settings.range[0][0], settings.range[0][1],
                                     horizontal_resolution)
            horizontal_return_line = np.linspace(settings.range[0][1], settings.range[0][0],
                                                 horizontal_back_resolution)
            horizontal_single_line = np.concatenate((horizontal, horizontal_return_line))
            horizontal_scan_array = np.tile(horizontal_single_line, vertical_resolution)

            vertical_axis = settings.axes[1]
            vertical = np.linspace(settings.range[1][0], settings.range[1][1],
                                   vertical_resolution)

            vertical_lines = np.repeat(vertical.reshape(vertical_resolution, 1),
                                       horizontal_resolution, axis=1)
            vertical_return_lines = np.linspace(vertical[:-1], vertical[1:],
                                                horizontal_back_resolution).T
            vertical_return_lines = np.concatenate((
                vertical_return_lines,
                np.ones((1, horizontal_back_resolution)) * vertical[-1]
            ))

            vertical_scan_array = np.concatenate(
                (vertical_lines, vertical_return_lines), axis=1
            ).ravel()

            coord_dict = {horizontal_axis: horizontal_scan_array,
                          vertical_axis: vertical_scan_array}
        else:
            raise ValueError(f"Not supported scan dimension: {settings.scan_dimension}")

        return self._expand_coordinate(coord_dict)

    def _init_scan_grid(self, settings: ScanSettings, back_settings: ScanSettings) -> Dict[str, np.ndarray]:
        return self._get_scan_lines(settings, back_settings)

    def _check_scan_grid(self, scan_coords):
        for ax, coords in scan_coords.items():
            position_min = self.constraints.axes[ax].position.minimum
            position_max = self.constraints.axes[ax].position.maximum
            out_of_range = any(coords < position_min) or any(coords > position_max)
            if out_of_range:
                raise ValueError(f"Scan axis {ax} out of range [{position_min}, {position_max}]")

    def _clip_ranges(self, settings: ScanSettings):
        valid_scan_grid = False
        i_trial, n_max_trials = 0, 25

        while not valid_scan_grid and i_trial < n_max_trials:
            ranges = settings.range
            if i_trial > 0:
                ranges = self._shrink_scan_ranges(ranges)
            settings_dict = asdict(settings)
            settings_dict['range'] = ranges
            settings = ScanSettings.from_dict(settings_dict)

            try:
                self._init_ni_scan_arrays(settings, settings)
                valid_scan_grid = True
            except ValueError:
                valid_scan_grid = False
            i_trial += 1

        if not valid_scan_grid:
            raise ValueError("Couldn't create scan grid.")
        if i_trial > 1:
            self.log.warning(f"Adapted out-of-bounds scan range to {ranges}")
        return settings

    @staticmethod
    def _shrink_scan_ranges(ranges, factor=0.01):
        lengths = [stop - start for (start, stop) in ranges]
        return [
            (start + factor * lengths[idx], stop - factor * lengths[idx])
            for idx, (start, stop) in enumerate(ranges)
        ]

    def _init_ni_scan_arrays(self, settings: ScanSettings, back_settings: ScanSettings) -> Dict[str, np.ndarray]:
        scan_coords = self._init_scan_grid(settings, back_settings)
        self._check_scan_grid(scan_coords)
        scan_voltages = {
            self._ni_channel_mapping[ax]: self._position_to_voltage(ax, val)
            for ax, val in scan_coords.items()
        }
        return scan_voltages

    def __ao_cursor_write_loop(self):
        t_start = time.perf_counter()
        try:
            current_pos_vec = self._pos_dict_to_vec(self.bare_scanner.get_position(self))

            with self._thread_lock_cursor:
                stop_loop = self._abort_cursor_move

                target_pos_vec = self._pos_dict_to_vec(self._target_pos)
                connecting_vec = target_pos_vec - current_pos_vec
                distance_to_target = np.linalg.norm(connecting_vec)

                if distance_to_target < self._scanner_distance_atol:
                    stop_loop = True

                if not stop_loop:
                    if not self.__t_last_follow:
                        self.__t_last_follow = time.perf_counter()

                    delta_t = t_start - self.__t_last_follow
                    self.__t_last_follow = t_start

                    max_step_distance = delta_t * self._follow_velocity

                    if max_step_distance < distance_to_target:
                        direction_vec = connecting_vec / distance_to_target
                        new_pos_vec = current_pos_vec + max_step_distance * direction_vec
                    else:
                        new_pos_vec = target_pos_vec

                    new_pos = self._pos_vec_to_dict(new_pos_vec)
                    new_voltage = {
                        self._ni_channel_mapping[ax]: self._position_to_voltage(ax, pos)
                        for ax, pos in new_pos.items()
                    }

                    self._ni_ao().setpoints = new_voltage

                    t_overhead = time.perf_counter() - t_start
                    self.__ni_ao_write_timer.start(
                        int(round(1000 * max(0, self._min_step_interval - t_overhead)))
                    )

            if stop_loop:
                self._abort_cursor_movement()
                if self._start_scan_after_cursor:
                    self._start_hw_timed_scan()
        except Exception:
            self.log.exception("Error in ao write loop:")

    def _start_hw_timed_scan(self):
        try:
            # Arm the TimeTagger BEFORE the NI clock starts emitting pulses, so that the very
            # first clock edge opens bin 0 of the `CountBetweenMarkers` measurement. We then wait
            # briefly so that the TimeTagger is guaranteed to be running before the NI fires its
            # first edge - without this, the first one or two bins occasionally come back empty
            # because the TimeTagger has not finished arming.
            self._timetagger().start_buffered_acquisition()
            time.sleep(self._tt_arm_delay)
            self._ni_finite_sampling_io().start_buffered_frame()
            self.sigNextDataChunk.emit()
        except Exception as e:
            self.log.error(f'Could not start frame due to {str(e)}')
            try:
                self._timetagger().stop_buffered_acquisition()
            except Exception:
                pass
            self.module_state.unlock()

        self._start_scan_after_cursor = False

    def _abort_cursor_movement(self):
        self._target_pos = self.bare_scanner.get_position(self)
        with self._thread_lock_cursor:
            self._abort_cursor_move = True
            self.__t_last_follow = None
            self._toggle_ao_setpoint_channels(False)

    def _move_to_and_start_scan(self, position):
        self._prepare_movement(position)
        self._start_scan_after_cursor = True
        self.__start_ao_write_timer()

    def _prepare_movement(self, position, velocity=None):
        with self._thread_lock_cursor:
            self._abort_cursor_move = False
            if not self._ao_setpoint_channels_active:
                self._toggle_ao_setpoint_channels(True)

            constr = self.constraints
            for axis, pos in position.items():
                in_range_flag, _ = in_range(pos, *constr.axes[axis].position.bounds)
                if not in_range_flag:
                    position[axis] = float(constr.axes[axis].position.clip(position[axis]))
                    self.log.warning(
                        f'Position {pos} out of range {constr.axes[axis].position.bounds} '
                        f'for axis {axis}. Value clipped to {position[axis]}'
                    )
                self._target_pos[axis] = position[axis]

            if velocity is None:
                velocity = self.__max_move_velocity
            v_in_range, velocity = in_range(velocity, 0, self.__max_move_velocity)
            if not v_in_range:
                self.log.warning(
                    f'Requested velocity is exceeding the maximum velocity of '
                    f'{self.__max_move_velocity} m/s. Move will be done at maximum velocity'
                )
            self._follow_velocity = velocity

    def __init_ao_timer(self):
        self.__ni_ao_write_timer = QtCore.QTimer(parent=self)
        self.__ni_ao_write_timer.setSingleShot(True)
        self.__ni_ao_write_timer.timeout.connect(
            self.__ao_cursor_write_loop, QtCore.Qt.ConnectionType.QueuedConnection
        )
        self.__ni_ao_write_timer.setInterval(1e3 * self._min_step_interval)

    def __start_ao_write_timer(self):
        try:
            if not self.is_move_running:
                if self.thread() is not QtCore.QThread.currentThread():
                    QtCore.QMetaObject.invokeMethod(
                        self.__ni_ao_write_timer, 'start',
                        QtCore.Qt.ConnectionType.BlockingQueuedConnection
                    )
                else:
                    self.__ni_ao_write_timer.start()
        except Exception:
            self.log.exception("")


class NiTimeTaggerScanningProbeInterfuse(CoordinateTransformMixin,
                                         NiTimeTaggerScanningProbeInterfuseBare):
    """ Tilt-correction aware variant of `NiTimeTaggerScanningProbeInterfuseBare`. """

    def _init_scan_grid(self, settings: ScanSettings, back_settings: ScanSettings) -> Dict[str, np.ndarray]:
        return self.coordinate_transform(
            super()._init_scan_grid(settings, back_settings), inverse=False
        )

    @QtCore.Slot()
    def _start_scan(self):
        super()._start_scan()

    @QtCore.Slot()
    def _stop_scan(self):
        super()._stop_scan()
