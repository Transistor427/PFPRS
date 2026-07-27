import logging
import os
import re
from collections import deque


def load_config(config):
    return PFPRS(config)


class PFPRS:
    LAYER_CHANGE_RE = re.compile(r'^;LAYER_CHANGE\b')
    LAYER_Z_RE = re.compile(r'^;Z:([0-9]+(?:\.[0-9]+)?)')
    TOOL_RE = re.compile(r'^T([0-9]+)\b')
    RESTORE_NAME = 'restore.gcode'

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.debug_mode = config.getboolean('debug_mode', False)
        self.dual = config.getboolean('dual', False)
        self.save_interval = config.getfloat(
            'save_interval', 15., minval=0., maxval=300.)
        self.history_size = config.getint(
            'history_size', 3, minval=1, maxval=20)
        self.save_delay = config.getint(
            'save_delay', 1, minval=0, maxval=max(0, self.history_size - 1))
        self.gcode_path = os.path.expanduser(
            config.get('gcode_path', '~/printer_data/gcodes'))
        self.restore_filename = config.get(
            'restore_filename', self.RESTORE_NAME)
        self.restart_macro = config.get(
            'restart_macro', '_START_PRINT_RESTORE')

        self.save_variables = None
        self.toolhead = None
        self.enabled = False
        self.resuming = False
        self.state_history = deque(maxlen=self.history_size)
        self.last_save_time = 0.
        self.timer = None
        self._print_was_active = False

        self.printer.register_event_handler('klippy:connect', self._handle_connect)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler(
            'klippy:disconnect', self._handle_disconnect)

        self.gcode.register_command(
            'PFPRS_ENABLE', self.cmd_PFPRS_ENABLE,
            desc=self.cmd_PFPRS_ENABLE_help)
        self.gcode.register_command(
            'PFPRS_DISABLE', self.cmd_PFPRS_DISABLE,
            desc=self.cmd_PFPRS_DISABLE_help)
        self.gcode.register_command(
            'PFPRS_SAVE_STATE', self.cmd_PFPRS_SAVE_STATE,
            desc=self.cmd_PFPRS_SAVE_STATE_help)
        self.gcode.register_command(
            'PFPRS_BUILD_RESTORE', self.cmd_PFPRS_BUILD_RESTORE,
            desc=self.cmd_PFPRS_BUILD_RESTORE_help)
        self.gcode.register_command(
            'PFPRS_QUERY_STATE', self.cmd_PFPRS_QUERY_STATE,
            desc=self.cmd_PFPRS_QUERY_STATE_help)
        self.gcode.register_command(
            'PFPRS_CLEAR', self.cmd_PFPRS_CLEAR,
            desc=self.cmd_PFPRS_CLEAR_help)

    def _log(self, msg):
        logging.info('PFPRS: %s' % (msg,))
        if self.debug_mode:
            self.gcode.respond_info('[PFPRS] %s' % (msg,))

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        try:
            self.save_variables = self.printer.lookup_object('save_variables')
        except self.printer.config_error:
            raise self.printer.config_error(
                'PFPRS requires [save_variables] in printer config')
        # Auto-detect second extruder if dual not forced
        if not self.dual:
            try:
                self.printer.lookup_object('extruder1')
                self.dual = True
                self._log('extruder1 detected, dual mode enabled')
            except Exception:
                pass

    def _handle_ready(self):
        if self.save_interval > 0:
            self.timer = self.reactor.register_timer(
                self._background_task, self.reactor.NOW)

    def _handle_disconnect(self):
        if self.timer is not None:
            self.reactor.unregister_timer(self.timer)
            self.timer = None

    def _is_printing(self):
        try:
            ps = self.printer.lookup_object('print_stats')
            status = ps.get_status(self.reactor.monotonic())
            return status.get('state') == 'printing'
        except Exception:
            return False

    def _background_task(self, eventtime):
        try:
            printing = self._is_printing()
            if printing and not self._print_was_active:
                self._print_was_active = True
                # During restore setup (resuming=True) wait for PFPRS_ENABLE
                # from _START_PRINT_RESTORE before saving again
                if not self.resuming:
                    self.enabled = True
                    self._log('print started, state saving enabled')
            elif not printing and self._print_was_active:
                self._print_was_active = False
                self.enabled = False
                self.resuming = False
                self._log('print finished, state saving disabled')
            if self.enabled and printing and not self.resuming:
                state = self._collect_state()
                if state is not None:
                    self.state_history.append(state)
                    if eventtime - self.last_save_time >= self.save_interval:
                        self._persist_delayed_state()
        except Exception:
            logging.exception('PFPRS: background task error')
        return eventtime + max(1., min(5., self.save_interval or 5.))

    def _get_gcode_position(self, eventtime):
        """Return XYZ in G-code coordinates (before skew / bed mesh)."""
        gcode_move = self.printer.lookup_object('gcode_move')
        status = gcode_move.get_status(eventtime)
        pos = status.get('gcode_position')
        if pos is None:
            # Fallback should be rare; still prefer gcode_move.position
            pos = status.get('position', [0., 0., 0.])
        return [float(pos[0]), float(pos[1]), float(pos[2])]

    def _collect_state(self):
        eventtime = self.reactor.monotonic()
        try:
            virtual_sdcard = self.printer.lookup_object('virtual_sdcard')
            print_stats = self.printer.lookup_object('print_stats')
            sd = virtual_sdcard.get_status(eventtime)
            ps = print_stats.get_status(eventtime)
            gcode_move = self.printer.lookup_object('gcode_move')
            gm = gcode_move.get_status(eventtime)

            filepath = sd.get('file_path') or ''
            filename = ps.get('filename') or ''
            file_position = int(sd.get('file_position', 0) or 0)
            file_size = int(sd.get('file_size', 0) or 0)

            # While printing restore.gcode, map offset back to the original
            # source so a second power loss can rebuild again correctly.
            if filepath and os.path.basename(filepath) == self.restore_filename:
                prev = self._get_saved_var('pr_file', None)
                if prev:
                    filepath = str(prev).strip().strip("'").strip('"')
                payload_start = int(
                    self._get_saved_var('pr_restore_payload_start', 0) or 0)
                source_base = int(
                    self._get_saved_var('pr_source_base', 0) or 0)
                if file_position >= payload_start:
                    file_position = source_base + (file_position - payload_start)
                else:
                    # Still in restore header / start macro — keep last source base
                    file_position = source_base
                filename = os.path.basename(filepath) if filepath else filename

            xyz = self._get_gcode_position(eventtime)

            extruder = self.printer.lookup_object('extruder')
            ext_st = extruder.get_status(eventtime)
            hotend_target = float(ext_st.get('target', 0.) or 0.)

            hotend1_target = 0.
            if self.dual:
                try:
                    e1 = self.printer.lookup_object('extruder1')
                    hotend1_target = float(
                        e1.get_status(eventtime).get('target', 0.) or 0.)
                except Exception:
                    pass

            bed_target = 0.
            try:
                bed = self.printer.lookup_object('heater_bed')
                bed_target = float(
                    bed.get_status(eventtime).get('target', 0.) or 0.)
            except Exception:
                pass

            chamber_target = 0.
            for name in ('heater_generic chamber', 'heater_generic Chamber'):
                try:
                    ch = self.printer.lookup_object(name)
                    chamber_target = float(
                        ch.get_status(eventtime).get('target', 0.) or 0.)
                    if chamber_target > 0:
                        break
                except Exception:
                    continue

            toolhead = self.printer.lookup_object('toolhead')
            active_ext = toolhead.get_extruder().get_name()

            fan_speed = 0.
            try:
                fan = self.printer.lookup_object('fan')
                fan_speed = float(
                    fan.get_status(eventtime).get('speed', 0.) or 0.)
            except Exception:
                pass

            return {
                'x': round(xyz[0], 3),
                'y': round(xyz[1], 3),
                'z': round(xyz[2], 3),
                'file_path': filepath,
                'filename': filename,
                'file_position': file_position,
                'file_size': file_size,
                't_ext': round(hotend_target, 1),
                't_ext1': round(hotend1_target, 1),
                't_bed': round(bed_target, 1),
                't_chamb': round(chamber_target, 1),
                'active_extruder': active_ext,
                'fan': round(fan_speed, 3),
                'absolute_coord': bool(gm.get('absolute_coordinates', True)),
                'absolute_extrude': bool(gm.get('absolute_extrude', False)),
                'collection_time': eventtime,
            }
        except Exception:
            logging.exception('PFPRS: state collection failed')
            return None

    def _save_variable(self, name, value):
        if isinstance(value, str):
            # Match existing PFPRS style: VALUE="'/path'"
            escaped = value.replace("'", "").replace('"', '')
            cmd = "SAVE_VARIABLE VARIABLE=%s VALUE=\"'%s'\"" % (name, escaped)
        elif isinstance(value, bool):
            cmd = 'SAVE_VARIABLE VARIABLE=%s VALUE=%s' % (
                name, 'True' if value else 'False')
        elif isinstance(value, float):
            cmd = 'SAVE_VARIABLE VARIABLE=%s VALUE=%.6f' % (name, value)
        else:
            cmd = 'SAVE_VARIABLE VARIABLE=%s VALUE=%s' % (name, value)
        self.gcode.run_script_from_command(cmd)

    def _get_saved_var(self, name, default=None):
        try:
            variables = self.save_variables.allVariables
            if name in variables:
                return variables[name]
        except Exception:
            pass
        return default

    def _persist_state(self, state):
        if state is None:
            return
        self._save_variable('pr_x', float(state['x']))
        self._save_variable('pr_y', float(state['y']))
        self._save_variable('pr_z', float(state['z']))
        self._save_variable('pr_file_pos', int(state['file_position']))
        self._save_variable('pr_t_ext', float(state['t_ext']))
        self._save_variable('pr_t_bed', float(state['t_bed']))
        self._save_variable('pr_t_chamb', float(state['t_chamb']))
        self._save_variable('pr_fan', float(state['fan']))
        self._save_variable('pr_abs_extrude', bool(state['absolute_extrude']))
        if state.get('file_path'):
            self._save_variable('pr_file', state['file_path'])
        if self.dual:
            self._save_variable('pr_t_ext1', float(state['t_ext1']))
            self._save_variable(
                'pr_act_ext', state.get('active_extruder', 'extruder'))
        self.last_save_time = self.reactor.monotonic()
        if self.debug_mode:
            self._log(
                'saved X=%.3f Y=%.3f Z=%.3f pos=%d file=%s' % (
                    state['x'], state['y'], state['z'],
                    state['file_position'],
                    os.path.basename(state.get('file_path') or '')))

    def _persist_delayed_state(self):
        if len(self.state_history) <= self.save_delay:
            return
        history = list(self.state_history)
        idx = -(self.save_delay + 1)
        self._persist_state(history[idx])

    cmd_PFPRS_ENABLE_help = 'Enable PFPRS state saving'
    def cmd_PFPRS_ENABLE(self, gcmd):
        self.resuming = False
        self.enabled = True
        gcmd.respond_info('PFPRS enabled')

    cmd_PFPRS_DISABLE_help = 'Disable PFPRS state saving'
    def cmd_PFPRS_DISABLE(self, gcmd):
        self.enabled = False
        gcmd.respond_info('PFPRS disabled')

    cmd_PFPRS_SAVE_STATE_help = 'Save current print state (gcode XY before skew)'
    def cmd_PFPRS_SAVE_STATE(self, gcmd):
        if self.resuming:
            gcmd.respond_info('PFPRS: skip save during resume')
            return
        state = self._collect_state()
        if state is None:
            raise gcmd.error('PFPRS: unable to collect printer state')
        z_override = gcmd.get_float('Z', None)
        if z_override is not None:
            state['z'] = round(float(z_override), 3)
        self.state_history.append(state)
        self._persist_state(state)
        gcmd.respond_info(
            'PFPRS: state saved X=%.3f Y=%.3f Z=%.3f' % (
                state['x'], state['y'], state['z']))

    cmd_PFPRS_QUERY_STATE_help = 'Show last saved PFPRS state'
    def cmd_PFPRS_QUERY_STATE(self, gcmd):
        x = self._get_saved_var('pr_x', None)
        y = self._get_saved_var('pr_y', None)
        z = self._get_saved_var('pr_z', None)
        path = self._get_saved_var('pr_file', None)
        pos = self._get_saved_var('pr_file_pos', None)
        gcmd.respond_info(
            'PFPRS state:\n'
            '  X/Y/Z: %s / %s / %s\n'
            '  file_pos: %s\n'
            '  file: %s' % (x, y, z, pos, path))

    cmd_PFPRS_CLEAR_help = 'Clear in-memory PFPRS history'
    def cmd_PFPRS_CLEAR(self, gcmd):
        self.state_history.clear()
        gcmd.respond_info('PFPRS history cleared')

    cmd_PFPRS_BUILD_RESTORE_help = 'Build restore.gcode from saved file offset'
    def cmd_PFPRS_BUILD_RESTORE(self, gcmd):
        src = self._resolve_source_file(gcmd)
        file_pos = int(self._get_saved_var('pr_file_pos', 0) or 0)
        z_height = self._get_saved_var('pr_z', None)

        resume_pos = file_pos
        if resume_pos <= 0 and z_height is not None:
            # Fallback for old saves without file offset
            resume_pos = self._find_layer_pos_by_z(src, float(z_height))

        if resume_pos is None or resume_pos < 0:
            raise gcmd.error(
                'PFPRS: cannot locate resume position in gcode '
                '(need pr_file_pos or OrcaSlicer ;Z: marker)')

        context = self._scan_prefix_context(src, resume_pos)
        out_path = os.path.join(self.gcode_path, self.restore_filename)
        self.resuming = True
        self.enabled = False
        self._write_restore_file(src, out_path, resume_pos, context)
        thumb_n = len(self._extract_thumbnails(src))
        gcmd.respond_info(
            'PFPRS: wrote %s (offset=%d, tool=T%s, thumbnails=%d)' % (
                self.restore_filename, resume_pos,
                context.get('tool', 0), thumb_n))

    def _resolve_source_file(self, gcmd):
        path = gcmd.get('GCODE_FILE', None)
        if not path:
            path = self._get_saved_var('pr_file', None)
        if path is None or str(path) in ('None', ''):
            raise gcmd.error('PFPRS: source gcode file not found in variables')
        path = str(path).strip().strip("'").strip('"')
        if not os.path.isabs(path):
            path = os.path.join(self.gcode_path, path)
        if not os.path.isfile(path):
            raise gcmd.error('PFPRS: gcode file does not exist: %s' % (path,))
        if os.path.basename(path) == self.restore_filename:
            raise gcmd.error('PFPRS: refusing to use restore.gcode as source')
        return path

    def _find_layer_pos_by_z(self, filepath, z_height):
        """Fallback: match OrcaSlicer ;Z: with float compare (1 == 1.0)."""
        target = float(z_height)
        with open(filepath, 'rb') as f:
            while True:
                pos = f.tell()
                raw = f.readline()
                if not raw:
                    break
                line = raw.decode('utf-8', 'ignore').strip()
                m = self.LAYER_Z_RE.match(line)
                if not m:
                    continue
                try:
                    z_val = float(m.group(1))
                except ValueError:
                    continue
                if abs(z_val - target) < 0.0005:
                    return self._rewind_to_layer_change(filepath, pos)
        return None

    def _rewind_to_layer_change(self, filepath, z_comment_pos):
        window = 4096
        start = max(0, z_comment_pos - window)
        with open(filepath, 'rb') as f:
            f.seek(start)
            data = f.read(z_comment_pos - start + 64)
        idx = data.rfind(b';LAYER_CHANGE')
        if idx < 0:
            return z_comment_pos
        return start + idx

    def _scan_prefix_context(self, filepath, end_pos):
        tool = 0
        absolute_extrude = False
        absolute_coord = True
        last_fan = None
        with open(filepath, 'rb') as f:
            data = f.read(max(0, int(end_pos)))
        for raw in data.splitlines():
            line = raw.decode('utf-8', 'ignore').strip()
            if not line or line.startswith(';'):
                continue
            tm = self.TOOL_RE.match(line)
            if tm:
                tool = int(tm.group(1))
                continue
            upper = line.upper()
            if upper.startswith('M82'):
                absolute_extrude = True
            elif upper.startswith('M83'):
                absolute_extrude = False
            elif upper.startswith('G90'):
                absolute_coord = True
            elif upper.startswith('G91'):
                absolute_coord = False
            elif upper.startswith('M106'):
                last_fan = line
            elif upper.startswith('M107'):
                last_fan = 'M107'
        return {
            'tool': tool,
            'absolute_extrude': absolute_extrude,
            'absolute_coord': absolute_coord,
            'fan_line': last_fan,
        }

    def _extract_thumbnails(self, filepath):
        """Copy OrcaSlicer / Prusa-style thumbnail comment blocks from source."""
        blocks = []
        in_thumbnail_block = False
        in_thumbnail = False
        current = []
        # Thumbnails live in the file header; stop once executable gcode starts
        max_header = 2 * 1024 * 1024
        with open(filepath, 'rb') as f:
            data = f.read(max_header)
        for raw in data.splitlines(True):
            line = raw.decode('utf-8', 'ignore')
            stripped = line.strip()
            upper = stripped.upper()

            if upper == '; THUMBNAIL_BLOCK_START':
                in_thumbnail_block = True
                current = [raw]
                continue
            if in_thumbnail_block:
                current.append(raw)
                if upper == '; THUMBNAIL_BLOCK_END':
                    blocks.append(b''.join(current))
                    current = []
                    in_thumbnail_block = False
                continue

            # Fallback: classic "; thumbnail begin" … "; thumbnail end"
            if (not in_thumbnail and stripped.lower().startswith('; thumbnail begin')):
                in_thumbnail = True
                current = [raw]
                continue
            if in_thumbnail:
                current.append(raw)
                if stripped.lower().startswith('; thumbnail end'):
                    blocks.append(b''.join(current))
                    current = []
                    in_thumbnail = False
                continue

            if upper == '; EXECUTABLE_BLOCK_START':
                break
            # Stop at first real gcode command in header scan
            if stripped and not stripped.startswith(';'):
                break

        return blocks

    def _write_restore_file(self, src, out_path, resume_pos, context):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        header = [
            '; PFPRS restore file',
            '; source: %s' % (src,),
            '; resume_byte: %d' % (resume_pos,),
            '; XY restored via %s using gcode_position (pre-skew)' % (
                self.restart_macro,),
            self.restart_macro,
            'G90' if context.get('absolute_coord', True) else 'G91',
            'M82' if context.get('absolute_extrude', False) else 'M83',
        ]
        # Tool select only for dual / multi-extruder printers
        if self.dual:
            header.append('T%d' % (int(context.get('tool', 0)),))
        if context.get('fan_line'):
            header.append(context['fan_line'])
        header.append('G92 E0')
        header.append('; --- resumed gcode ---')

        thumbnails = self._extract_thumbnails(src)

        with open(src, 'rb') as infile, open(out_path, 'wb') as outfile:
            for block in thumbnails:
                outfile.write(block)
                if not block.endswith(b'\n'):
                    outfile.write(b'\n')
            if thumbnails:
                outfile.write(b'\n')
            for line in header:
                outfile.write((line + '\n').encode('utf-8'))
            payload_start = outfile.tell()
            if resume_pos <= 0:
                infile.seek(0)
            else:
                infile.seek(resume_pos - 1)
                prev = infile.read(1)
                if prev not in (b'\n', b'\r'):
                    infile.readline()
            while True:
                chunk = infile.read(1024 * 1024)
                if not chunk:
                    break
                outfile.write(chunk)

        # Map restore.gcode offsets back to the original source on later saves
        self._save_variable('pr_restore_payload_start', int(payload_start))
        self._save_variable('pr_source_base', int(resume_pos))
        if src:
            self._save_variable('pr_file', src)
