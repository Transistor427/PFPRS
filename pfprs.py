# Support for power loss recovery on klipper based 3d printers
#
# Copyright (C) 2025  Ankur Verma <ankurver@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import json
import os
from collections import deque
from typing import Dict, Any, Optional, Tuple, Deque

def load_config(config):
    return PowerLossRecovery(config)

class PowerLossRecovery:
    
    def _parse_gcode_config_option(self, config, option_name, default=''):
        """
        Parse a G-code configuration option, handling multi-line commands.
        
        Args:
            config: Klipper config object
            option_name: Name of the config option to parse
            default: Default value if option is not found
        
        Returns:
            tuple: (raw_string, parsed_lines_list)
        """
        try:
            # Get raw string from config
            raw_value = config.get(option_name, default)
            
            # Parse into lines, filtering empty ones
            lines = [line.strip() for line in raw_value.split('\n') if line.strip()]
            
            if self.debug_mode:
                logging.info(f"PowerLossRecovery: Parsed {option_name}: {len(lines)} lines")
                if lines:
                    logging.info(f"First line example: {lines[0]}")
                
            return raw_value, lines
            
        except Exception as e:
            raise config.error(f"Error parsing {option_name}: {str(e)}")
            
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        
        # Get configuration values with proper error handling
        try:
            self.save_interval = config.getfloat('save_interval', 30., 
                                           minval=0., maxval=300.)
            self.save_on_layer = config.getboolean('save_on_layer', True)
            self.variables_file = config.get('variables_file', 
                                          '~/printer_state_vars.cfg')
            self.debug_mode = config.getboolean('debug_mode', False)
            self.resuming_print = False  # Flag to track PLR resume process
            
            # Get part cooling fans configuration
            self.part_cooling_fans = []
            fans_str = config.get('part_cooling_fans', '')
            if fans_str:
                self.part_cooling_fans = [fan.strip() for fan in fans_str.split(',') if fan.strip()]
                if self.debug_mode:
                    logging.info(f"PowerLossRecovery: Configured part cooling fans: {self.part_cooling_fans}")
            
            # Get extruders configuration
            self.extruders = []
            extruders_str = config.get('extruders', 'extruder')
            if extruders_str:
                self.extruders = [extruder.strip() for extruder in extruders_str.split(',') if extruder.strip()]
                if self.debug_mode:
                    logging.info(f"PowerLossRecovery: Configured extruders: {self.extruders}")
            
            # Get chamber heater configuration
            self.chamber_heater_name = config.get('chamber_heater', '')
            if self.debug_mode and self.chamber_heater_name:
                logging.info(f"PowerLossRecovery: Configured chamber heater: {self.chamber_heater_name}")
            
            # New configuration options for history
            self.history_size = config.getint('history_size', 5, minval=2, maxval=20)
            self.save_delay = config.getint('save_delay', 2, minval=0, 
                                          maxval=self.history_size - 1)
            
            if self.debug_mode:
                logging.info("PowerLossRecovery: Debug mode enabled")
                logging.info(f"PowerLossRecovery: History size: {self.history_size}, Save delay: {self.save_delay}")
            
            try:
                # Dictionary mapping config names to attribute names
                gcode_configs = {
                    'restart_gcode': ('restart_gcode', 'restart_gcode_lines'),
                    'before_resume_gcode': ('before_resume_gcode', 'before_resume_gcode_lines'),
                    'after_resume_gcode': ('after_resume_gcode', 'after_resume_gcode_lines'),
                    'before_calibrate_gcode': ('before_calibrate_gcode', 'before_calibrate_gcode_lines'),
                    'after_calibrate_gcode': ('after_calibrate_gcode', 'after_calibrate_gcode_lines')
                }
                
                # Parse each config option
                for config_name, (raw_attr, lines_attr) in gcode_configs.items():
                    raw_value, lines = self._parse_gcode_config_option(config, config_name)
                    setattr(self, raw_attr, raw_value)
                    setattr(self, lines_attr, lines)
                    
                    # Special handling for restart_gcode debug output
                    if config_name == 'restart_gcode' and self.debug_mode and lines:
                        logging.info(f"PowerLossRecovery: Found {len(lines)} restart G-code lines")
                        for line in lines:
                            logging.info(f"PowerLossRecovery: G-code line: {repr(line)}")
                            
                if self.debug_mode:
                    logging.info("PowerLossRecovery: Completed G-code configuration parsing")
                    for config_name, (raw_attr, lines_attr) in gcode_configs.items():
                        lines = getattr(self, lines_attr)
                        logging.info(f"{config_name}: {len(lines)} lines parsed")
                        
            except Exception as e:
                raise config.error(f"Error parsing G-code configurations: {str(e)}")

                
            # Important: Check if time-based saving is enabled
            self.time_based_enabled = self.save_interval > 0
            # Add tracking for cumulative layer height
            self.current_z_height = 0.

                
        except Exception as e:
            raise config.error(f"Error reading PowerLossRecovery config: {str(e)}")
        
        

        # Initialize state variables
        self.gcode = self.printer.lookup_object('gcode')
        self.save_variables = None
        self.toolhead = None
        self.extruder = None
        self.heater_bed = None
        self.heater_chamber = None
        self.last_layer = 0
        self.is_active = False
        self.last_save_time = 0
        # After other self.* initializations:
        self._last_layer_change_time = 0
        self._last_extruder_change_time = 0
        self._consecutive_failures = 0
        self._last_save_attempt = 0
        # Add with other state variables initialization
        self.power_loss_recovery_enabled = False  # Default to enabled
        
        # Initialize history queue
        self.state_history: Deque[Dict[str, Any]] = deque(maxlen=self.history_size)
        
        ### MANUAL Z HEIGHT SETTING ####
        
        self.name = config.get_name()
        # Load config values
        self.slow_homing_speed = config.getfloat('slow_homing_speed', 2.0, above=0.)
        
        # Register save_variables support
        self.save_variables = None
        try:
            self.save_variables = self.printer.load_object(config, 'save_variables')
        except self.printer.config_error as e:
            raise self.printer.config_error(
                "save_variables module required for z_offset storage")
                                  
        # Initialize lists
        self.stepper_names = ['stepper_z']
        
        # Variables for storing results
        self.z_offsets = {}
        
        # Register event handlers
        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.printer.register_event_handler("extruder:activate_extruder",
                                          self._handle_activate_extruder)
        # Add to the printer.register_event_handler section in __init__
        self.printer.register_event_handler("print_stats:complete", 
                                          self._handle_print_complete)
        self.printer.register_event_handler("print_stats:error", 
                                          self._handle_print_complete)
                                          
        # Setup GCode commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('PLR_Z_HOME',
                                  self.cmd_PLR_Z_HOME,
                                  desc=self.cmd_PLR_Z_HOME_help)
        self.gcode.register_command('PLR_SAVE_PRINT_STATE', self.cmd_PLR_SAVE_PRINT_STATE,
                                  desc=self.cmd_PLR_SAVE_PRINT_STATE_help)
        
        if self.save_on_layer:
            self.gcode.register_command('PLR_SAVE_PRINT_STATE_WITH_LAYER',
                                  self._handle_layer_change,
                                  desc="Layer change handler for PowerLossRecovery")
    
        self.gcode.register_command('PLR_QUERY_SAVED_STATE', 
                                  self.cmd_PLR_QUERY_SAVED_STATE,
                                  desc=self.cmd_PLR_QUERY_SAVED_STATE_help)
        
        self.gcode.register_command('PLR_RESET_PRINT_DATA',
                                  self.cmd_PLR_RESET_PRINT_DATA,
                                  desc=self.cmd_PLR_RESET_PRINT_DATA_help)

        # Add with other command registrations
        self.gcode.register_command('PLR_ENABLE', 
                                  self.cmd_PLR_ENABLE,
                                  desc=self.cmd_PLR_ENABLE_help)
        self.gcode.register_command('PLR_DISABLE',
                                  self.cmd_PLR_DISABLE,
                                  desc=self.cmd_PLR_DISABLE_help)
                                  
        self.gcode.register_command('PLR_RESUME_PRINT', 
                                self.cmd_PLR_RESUME_PRINT,
                                desc=self.cmd_PLR_RESUME_PRINT_help)

        self.gcode.register_command('PLR_SAVE_MESH',
                                      self.cmd_PLR_SAVE_MESH,
                                      desc=self.cmd_PLR_SAVE_MESH_help)
        self.gcode.register_command('PLR_LOAD_MESH',
                                      self.cmd_PLR_LOAD_MESH,
                                      desc=self.cmd_PLR_LOAD_MESH_help)
                                      
    ### MANUAL Z HEIGHT SETTING #####
    
    def _load_saved_z_offsets(self):
        """Load saved Z offsets from variables file"""
        if not self.save_variables:
            return {}
            
        loaded_offsets = {}
        try:
            # Get variables directly from save_variables status
            eventtime = self.printer.get_reactor().monotonic()
            variables = self.save_variables.get_status(eventtime)['variables']
            
            for name in self.stepper_names:
                var_name = f"z_offset_{name}"
                try:
                    # Look directly in variables dictionary
                    if var_name in variables:
                        offset = float(variables[var_name])
                        loaded_offsets[name] = offset
                        if self.debug_mode:
                            self._debug_log(f"Loaded offset for {name}: {offset:.3f}mm")
                    else:
                        if self.debug_mode:
                            self._debug_log(f"No saved offset found for {name}")
                except Exception as e:
                    if self.debug_mode:
                        self._debug_log(f"Error loading offset for {name}: {str(e)}")
                    continue
            
            if self.debug_mode:
                if loaded_offsets:
                    self._debug_log("Successfully loaded offsets:")
                    for name, offset in loaded_offsets.items():
                        self._debug_log(f"{name}: {offset:.3f}mm")
                else:
                    self._debug_log("No valid offsets found in variables file")
                    
            return loaded_offsets
        
        except Exception as e:
            self._debug_log(f"Error loading saved offsets: {str(e)}")
            return {}
            
    def _save_z_offset(self, name, z_offset):
        """Save Z offset to Klipper variables"""
        if not self.save_variables:
            return
        try:
            var_name = f"z_offset_{name}"
            save_cmd = self.gcode.create_gcode_command(
                "SAVE_VARIABLE", "SAVE_VARIABLE",
                {"VARIABLE": var_name, "VALUE": z_offset})
            self.save_variables.cmd_SAVE_VARIABLE(save_cmd)
        except Exception as e:
            raise self.printer.command_error(
                f"Error saving Z offset for {name}: {str(e)}")
    
    def _debug_log(self, message):
        """
        Output debug messages to both the printer console and klippy log
        
        Args:
            message: The message to log
        """
        if not self.debug_mode:
            return
            
        prefix = "PLR LOG:: "
        formatted_msg = f"{prefix}{message}"
        
        # Output to printer console
        self.gcode.respond_info(formatted_msg)
    
    def _restore_fan_speeds(self, state_data):
        """Restore part cooling fan speeds from saved state"""
        try:
            if not state_data or 'fan_speeds' not in state_data:
                if self.debug_mode:
                    self._debug_log("No fan speed data found in saved state")
                return
        
            fan_speeds = state_data.get('fan_speeds', {})
            if not fan_speeds:
                return
        
            # Get current active extruder
            toolhead = self.printer.lookup_object('toolhead')
            cur_extruder = toolhead.get_extruder().get_name()
            
            for fan_name in self.part_cooling_fans:
                try:
                    if fan_name not in fan_speeds:
                        continue
                        
                    speed = fan_speeds[fan_name]
                    
                    if self.debug_mode:
                        self._debug_log(f"Fan found - {fan_name} with speed {speed}")
                    
                    # Check if this is the parts cooling fan for current extruder
                    if fan_name == 'fan' or fan_name == f"{cur_extruder}_fan":
                        # Use M106 with P0 for parts cooling fan
                        speed_byte = int(speed * 255. + .5)
                        fan_cmd = f"M106 P0 S{speed_byte}"
                        if self.debug_mode:
                            self._debug_log(f"Setting parts cooling fan {fan_name} using M106: {fan_cmd}")
                    else:
                        # Use SET_FAN_SPEED for other fans
                        fan = self.printer.lookup_object(fan_name, None)
                        if fan is None:
                            if self.debug_mode:
                                self._debug_log(f"Fan {fan_name} not found - skipping")
                            continue
                            
                        fan_cmd = f"SET_FAN_SPEED FAN={fan_name} SPEED={speed}"
                        if self.debug_mode:
                            self._debug_log(f"Setting generic fan {fan_name} using SET_FAN_SPEED: {fan_cmd}")
                    
                    self.gcode.run_script_from_command(fan_cmd)
                    
                except Exception as e:
                    if self.debug_mode:
                        self._debug_log(f"Error restoring {fan_name} speed: {str(e)}")
        
        except Exception as e:
            if self.debug_mode:
                self._debug_log(f"Error in fan speed restoration: {str(e)}")
    
    
    def _restore_xyz_offsets(self, state_data):
        """Restore XYZ offsets from saved state"""
        try:
            if not state_data or 'xyz_offsets' not in state_data:
                if self.debug_mode:
                    self._debug_log("No XYZ offset data found in saved state")
                return
        
            offsets = state_data.get('xyz_offsets', {})
            if not offsets:
                return
        
            # Construct SET_GCODE_OFFSET command
            offset_cmd = "SET_GCODE_OFFSET"
            for axis, value in offsets.items():
                offset_cmd += f" {axis.upper()}={value}"
            
            if self.debug_mode:
                self._debug_log(f"Restoring XYZ offsets: {offset_cmd}")
            
            self.gcode.run_script_from_command(offset_cmd)
        
        except Exception as e:
            if self.debug_mode:
                self._debug_log(f"Error restoring XYZ offsets: {str(e)}")
    
    def _restore_active_extruder(self, state_data):
        """Restore active extruder from saved state"""
        try:
            if not state_data or 'active_extruder' not in state_data:
                if self.debug_mode:
                    self._debug_log("No active extruder data found in saved state")
                return
        
            active_extruder = state_data.get('active_extruder')
            if not active_extruder:
                return
                
            # Check if extruder exists
            if active_extruder not in self.extruders:
                if self.debug_mode:
                    self._debug_log(f"Extruder {active_extruder} not in configured extruders list")
                return
                
            # Activate the extruder
            activate_cmd = f"ACTIVATE_EXTRUDER EXTRUDER={active_extruder}"
            if self.debug_mode:
                self._debug_log(f"Restoring active extruder: {activate_cmd}")
            
            self.gcode.run_script_from_command(activate_cmd)
        
        except Exception as e:
            if self.debug_mode:
                self._debug_log(f"Error restoring active extruder: {str(e)}")
    
    def _restore_chamber_temperature(self, state_data):
        """Restore chamber temperature from saved state"""
        try:
            if not state_data or 'chamber_temp' not in state_data:
                if self.debug_mode:
                    self._debug_log("No chamber temperature data found in saved state")
                return
        
            chamber_temp = state_data.get('chamber_temp', 0)
            if chamber_temp <= 0:
                if self.debug_mode:
                    self._debug_log("Chamber temperature is 0 or negative, skipping restoration")
                return
                
            if not self.chamber_heater_name:
                if self.debug_mode:
                    self._debug_log("No chamber heater configured")
                return
                
            # Set chamber temperature
            chamber_cmd = f"SET_HEATER_TEMPERATURE HEATER={self.chamber_heater_name} TARGET={chamber_temp}"
            if self.debug_mode:
                self._debug_log(f"Restoring chamber temperature: {chamber_cmd}")
            
            self.gcode.run_script_from_command(chamber_cmd)
        
        except Exception as e:
            if self.debug_mode:
                self._debug_log(f"Error restoring chamber temperature: {str(e)}")
                
            
    cmd_PLR_Z_HOME_help = "Set Z axis height manually. MODE=CALIBRATE to save height, MODE=RESUME to use saved height"
    def cmd_PLR_Z_HOME(self, gcmd):
        mode = gcmd.get('MODE', 'CALIBRATE').upper()
        if mode not in ['CALIBRATE', 'RESUME']:
            raise self.printer.command_error("MODE must be either CALIBRATE or RESUME")
            
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        
        # Execute pre-operation G-code based on mode
        try:
            if mode == 'RESUME' and self.before_resume_gcode_lines:
                if self.debug_mode:
                    self._debug_log("Executing before-resume G-code commands...")
                for line in self.before_resume_gcode_lines:
                    self.gcode.run_script_from_command(line)
                    toolhead.wait_moves()
            elif mode == 'CALIBRATE' and self.before_calibrate_gcode_lines:
                if self.debug_mode:
                    self._debug_log("Executing before-calibrate G-code commands...")
                for line in self.before_calibrate_gcode_lines:
                    self.gcode.run_script_from_command(line)
                    toolhead.wait_moves()
        except Exception as e:
            raise self.printer.command_error(
                f"Error executing pre-operation G-code: {str(e)}")
        
        if 'z' not in toolhead.get_status(curtime)['homed_axes']:
            raise self.printer.command_error("Must home Z first")
        
        try:
            if self.debug_mode:
                self._debug_log(f"\nStarting PLR Z Home in {mode} mode")
            
            if mode == 'CALIBRATE':
                # Get Z height from parameter
                target_z = gcmd.get_float('Z')
                if self.debug_mode:
                    self._debug_log(f"Setting Z height to: {target_z}")
                
                # Save Z height
                self._save_z_offset('stepper_z', target_z)
                
                # Move to target Z height
                pos = toolhead.get_position()
                pos[2] = target_z
                toolhead.manual_move(pos, self.slow_homing_speed)
                toolhead.wait_moves()
                
            else:  # RESUME mode
                # Load saved Z height
                saved_offsets = self._load_saved_z_offsets()
                target_z = saved_offsets.get('stepper_z', 0)
                
                if self.debug_mode:
                    self._debug_log(f"Restoring Z height to: {target_z}")
                
                # Move to saved Z height
                pos = toolhead.get_position()
                pos[2] = target_z
                toolhead.manual_move(pos, self.slow_homing_speed)
                toolhead.wait_moves()
            
            try:
                # Execute post-operation G-code based on mode
                if mode == 'RESUME' and self.after_resume_gcode_lines:
                    if self.debug_mode:
                        self._debug_log("Executing after-resume G-code commands...")
                    for line in self.after_resume_gcode_lines:
                        self.gcode.run_script_from_command(line)
                        toolhead.wait_moves()
                elif mode == 'CALIBRATE' and self.after_calibrate_gcode_lines:
                    if self.debug_mode:
                        self._debug_log("Executing after-calibrate G-code commands...")
                    for line in self.after_calibrate_gcode_lines:
                        self.gcode.run_script_from_command(line)
                        toolhead.wait_moves()
            except Exception as e:
                # Log error but don't fail the operation
                if self.debug_mode:
                    self._debug_log(
                        f"Warning: Error executing post-operation G-code: {str(e)}")
            
            if mode == 'RESUME':
                try:
                    saved_state = self._get_saved_state()
                    if saved_state:
                        # Restore fan speeds
                        self._restore_fan_speeds(saved_state)
                        # Restore XYZ offsets
                        self._restore_xyz_offsets(saved_state)
                        # Restore active extruder
                        self._restore_active_extruder(saved_state)
                        # Restore chamber temperature
                        self._restore_chamber_temperature(saved_state)
                except Exception as e:
                    if self.debug_mode:
                        self._debug_log(f"Warning: Error restoring settings: {str(e)}")
                
                self.resuming_print = False  # Ensure flag is set before you start printing.
            
            # Success message with results
            msg = [f"\nPLR Z Home ({mode} mode) completed successfully:"]
            msg.append(f"Z height: {target_z:.3f}mm")
            
            self._debug_log("\n".join(msg))
            
        except Exception as e:
            msg = str(e)
            if self.debug_mode:
                self._debug_log(f"Error in Z calibration: {str(e)}")
            raise self.printer.command_error(
                f"Z calibration failed: {msg}")
            
    ### STATE MANAGEMENT and GCODE MODIFICATION ####
    
                                        
    def _verify_state(self, state: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Verify the integrity and validity of a state before saving.
        Returns (is_valid, error_message).
        """
        if not isinstance(state, dict):
            return False, "State must be a dictionary"
            
        # Required keys and their type checks
        required_fields = {
            'position': {
                'type': dict,
                'subfields': {'x': (float, int), 'y': (float, int), 'z': (float, int)}
            },
            'layer': {'type': int},
            'layer_height': {'type': (float, int)},
            'file_progress': {
                'type': dict,
                'subfields': {
                    'position': int,
                    'total_size': int,
                    'progress_pct': (float, int)
                }
            },
            'collection_time': {'type': (float, int)},
            'save_time': {'type': (float, int)},
            'hotend_temp': {'type': (float, int)},
            'bed_temp': {'type': (float, int)},
            'chamber_temp': {'type': (float, int)},
            'active_extruder': {'type': str}
        }
        
        # Verify all required fields exist and are of correct type
        for field, validation in required_fields.items():
            if field not in state:
                return False, f"Missing required field: {field}"
                
            field_type = validation['type']
            if isinstance(field_type, tuple):
                if not isinstance(state[field], field_type):
                    return False, f"Field {field} has wrong type"
            else:
                if not isinstance(state[field], validation['type']):
                    return False, f"Field {field} has wrong type"
                
            # Check subfields if they exist
            if 'subfields' in validation:
                for subfield, subtype in validation['subfields'].items():
                    if subfield not in state[field]:
                        return False, f"Missing subfield {subfield} in {field}"
                    if isinstance(subtype, tuple):
                        if not isinstance(state[field][subfield], subtype):
                            return False, f"Subfield {subfield} in {field} has wrong type"
                    else:
                        if not isinstance(state[field][subfield], subtype):
                            return False, f"Subfield {subfield} in {field} has wrong type"
        
        # Verify logical constraints
        if state['file_progress']['total_size'] < 0:
            return False, "File size cannot be negative"
            
        if not (0 <= state['file_progress']['progress_pct'] <= 100):
            return False, "Progress percentage must be between 0 and 100"
            
        if state['layer'] < 0:
            return False, "Layer number cannot be negative"
                
        # Verify temperature ranges (basic sanity checks)
        if not (-273.15 <= float(state['hotend_temp']) <= 500):
            return False, "Hotend temperature out of reasonable range"
            
        if not (-273.15 <= float(state['bed_temp']) <= 200):
            return False, "Bed temperature out of reasonable range"
            
        if not (-273.15 <= float(state['chamber_temp']) <= 100):
            return False, "Chamber temperature out of reasonable range"
            
        # Verify active extruder is in configured list
        if state['active_extruder'] not in self.extruders:
            return False, f"Active extruder {state['active_extruder']} not in configured extruders list"
            
        return True, None


    def _optimize_background_interval(self) -> float:
        """
        Calculate optimal background task interval based on current conditions.
        Returns the recommended interval in seconds.
        """
        try:
            # Base interval from configuration
            interval = self.save_interval
            
            # Track time since critical events
            current_time = self.reactor.monotonic()
            time_since_extruder_change = current_time - self._last_extruder_change_time
            
            # Start with base interval
            reduction_factor = 1.0
            
            # Extruder change effect (moderate at start, tapering off)
            if time_since_extruder_change < 20:
                extruder_factor = 0.3 + (time_since_extruder_change / 20.0) * 0.7
                reduction_factor = min(reduction_factor, extruder_factor)
            
            # Apply reduction to interval
            interval = interval * reduction_factor
            
            # Check recent state changes if we have history
            if len(self.state_history) >= 2:
                last_states = list(self.state_history)[-2:]
                
                # Calculate position change
                pos_change = sum(
                    abs(last_states[1]['position'][axis] - 
                        last_states[0]['position'][axis])
                    for axis in ['x', 'y', 'z']
                )
                
                # If significant movement, further decrease interval
                if pos_change > 10:  # mm of movement
                    interval = interval * 0.75
                
                # Check temperature stability
                temp_change = abs(
                    last_states[1]['hotend_temp'] - 
                    last_states[0]['hotend_temp']
                )
                if temp_change > 5:  # degrees of change
                    interval = interval * 0.75
            
            # Different minimum intervals based on event type
            min_interval = 5.0  # default minimum
            if time_since_extruder_change < 5:  # Very recent extruder change
                min_interval = 3.0  # Moderate for extruder changes
                
            # Never go below minimum safe interval for current conditions
            return max(interval, min_interval)
            
        except Exception as e:
            if self.debug_mode:
                logging.info(f"Error optimizing interval: {str(e)}")
            return self.save_interval

    def _collect_current_state(self) -> Dict[str, Any]:
          try:
              # Get single eventtime for all status queries
              eventtime = self.reactor.monotonic()
              
              # Get all status objects at once using the same eventtime
              try:
                  print_stats = self.printer.lookup_object('print_stats')
                  virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
                  print_stats_status = print_stats.get_status(eventtime)
                  sdcard_status = virtual_sdcard.get_status(eventtime)
                  extruder_status = self.extruder.get_status(eventtime)
                  toolhead_status = self.toolhead.get_status(eventtime)
                  heater_bed_status = self.heater_bed.get_status(eventtime) if self.heater_bed else {}
                  heater_chamber_status = self.heater_chamber.get_status(eventtime) if self.heater_chamber else {}
                  
                  # Get fan speeds for configured part cooling fans
                  fan_speeds = {}
                  for fan_name in self.part_cooling_fans:
                      try:
                          fan = self.printer.lookup_object(fan_name)
                          if fan:
                              fan_status = fan.get_status(eventtime)
                              fan_speeds[fan_name] = round(float(fan_status.get('speed', 0)), 3)
                      except Exception as e:
                          if self.debug_mode:
                              logging.info(f"PowerLossRecovery: Error getting status for fan {fan_name}: {str(e)}")
                  
                  # Get file information
                  current_file = print_stats_status.get('filename', 'unknown')
                  file_position = sdcard_status.get('file_position', 0)
                  file_size = sdcard_status.get('file_size', 0)
                  
                  # Calculate progress percentage
                  progress = (file_position / file_size * 100) if file_size > 0 else 0
                  
                  current_progress = {
                      'position': file_position,
                      'total_size': file_size,
                      'progress_pct': round(progress, 2)
                  }
                  
                  # Get positions from the same timestamp
                  cur_pos = toolhead_status.get('position', [0., 0., 0., 0.])[:3]
                  
                  # Get temperatures from the same timestamp
                  hotend_temp = extruder_status.get('temperature', 0)
                  bed_temp = heater_bed_status.get('temperature', 0) if self.heater_bed else 0
                  chamber_temp = heater_chamber_status.get('temperature', 0) if self.heater_chamber else 0
                  
                  # Get current XYZ offsets
                  gcode_move = self.printer.lookup_object('gcode_move')
                  if gcode_move:
                      gcode_status = gcode_move.get_status(eventtime)
                      homing_origin = gcode_status.get('homing_origin', [0., 0., 0.])
                      position_offset = gcode_status.get('position_offset', [0., 0., 0.])
                      xyz_offsets = {
                          'x': round(float(homing_origin[0] + position_offset[0]), 3),
                          'y': round(float(homing_origin[1] + position_offset[1]), 3),
                          'z': round(float(homing_origin[2] + position_offset[2]), 3)
                      }
                  else:
                      xyz_offsets = {'x': 0., 'y': 0., 'z': 0.}
                      if self.debug_mode:
                          logging.info("PowerLossRecovery: Could not get gcode_move object for XYZ offsets")

                  # Compile synchronized state information
                  state_info = {
                      'position': {
                          'x': round(float(cur_pos[0]), 3),
                          'y': round(float(cur_pos[1]), 3),
                          'z': round(float(cur_pos[2]), 3)
                      },
                      'xyz_offsets': xyz_offsets,
                      'fan_speeds': fan_speeds,
                      'layer': self.last_layer,
                      'layer_height': round(float(self.current_z_height), 3),
                      'file_progress': current_progress,
                      'active_extruder': extruder_status.get('active_extruder', 'extruder'),
                      'hotend_temp': round(float(hotend_temp), 1),
                      'bed_temp': round(float(bed_temp), 1),
                      'chamber_temp': round(float(chamber_temp), 1),
                      'save_time': eventtime,
                      'current_file': current_file,
                      'collection_time': eventtime  # Add timestamp for verification
                  }
                  
                  if self.debug_mode:
                      logging.info(f"PowerLossRecovery: Collected synchronized state at time {eventtime:.2f} "
                                 f"for file {current_file} at {current_progress['progress_pct']:.2f}%")
                  
                  return state_info
                  
              except Exception as e:
                  if self.debug_mode:
                      logging.info(f"Error collecting synchronized state: {str(e)}")
                  return None
                  
          except Exception as e:
              logging.exception("PowerLossRecovery: Error in state collection")
              if self.debug_mode:
                  logging.info(f"Error collecting state: {str(e)}")
              return None

    def _get_move_buffer_status(self) -> dict:
        """Get move buffer status from MCU"""
        try:
            mcu = self.printer.lookup_object('mcu')
            status = mcu.get_status(self.reactor.monotonic())
            return {
                'moves_pending': status.get('moves_pending', 0),
                'min_move_time': status.get('min_move_time', 0),
                'max_move_time': status.get('max_move_time', 0)
            }
        except Exception as e:
            if self.debug_mode:
                self._debug_log(f"Error getting move buffer status: {str(e)}")
            return {'moves_pending': 0, 'min_move_time': 0, 'max_move_time': 0}

    def _calculate_optimal_delay(self) -> int:
        try:
            toolhead = self.printer.lookup_object('toolhead')
            reactor = self.printer.get_reactor()
            mcu = self.printer.lookup_object('mcu')
            eventtime = reactor.monotonic()
            print_time = toolhead.get_last_move_time()
            est_print_time = mcu.estimated_print_time(eventtime)
            
            # Calculate MCU lag
            mcu_lag = print_time - est_print_time
            
            # Calculate how many save_intervals we need to cover the MCU lag
            needed_intervals = max(1, int(mcu_lag / self.save_interval) + 1)
            
            # Calculate optimal delay to cover the lag
            optimal_delay = needed_intervals
            
            if self.debug_mode:
                self._debug_log(
                    f"Delay calculation:\n"
                    f"  Print time: {print_time:.3f}\n"
                    f"  Est print time: {est_print_time:.3f}\n"
                    f"  MCU lag: {mcu_lag:.3f}s\n"
                    f"  Save interval: {self.save_interval}s\n"
                    f"  Needed intervals: {needed_intervals}\n"
                    f"  Total time lag: {mcu_lag + (optimal_delay * self.save_interval):.3f}s\n"
                    f"  Final delay: {optimal_delay} (base: {self.save_delay})"
                )
                
            return min(optimal_delay, self.history_size - 1)
            
        except Exception as e:
            if self.debug_mode:
                self._debug_log(f"Error calculating optimal delay: {str(e)}")
            return self.save_delay

    def _save_current_state(self):
        if not self.is_active:
            if self.debug_mode:
                logging.info("PowerLossRecovery: Not saving state - printer not active")
            return
            
        if self.resuming_print:
            if self.debug_mode:
                logging.info("PowerLossRecovery: Not saving state - PLR resume in progress")
            return
            
        if not self.power_loss_recovery_enabled:
            if self.debug_mode:
                logging.info("PowerLossRecovery: Not saving state - power loss recovery disabled")
            return
                
        if self.save_variables is None:
            if self.debug_mode:
                logging.info("PowerLossRecovery: Cannot save state - save_variables not initialized")
            return
        
        try:
            # Get the delayed state from history
            optimal_delay = self._calculate_optimal_delay()
            
            if len(self.state_history) > optimal_delay:
                
                # Convert deque to list for easier indexing from end
                history_list = list(self.state_history)
                state_to_save = history_list[-(self.save_delay + 1)]
                
                # Add buffer status to saved state
                buffer_status = self._get_move_buffer_status()
                state_to_save['mcu_status'] = buffer_status
                
                # Verify state before saving
                is_valid, error_msg = self._verify_state(state_to_save)
                if not is_valid:
                    if self.debug_mode:
                        logging.info(f"PowerLossRecovery: Invalid state, not saving: {error_msg}")
                    return
                
                if self.debug_mode:
                    collection_time = state_to_save.get('collection_time', 0)
                    progress_info = state_to_save.get('file_progress', {})
                    logging.info(f"PowerLossRecovery: Saving synchronized state from time {collection_time:.2f} "
                               f"at {progress_info.get('progress_pct', 0):.2f}% completion")
                
                # Save to variables file
                state_json = json.dumps(state_to_save)
                escaped_json = state_json.replace('"', '\\"')
                self.gcode.run_script_from_command(
                    f'SAVE_VARIABLE VARIABLE=resume_meta_info VALUE="{escaped_json}"')
                
                self.last_save_time = self.reactor.monotonic()
                self._consecutive_failures = 0
                
                if self.debug_mode:
                    logging.info("PowerLossRecovery: Successfully saved synchronized state")
            else:
                if self.debug_mode:
                    logging.info(f"PowerLossRecovery: Not enough history ({len(self.state_history)} states) "
                               f"to save delayed state (need {self.save_delay + 1})")
                
        except Exception as e:
            logging.exception("PowerLossRecovery: Error saving printer state")
            if self.debug_mode:
                logging.info(f"Error saving printer state: {str(e)}")
          

    def _background_task(self, eventtime):
        try:
            # Previous state tracking
            last_save_attempt = getattr(self, '_last_save_attempt', 0)
            consecutive_failures = getattr(self, '_consecutive_failures', 0)
            
            print_stats = self.printer.lookup_object('print_stats')
            current_state = print_stats.get_status(eventtime)['state']
            printing = current_state == 'printing'
            
            if printing != self.is_active:
                self.is_active = printing
                if printing:
                    if self.debug_mode:
                        logging.info("PowerLossRecovery: Print started - activating")
                    self.state_history.clear()
                    consecutive_failures = 0
                else:
                    if self.debug_mode:
                        logging.info("PowerLossRecovery: Print ended - deactivating")
            
            if self.is_active :
                if not self.power_loss_recovery_enabled:
                    return eventtime + 1.0  # Check again in 30 seconds
                    
                # Collect current state
                current_state = self._collect_current_state()
                if current_state:
                    # Verify state before adding to history
                    is_valid, error_msg = self._verify_state(current_state)
                    if is_valid:
                        self.state_history.append(current_state)
                        consecutive_failures = 0
                        if self.debug_mode:
                            logging.info(
                                f"PowerLossRecovery: Collected valid state "
                                f"(history size: {len(self.state_history)})"
                            )
                    else:
                        consecutive_failures += 1
                        if self.debug_mode:
                            logging.info(
                                f"PowerLossRecovery: Invalid state collected: {error_msg}"
                            )
                
                # Determine if we should save state
                should_save = False
                if self.time_based_enabled:
                    time_since_last = eventtime - self.last_save_time
                    interval = self._optimize_background_interval()
                    should_save = time_since_last >= interval
                
                # Implement exponential backoff for failures
                if consecutive_failures > 0:
                    backoff = min(30, 2 ** consecutive_failures)
                    if eventtime - last_save_attempt < backoff:
                        should_save = False
                
                if should_save:
                    self._last_save_attempt = eventtime
                    self._save_current_state()
            
            # Store state for next iteration
            self._consecutive_failures = consecutive_failures
            
            # Calculate next wake time
            if not self.time_based_enabled or not printing:
                return eventtime + 1.0
            
            # Use optimized interval
            return eventtime + self._optimize_background_interval()
                
        except Exception as e:
            logging.exception("Error in background task")
            return eventtime + 1.0
              
                      
    def _handle_layer_change(self, gcmd):
        if not self.save_on_layer or not self.is_active:
            return
            
        try:
            self.last_layer = gcmd.get_int('LAYER', None)
            layer_height = gcmd.get_float('LAYER_HEIGHT', None)
            
            # Update last layer change time
            self._last_layer_change_time = self.reactor.monotonic()
            
            # Track cumulative layer height if provided
            if layer_height is not None:
                self.current_z_height = layer_height
                if self.debug_mode:
                    logging.info(f"PowerLossRecovery: Layer height: {layer_height:.3f}, "
                               f"Cumulative Z height: {self.current_z_height:.3f}")
            
            if self.debug_mode:
                logging.info(f"PowerLossRecovery: Layer changed to {self.last_layer}")
            self._save_current_state()
            
            # Trigger the next background task immediately if active
            if self.is_active and self.time_based_enabled:
                self.reactor.register_timer(self._background_task, self.reactor.monotonic())
                
        except Exception as e:
            logging.exception("Error handling layer change")
            if self.debug_mode:
                logging.info(f"Error handling layer change: {str(e)}")
        
    def _handle_activate_extruder(self, eventtime):
        if not self.is_active:
            if self.debug_mode:
                logging.info("PowerLossRecovery: Extruder activation detected but printer not active")
            return
            
        try:
            # Update last extruder change time
            self._last_extruder_change_time = self.reactor.monotonic()
            
            if self.debug_mode:
                logging.info("PowerLossRecovery: Extruder activation detected - saving state")
            self._save_current_state()
            
            # Trigger the next background task immediately if active
            if self.is_active and self.time_based_enabled:
                self.reactor.register_timer(self._background_task, self.reactor.monotonic())
                
        except Exception as e:
            logging.exception("Error handling extruder activation")
            if self.debug_mode:
                logging.info(f"Error saving state after extruder activation: {str(e)}")
                
    def _handle_connect(self):
        try:
            self.save_variables = self.printer.lookup_object('save_variables', None)
            if self.save_variables is None:
                logging.info("save_variables not found. PowerLossRecovery will not save state.")
            elif self.debug_mode:
                logging.info("PowerLossRecovery: Successfully connected to save_variables")
            
            # Try to get chamber heater if configured
            if self.chamber_heater_name:
                try:
                    self.heater_chamber = self.printer.lookup_object(self.chamber_heater_name, None)
                    if self.heater_chamber and self.debug_mode:
                        logging.info(f"PowerLossRecovery: Found chamber heater: {self.chamber_heater_name}")
                except Exception as e:
                    if self.debug_mode:
                        logging.info(f"PowerLossRecovery: Error getting chamber heater: {str(e)}")
        except Exception as e:
            logging.exception("Error during PowerLossRecovery connection")
            raise
    
    def _handle_ready(self):
        try:
            self.toolhead = self.printer.lookup_object('toolhead')
            self.extruder = self.printer.lookup_object('extruder')
            self.heater_bed = self.printer.lookup_object('heater_bed', None)
            
            # Try to get chamber heater if configured
            if self.chamber_heater_name and not self.heater_chamber:
                try:
                    self.heater_chamber = self.printer.lookup_object(self.chamber_heater_name, None)
                    if self.heater_chamber and self.debug_mode:
                        logging.info(f"PowerLossRecovery: Found chamber heater: {self.chamber_heater_name}")
                except Exception as e:
                    if self.debug_mode:
                        logging.info(f"PowerLossRecovery: Error getting chamber heater: {str(e)}")
            
            if self.debug_mode:
                logging.info("PowerLossRecovery: Ready state - starting background task")
            
            # Start periodic timer with immediate first run
            self.reactor.register_timer(self._background_task, self.reactor.NOW)
            
        except Exception as e:
            logging.exception("Error during PowerLossRecovery ready state")
            raise
            
    
    def _reset_state(self):
        if self.save_variables is None:
            return
            
        try:
            empty_state = json.dumps({})
            self.gcode.run_script_from_command(
                f'SAVE_VARIABLE VARIABLE=resume_meta_info VALUE="{empty_state}"')
            self.last_layer = 0
            self.last_save_time = 0
            if self.debug_mode:
                logging.info("PowerLossRecovery: State reset completed")
        except Exception as e:
            logging.exception("Error resetting state")
            if self.debug_mode:
                logging.info(f"Error resetting printer state: {str(e)}")

    cmd_PLR_QUERY_SAVED_STATE_help = "Query the current status of the state saver"
    def cmd_PLR_QUERY_SAVED_STATE(self, gcmd):
        msg = ["PowerLossRecovery Status:"]
        msg.append(f"Active: {self.is_active}")
        msg.append(f"Power Loss Recovery: {'Enabled' if self.power_loss_recovery_enabled else 'Disabled'}")
        msg.append(f"Debug Mode: {'Enabled' if self.debug_mode else 'Disabled'}")
        msg.append(f"Time-based saving: {'Enabled (%ds interval)' % self.save_interval if self.time_based_enabled else 'Disabled'}")
        msg.append(f"Layer-based saving: {'Enabled (current layer: %d)' % self.last_layer if self.save_on_layer else 'Disabled'}")
        msg.append(f"History size: {self.history_size} (current: {len(self.state_history)})")
        msg.append(f"Save delay: {self.save_delay} states")
        
        try:
            val = self.save_variables.get_stored_variable('resume_meta_info')
            if val:
                saved_data = json.loads(val)
                progress_info = saved_data.get('file_progress', {})
                collection_time = saved_data.get('collection_time', 0)
                msg.extend([
                    "",
                    "Currently Saved State:",
                    f"Collected at: {collection_time:.2f}",
                    f"File: {saved_data.get('current_file', 'unknown')}",
                    f"Layer: {saved_data.get('layer', 'unknown')}",
                    f"Progress: {progress_info.get('progress_pct', 0):.2f}% " +
                    f"(Position: {progress_info.get('position', 0)}/{progress_info.get('total_size', 0)} bytes)",
                    "Position: X%.1f Y%.1f Z%.1f" % (
                        saved_data.get('position', {}).get('x', 0),
                        saved_data.get('position', {}).get('y', 0),
                        saved_data.get('position', {}).get('z', 0)
                    ),
                    f"Temperatures - Hotend: {saved_data.get('hotend_temp', 0):.1f}°C, " +
                    f"Bed: {saved_data.get('bed_temp', 0):.1f}°C, " +
                    f"Chamber: {saved_data.get('chamber_temp', 0):.1f}°C",
                    f"Active Extruder: {saved_data.get('active_extruder', 'unknown')}"
                ])
        except Exception as e:
            if self.debug_mode:
                msg.append(f"\nError reading saved state: {str(e)}")
                
                        
    cmd_PLR_SAVE_PRINT_STATE_help = "Manually save current printer state"
    def cmd_PLR_SAVE_PRINT_STATE(self, gcmd):
        self._save_current_state()
        gcmd.respond_info("Printer state saved")
        
    cmd_PLR_RESET_PRINT_DATA_help = "Clear all saved state data"
    def cmd_PLR_RESET_PRINT_DATA(self, gcmd):
        try:
            self._reset_state()
            msg = "PowerLossRecovery: All saved state data cleared"
            if self.debug_mode:
                msg += "\nDebug: Reset complete, variables file updated"
            gcmd.respond_info(msg)
        except Exception as e:
            gcmd.respond_info(f"Error clearing saved state: {str(e)}")    
            
    cmd_PLR_ENABLE_help = "Enable power loss recovery state saving"
    def cmd_PLR_ENABLE(self, gcmd):
        self.power_loss_recovery_enabled = True
        if self.debug_mode:
            logging.info("PowerLossRecovery: Power loss recovery enabled")
        gcmd.respond_info("Power loss recovery enabled")
    
    cmd_PLR_DISABLE_help = "Disable power loss recovery state saving"
    def cmd_PLR_DISABLE(self, gcmd):
        self.power_loss_recovery_enabled = False
        if self.debug_mode:
            logging.info("PowerLossRecovery: Power loss recovery disabled")
        gcmd.respond_info("Power loss recovery disabled")
    
    cmd_PLR_SAVE_MESH_help = "Save the currently active bed mesh profile name to variables file"
    def cmd_PLR_SAVE_MESH(self, gcmd):
        try:
            bed_mesh = self.printer.lookup_object('bed_mesh')
            if bed_mesh is None:
                raise self.printer.command_error("bed_mesh module not found")
                
            status = bed_mesh.get_status(self.reactor.monotonic())
            profile_name = status.get('profile_name')
            
            if not profile_name:
                raise self.printer.command_error("No bed mesh profile currently active")
                
            # Convert profile name to JSON string to handle literals properly
            save_cmd = self.gcode.create_gcode_command(
                "SAVE_VARIABLE", "SAVE_VARIABLE",
                {"VARIABLE": "saved_mesh_profile", "VALUE": json.dumps(profile_name)})
            self.save_variables.cmd_SAVE_VARIABLE(save_cmd)
            
            if self.debug_mode:
                self._debug_log(f"Saved bed mesh profile: {profile_name}")
                
            gcmd.respond_info(f"Saved bed mesh profile: {profile_name}")
            
        except Exception as e:
            msg = f"Error saving bed mesh profile: {str(e)}"
            if self.debug_mode:
                self._debug_log(msg)
            raise self.printer.command_error(msg)
            
    cmd_PLR_LOAD_MESH_help = "Load the previously saved bed mesh profile"
    def cmd_PLR_LOAD_MESH(self, gcmd):
        try:
            if self.save_variables is None:
                raise self.printer.command_error("save_variables not initialized")
                
            eventtime = self.reactor.monotonic()
            variables = self.save_variables.get_status(eventtime)['variables']
            profile_name = variables.get('saved_mesh_profile')
            
            if not profile_name or profile_name == '""':
                raise self.printer.command_error("No saved bed mesh profile found")
                
            # Remove any quotes from the stored profile name
            profile_name = profile_name.strip('"')
            profile_name = profile_name.strip('"')
            # Verify bed_mesh object exists
            bed_mesh = self.printer.lookup_object('bed_mesh')
            if bed_mesh is None:
                raise self.printer.command_error("bed_mesh module not found")
                
            # Verify profile exists before trying to load it
            profiles = bed_mesh.get_status(eventtime).get('profiles', {})
            if profile_name not in profiles:
                raise self.printer.command_error(f"Profile '{profile_name}' not found in bed_mesh profiles")
            
            load_cmd = f"BED_MESH_PROLOAD='{profile_name}'"
            self.gcode.run_script_from_command(load_cmd)
            
            if self.debug_mode:
                self._debug_log(f"Loaded bed mesh profile: {profile_name}")
                
            gcmd.respond_info(f"Loaded bed mesh profile: {profile_name}")
            
        except Exception as e:
            msg = f"Error loading bed mesh profile: {str(e)}"
            if self.debug_mode:
                self._debug_log(msg)
            raise self.printer.command_error(msg)

    
    ### POWER LOSS RECOVERY GCODE MODIFICATION SECTION ####
    
    
    def _get_saved_state(self) -> Optional[Dict[str, Any]]:
        """
        Retrieve the last saved state from the variables file.
        Returns None if no valid state is found.
        """
        try:
            if self.save_variables is None:
                if self.debug_mode:
                    logging.info("PowerLossRecovery: Cannot get state - save_variables not initialized")
                return None
                
            # Get the stored state from save_variables status
            eventtime = self.reactor.monotonic()
            variables = self.save_variables.get_status(eventtime)['variables']
            state_data = variables.get('resume_meta_info')
            
            if not state_data:
                if self.debug_mode:
                    logging.info("PowerLossRecovery: No saved state found")
                return None
                
            # Verify state integrity
            is_valid, error_msg = self._verify_state(state_data)
            if not is_valid:
                if self.debug_mode:
                    logging.info(f"PowerLossRecovery: Invalid saved state: {error_msg}")
                return None
                
            return state_data
            
        except Exception as e:
            if self.debug_mode:
                logging.info(f"PowerLossRecovery: Error getting saved state: {str(e)}")
            return None
            
    def _get_gcode_dir(self) -> str:
        """
        Get the gcode directory from Klipper's configuration.
        Returns the configured path or the default '~/gcode' if not found.
        """
        try:
            # Get the virtual_sdcard object directly
            virtual_sd = self.printer.lookup_object('virtual_sdcard')
            if virtual_sd:
                if hasattr(virtual_sd, 'sdcard_dirname'):
                    return os.path.expanduser(virtual_sd.sdcard_dirname)
                if hasattr(virtual_sd, '_sdcard_dirname'):
                    return os.path.expanduser(virtual_sd._sdcard_dirname)
        except Exception as e:
            if self.debug_mode:
                logging.info(f"PowerLossRecovery: Error getting gcode directory from config: {str(e)}")
        
        # Default fallback path
        return os.path.expanduser('~/gcode')
    
    cmd_PLR_RESUME_PRINT_help = "Create a modified gcode file for power loss recovery resume"
    def cmd_PLR_RESUME_PRINT(self, gcmd):
        """
        Create a modified version of the last printed gcode file for power loss recovery.
        The new file will start from the last saved position.
        """
        try:
            
            self.resuming_print = True  # Set flag to disable state saving
            
            # Get the saved state
            state_data = self._get_saved_state()
            if not state_data:
                gcmd.respond_info("No valid saved state found")
                return
                
            # Extract required information
            current_file = state_data.get('current_file')
            if not current_file:
                gcmd.respond_info("No filename found in saved state")
                return
                
            file_progress = state_data.get('file_progress', {})
            file_position = file_progress.get('position')
            if file_position is None:
                gcmd.respond_info("No file position found in saved state")
                return
                
            # Get gcode directory from config and construct full file path
            gcode_dir = self._get_gcode_dir()
            input_file = os.path.join(gcode_dir, current_file)
            
            if not os.path.exists(input_file):
                gcmd.respond_info(f"Original gcode file not found: {input_file}")
                return
                
            # Modify the gcode file
            output_file = self._modify_gcode_file(input_file, file_position)
            if not output_file:
                gcmd.respond_info("Error creating modified gcode file")
                return
                
            # Start the print with the modified file
            try:
                virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
                if not virtual_sdcard:
                    raise self.printer.command_error("virtual_sdcard not found")
                    
                # Load and start the print
                basename = os.path.basename(output_file)
                self.gcode.run_script_from_command(f'SDCARD_PRINT_FILE FILENAME="{basename}"')
                
                msg = [
                    f"Created and started power loss recovery file: {basename}",
                    f"Original file: {current_file}",
                    f"Resume position: {file_position} ({file_progress.get('progress_pct', 0):.1f}%)",
                    f"Active extruder: {state_data.get('active_extruder', 'unknown')}",
                    f"Chamber temperature: {state_data.get('chamber_temp', 0):.1f}°C"
                ]
                gcmd.respond_info("\n".join(msg))
                
            except Exception as e:
                gcmd.respond_info(f"Error starting print: {str(e)}")
            
        except Exception as e:
            gcmd.respond_info(f"Error processing PLR resume: {str(e)}")
    
    
    def _modify_gcode_file(self, input_file: str, file_position: int) -> Optional[str]:
        try:
            # Get saved state for position information
            saved_state = self._get_saved_state()
            if not saved_state:
                if self.debug_mode:
                    self._debug_log("PowerLossRecovery: No saved state found for position restoration")
                return None
                
            # Extract Z position from saved state
            try:
                saved_z = saved_state['position']['z']
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Found saved Z position: {saved_z}")
            except KeyError as e:
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Could not find Z position in saved state: {e}")
                return None
            
            # Extract active extruder from saved state
            try:
                active_extruder = saved_state['active_extruder']
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Found saved active extruder: {active_extruder}")
            except KeyError as e:
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Could not find active extruder in saved state: {e}")
                active_extruder = 'extruder'  # Default to first extruder
            
            # Extract chamber temperature from saved state
            try:
                chamber_temp = saved_state['chamber_temp']
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Found saved chamber temperature: {chamber_temp}")
            except KeyError as e:
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Could not find chamber temperature in saved state: {e}")
                chamber_temp = 0
            
            # Get file paths
            base_name, ext = os.path.splitext(input_file)
            backup_file = f"{base_name}{ext}.plr"
            
            # Rename original file to backup
            try:
                os.rename(input_file, backup_file)
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Renamed original file to {backup_file}")
            except Exception as e:
                if self.debug_mode:
                    self._debug_log(f"PowerLossRecovery: Error renaming original file: {str(e)}")
                return None
            
            if self.debug_mode:
                self._debug_log(f"PowerLossRecovery: Modifying {input_file} to resume from position {file_position}")
            
            # Define placeholder text
            PLACEHOLDER = ";;;;; PLR_RESUME - PRINT GCODE STARTS ;;;;;"
            
            found_placeholder = False
            current_position = 0
            
            with open(backup_file, 'r') as infile, open(input_file, 'w') as outfile:
                # Write restart G-code at the beginning of the file
                if self.restart_gcode_lines:
                    if self.debug_mode:
                        self._debug_log(f"Writing {len(self.restart_gcode_lines)} restart G-code lines")
                    
                    # Write extruder activation
                    extruder_cmd = f"ACTIVATE_EXTRUDER EXTRUDER={active_extruder}\n"
                    if self.debug_mode:
                        self._debug_log(f"Writing extruder activation: {extruder_cmd}")
                    outfile.write(extruder_cmd)
                    
                    # Write chamber temperature if > 0
                    if chamber_temp > 0 and self.chamber_heater_name:
                        chamber_cmd = f"SET_HEATER_TEMPERATURE HEATER={self.chamber_heater_name} TARGET={chamber_temp}\n"
                        if self.debug_mode:
                            self._debug_log(f"Writing chamber temperature: {chamber_cmd}")
                        outfile.write(chamber_cmd)
                    
                    # Write Z restoration
                    z_restore_gcode = f"G1 Z{saved_z:.3f} F3000 ; Restore Z height\n"
                    if self.debug_mode:
                        self._debug_log(f"Writing Z restore: {z_restore_gcode}")
                    outfile.write(z_restore_gcode)
                    
                    # Write all other restart G-code lines
                    for gcode_line in self.restart_gcode_lines:
                        if self.debug_mode:
                            self._debug_log(f"Writing line: {repr(gcode_line)}")
                        outfile.write(f"{gcode_line}\n")
                
                # Write placeholder
                outfile.write(f"{PLACEHOLDER}\n")
                
                # Copy content from original file starting from the resume position
                for line in infile:
                    current_position += len(line)
                    
                    # Skip everything before the resume position
                    if current_position < file_position:
                        continue
                    
                    # Write everything after the resume position
                    outfile.write(line)
            
            if self.debug_mode:
                self._debug_log(f"PowerLossRecovery: Successfully created modified file: {input_file}")
                self._debug_log(f"PowerLossRecovery: Original file backed up as: {backup_file}")
            
            return input_file
            
        except Exception as e:
            if self.debug_mode:
                self._debug_log(f"PowerLossRecovery: Error modifying gcode file: {str(e)}")
            # Attempt to restore original file if an error occurred
            try:
                if os.path.exists(input_file):
                    os.remove(input_file)
                if os.path.exists(backup_file):
                    os.rename(backup_file, input_file)
            except:
                pass
            return None
    
    def _restore_original_gcode(self, filename: str):
        """Restore the original gcode file after print completion or cancellation"""
        try:
            base_name, ext = os.path.splitext(filename)
            if ext == '.plr':  # Don't process files that are already backups
                return
                
            gcode_dir = self._get_gcode_dir()
            original_file = os.path.join(gcode_dir, filename)
            backup_file = f"{original_file}.plr"
            
            if os.path.exists(backup_file):
                if os.path.exists(original_file):
                    os.remove(original_file)
                os.rename(backup_file, original_file)
                if self.debug_mode:
                    logging.info(f"PowerLossRecovery: Restored original file: {original_file}")
        except Exception as e:
            if self.debug_mode:
                logging.info(f"PowerLossRecovery: Error restoring original file: {str(e)}")
    
    def _handle_print_complete(self, print_stats, eventtime):
        """Handle print completion or cancellation"""
        if not print_stats:
            return
            
        try:
            filename = print_stats.get_status(eventtime)['filename']
            if filename:
                self._restore_original_gcode(filename)
        except Exception as e:
            if self.debug_mode:
                logging.info(f"PowerLossRecovery: Error handling print complete: {str(e)}")