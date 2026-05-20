# Introduction

The scanning toolchain is designed to be fully configurable with respect to multiple signal inputs (eg. APD counts, analogue inputs) and an arbitrary scanning axes configuration.
To this end, its written in a very modular way.
A typical working toolchain consists out of the following qudi modules:

logic:
- [scanning_data_logic](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/logic/scanning_data_logic.py#L50)
- [scanning_probe_logic](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/logic/scanning_probe_logic.py#L33)
- [scanning_optimize_logic](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/logic/scanning_optimize_logic.py#L33)

hardware (here NI X-series):
- [analog_output](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/hardware/ni_x_series/ni_x_series_analog_output.py#L39)
- [finite_sampling_input](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/hardware/ni_x_series/ni_x_series_finite_sampling_input.py#L46)
- [finite_sampling_io](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/hardware/ni_x_series/ni_x_series_finite_sampling_io.py#L50)
- ([in_streamer](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/hardware/ni_x_series/ni_x_series_in_streamer.py#L45), optional)

gui:
- [scannergui](https://github.com/Ulm-IQO/qudi-iqo-modules/blob/main/src/qudi/gui/scanning/scannergui.py#L83)

# Example config

These modules need to be configured and connected in your qudi config file.
We here provide an examplary config for a toolchain based on a NI X-series scanner with analogue output and digital (APD TTL) input.
Note: This readme file might not be up-to-date with the most recent development. We advice to check the examplary config present in the 
docstring of every module's python file. In the list above, a direct link for every module is provided:


    gui:
        scanner_gui:
          module.Class: 'scanning.scannergui.ScannerGui'
          options:  
              image_axes_padding: 0.02
              default_position_unit_prefix: null  # optional, use unit prefix characters, e.g. 'u' or 'n'
              optimizer_plot_dimensions: [2,1]
          connect:
              scanning_logic: scanning_probe_logic
              data_logic: scanning_data_logic
              optimize_logic: scanning_optimize_logic
    
    
    logic:
        scanning_probe_logic:
            module.Class: 'scanning_probe_logic.ScanningProbeLogic'
            options:  
                max_history_length: 20
                max_scan_update_interval: 2
                position_update_interval: 1
            connect:
                scanner: ni_scanner

        scanning_data_logic:
            module.Class: 'scanning_data_logic.ScanningDataLogic'
            options:  
                max_history_length: 20
            connect:
                scan_logic: scanning_probe_logic

        scanning_optimize_logic:
            module.Class: 'scanning_optimize_logic.ScanningOptimizeLogic'
            connect:
                scan_logic: scanning_probe_logic

    
    hardware:
        ni_scanner:
            module.Class: 'interfuse.ni_scanning_probe_interfuse.NiScanningProbeInterfuse'
            connect:
                scan_hardware: 'ni_io'
                analog_output: 'ni_ao'
            options:  
                ni_channel_mapping:
                    x: 'ao0'
                    y: 'ao1'
                    z: 'ao2'
                    #a: 'ao3'
                    APD1: 'PFI8'
                    #APD2: 'PFI9'
                    #AI0: 'ai0'
                    #APD3: 'PFI10'
                position_ranges: # in m
                    x: [0, 200e-6]
                    y: [0, 200e-6]
                    z: [-100e-6, 100e-6]
                frequency_ranges:
                    x: [1, 5000]
                    y: [1, 5000]
                    z: [1, 1000]
                resolution_ranges:
                    x: [1, 10000]
                    y: [1, 10000]
                    z: [1, 10000]
                input_channel_units:
                    APD1: 'c/s'
                    #AI0: 'V'
                    #APD2: 'c/s'
                    #APD3: 'c/s'
                backwards_line_resolution: 50 # optional
                maximum_move_velocity: 400e-6 #m/s
        
        # dummy, if no real hardware available
        scanner_dummy:
            module.Class: 'dummy.scanning_probe_dummy.ScanningProbeDummy'
            options:
                position_ranges:
                    'x': [0, 200e-6]
                    'y': [0, 200e-6]
                    'z': [-100e-6, 100e-6]
                frequency_ranges:
                    'x': [0, 10000]
                    'y': [0, 10000]
                    'z': [0, 5000]
                resolution_ranges:
                    'x': [2, 2147483647]
                    'y': [2, 2147483647]
                    'z': [2, 2147483647]
                position_accuracy:
                    'x': 10e-9
                    'y': 10e-9
                    'z': 50e-9
                spot_density: 1e11
        
        ni_io:
            module.Class: 'ni_x_series.ni_x_series_finite_sampling_io.NIXSeriesFiniteSamplingIO'
            options:
                device_name: 'Dev1'
                input_channel_units:  # optional
                    PFI8: 'c/s'
                    #PFI9: 'c/s'
                    #PFI10: 'c/s'
                    #ai0: 'V'
                    #ai1: 'V'
                output_channel_units:
                    'ao0': 'V'
                    'ao1': 'V'
                    'ao2': 'V'
                adc_voltage_ranges:
                    #ai0: [-10, 10]  # optional
                    #ai1: [-10, 10]  # optional
                output_voltage_ranges:
                    ao0: [-10, 10]
                    ao1: [-10, 10]
                    ao2: [-10, 10]

                frame_size_limits: [1, 1e9]  # optional #TODO actual HW constraint?
                output_mode: 'JUMP_LIST' #'JUMP_LIST' # optional, must be name of SamplingOutputMode
                read_write_timeout: 10  # optional
                #sample_clock_output: '/Dev1/PFI11' # optional

        ni_ao:
            module.Class: 'ni_x_series.ni_x_series_analog_output.NIXSeriesAnalogOutput'
            options:
                device_name: 'Dev1'
                channels:
                    ao0:
                        limits: [-10.0, 10.0]
                        keep_value: True
                    ao1:
                        limits: [-10.0, 10.0]
                        keep_value: True
                    ao2:
                        limits: [-10.0, 10.0]
                        keep_value: True
                    ao3:
                        limits: [-10.0, 10.0]
                        keep_value: True

        
        # optional, for slow counter / timer series reader
        ni_instreamer:
            module.Class: 'ni_x_series.ni_x_series_in_streamer.NIXSeriesInStreamer'
            options:
                device_name: 'Dev1'
                digital_sources:  # optional
                    - 'PFI8'
                #analog_sources:  # optional
                #   - 'ai0'
                #   - 'ai1'
                # external_sample_clock_source: 'PFI0'  # optional
                # external_sample_clock_frequency: 1000  # optional
                adc_voltage_range: [-10, 10]  # optional
                max_channel_samples_buffer: 10000000  # optional
                read_write_timeout: 10  # optional

# Configuration hints
- The maximum scanning frequency is given by the bandwidth of your Piezo controller (check the datasheet). It might make sense to put an even smaller limit into your config, since scanning at the hardware limit might introduce artifacts/offsets to your confocal scan.
- The optimizer scan behavior and sequence are configurable in the scanning gui -> Settings -> Optimizer settings.

Deprecated:
- Until v0.5.1, the scanning gui's `optimizer_plot_dimensions` ConfigOption allowed to specify the optimizer's scanning behavior. The default setting `[2,1]` enables one 2D and one 1D optimization step. You may set to eg. `[2,2,2]` to have three two-dimensionsal scans done for optimzation. In the gui (Settings/Optimizer Settings), this will change the list of possible optimizer sequences.  

# Confocal scanning with TimeTagger counting

If you want to keep the NI X-series card for positioning (analog output and clock generation)
but delegate photon counting to a Swabian Instruments TimeTagger using its
[`CountBetweenMarkers`](https://www.swabianinstruments.com/static/documentation/TimeTagger/api/Measurements.html#countbetweenmarkers)
measurement, use the `NiTimeTaggerScanningProbeInterfuse` interfuse together with the new
`TimeTaggerFiniteCounter` hardware module.

## Wiring

- Connect the NI sample clock output (set in `sample_clock_output` on the
  `NIXSeriesFiniteSamplingIO` module, e.g. `/Dev1/PFI11`) to one TimeTagger digital input channel
  (e.g. channel 8). This channel is the `begin_channel` of `CountBetweenMarkers`.
- Connect the APD TTL output to another TimeTagger channel (e.g. channel 1). This is the
  `click_channel`.
- AO outputs (ao0/ao1/ao2) keep going to the scanner (galvos/piezo) just like in the NI-only
  setup.
- No PFI inputs of the NI card are used for counting; all NI counters except the sample clock
  counter are free.

## How the synchronization works

`NIXSeriesFiniteSamplingIO` programs its sample-clock counter for `frame_size + 1` finite pulses
per scan, where `frame_size = n_lines * (forward_resolution + backward_resolution)`.
`CountBetweenMarkers` is configured with `n_values = frame_size` and `end_channel = CHANNEL_UNUSED`,
which means each clock edge starts a new bin and closes the previous one. The TimeTagger is
armed before the NI clock starts, so the very first edge opens bin 0 and the last edge closes
bin `frame_size - 1`, giving one count value per pixel that is fully synchronous with the
scanner position.

## Example config

    gui:
        scanner_gui:
          module.Class: 'scanning.scannergui.ScannerGui'
          options:
              image_axes_padding: 0.02
              default_position_unit_prefix: null
              optimizer_plot_dimensions: [2,1]
          connect:
              scanning_logic: scanning_probe_logic
              data_logic: scanning_data_logic
              optimize_logic: scanning_optimize_logic


    logic:
        scanning_probe_logic:
            module.Class: 'scanning_probe_logic.ScanningProbeLogic'
            options:
                max_history_length: 20
                max_scan_update_interval: 2
                position_update_interval: 1
            connect:
                scanner: ni_timetagger_scanner

        scanning_data_logic:
            module.Class: 'scanning_data_logic.ScanningDataLogic'
            options:
                max_history_length: 20
            connect:
                scan_logic: scanning_probe_logic

        scanning_optimize_logic:
            module.Class: 'scanning_optimize_logic.ScanningOptimizeLogic'
            connect:
                scan_logic: scanning_probe_logic


    hardware:
        ni_timetagger_scanner:
            module.Class: 'interfuse.ni_timetagger_scanning_probe_interfuse.NiTimeTaggerScanningProbeInterfuse'
            connect:
                scan_output: 'ni_io'
                analog_output: 'ni_ao'
                counter: 'tt_counter'
            options:
                ni_channel_mapping:
                    x: 'ao0'
                    y: 'ao1'
                    z: 'ao2'
                position_ranges: # in m
                    x: [0, 200e-6]
                    y: [0, 200e-6]
                    z: [-100e-6, 100e-6]
                frequency_ranges:
                    x: [1, 5000]
                    y: [1, 5000]
                    z: [1, 1000]
                resolution_ranges:
                    x: [1, 10000]
                    y: [1, 10000]
                    z: [1, 10000]
                input_channel_units:
                    APD1: 'c/s'        # must match TimeTaggerFiniteCounter.channel_name
                backwards_line_resolution: 50
                maximum_move_velocity: 400e-6

        ni_io:
            module.Class: 'ni_x_series.ni_x_series_finite_sampling_io.NIXSeriesFiniteSamplingIO'
            options:
                device_name: 'Dev1'
                input_channel_units: {}              # output-only mode: no PFI counting on NI
                output_channel_units:
                    'ao0': 'V'
                    'ao1': 'V'
                    'ao2': 'V'
                output_voltage_ranges:
                    ao0: [-10, 10]
                    ao1: [-10, 10]
                    ao2: [-10, 10]
                frame_size_limits: [1, 1e9]
                output_mode: 'JUMP_LIST'
                read_write_timeout: 10
                sample_clock_output: '/Dev1/PFI11'   # REQUIRED: routed to the TimeTagger input

        ni_ao:
            module.Class: 'ni_x_series.ni_x_series_analog_output.NIXSeriesAnalogOutput'
            options:
                device_name: 'Dev1'
                channels:
                    ao0:
                        limits: [-10.0, 10.0]
                        keep_value: True
                    ao1:
                        limits: [-10.0, 10.0]
                        keep_value: True
                    ao2:
                        limits: [-10.0, 10.0]
                        keep_value: True

        tt_counter:
            module.Class: 'swabian_instruments.timetagger_finite_counter.TimeTaggerFiniteCounter'
            options:
                timetagger_channel_apd: 1            # APD click channel on the TimeTagger
                timetagger_channel_clock: 8         # TimeTagger channel wired to /Dev1/PFI11
                channel_name: 'APD1'                # must match input_channel_units above
                channel_unit: 'c/s'
                # trigger_level: 0.5                # optional, set TT trigger level on clock channel (V)
                sample_rate_limits: [1, 1e6]
                frame_size_limits: [1, 1e8]

## Notes

- `input_channel_units: {}` on `ni_io` selects the output-only mode added to the
  `NIXSeriesFiniteSamplingIO` module. If for any reason you cannot leave it empty in your
  branch, declare one analog input channel (e.g. `ai0: 'V'`) and the interfuse will ignore
  whatever samples come back from it; only the TimeTagger counts are pushed to the GUI.
- The NI device must have at least one free counter to generate the sample clock. All other
  counters are available for other measurements (e.g. ODMR via `ni_finite_sampling_input`).
- The `sample_rate` configured on the TimeTagger module is only used to convert raw photon
  counts to `c/s` (it multiplies `getData()` by the sample rate, mirroring the conversion done
  by the NI module for its PFI counters). The true bin boundaries are defined by the NI clock
  edges, not by this rate.

# Tilt correction

The above configuration will enable the tilt correction feature for the ScanningProbeDummy and NiScanningProbeInterfuse.
This allows to perform scans in tilted layers, eg. along the surface of a non-flat sample. 
- In the scanning_probe_gui, you can configure this feature in the menu enabled by 'View' -> 'Tilt correction'.
- Choose three support vectors in the plane that should become the new $\hat{e}_z$ plane.
  Instead of manually typing the coordinates of a support vector, hitting the 'Vec 1" button will
  insert the current crosshair position as support vector 1. 
- Enable the transformation by the "Tilt correction" button.

# Todo this readme
