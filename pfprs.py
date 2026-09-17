# PFPRS - Power Failure Print Recovery System
#
# Восстановление печати после отключения питания для Klipper.
#
# Ключевые отличия от предыдущих версий:
#   * состояние пишется атомарно в собственный JSON (двойная буферизация),
#     а не 12 раз подряд через SAVE_VARIABLE;
#   * файл печати не копируется - возобновление идёт по оригиналу через
#     virtual_sdcard (эквивалент M23 + M26 + M24);
#   * контекст печати (E, F, M220, M221, вентилятор, сетка стола, offsets)
#     снимается из живого статуса Klipper, а не сканированием префикса файла;
#   * точка восстановления инвалидируется при штатном завершении печати;
#   * автоматическое возобновление по разнице температур хотенда.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import json
import logging
import os
import time
from collections import deque

STATE_VERSION = 3


def load_config(config):
    return PFPRS(config)


class AtomicStore:
    """Двухслотовое атомарное JSON-хранилище.

    Слоты пишутся по очереди. Запись идёт во временный файл, который
    fsync-ается до атомарного os.replace(). Обрыв питания способен испортить
    только слот, который писался в этот момент - второй слот содержит
    предыдущее целое поколение.
    """

    def __init__(self, base_path):
        self.base_path = base_path
        self.slots = ('%s.0.json' % (base_path,), '%s.1.json' % (base_path,))
        self.seq = 0
        self.next_slot = 0
        self.rejected = 0

    def load(self, validator=None):
        """Вернуть новейшее ПРИГОДНОЕ поколение.

        Если самая свежая запись не проходит проверку (например, её успели
        сделать уже после выключения нагревателей), откатываемся на
        предыдущий слот - ровно ради этого и держатся два.
        """
        gens = []
        for idx, slot in enumerate(self.slots):
            try:
                with open(slot, 'rb') as f:
                    data = json.loads(f.read().decode('utf-8'))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            seq = data.get('seq')
            if not isinstance(seq, int):
                continue
            gens.append((seq, idx, data))
        if not gens:
            self.seq = 0
            self.next_slot = 0
            return None
        gens.sort(key=lambda g: g[0], reverse=True)
        logging.info('PFPRS: store generations on disk: %s',
                     ', '.join('slot%d seq=%s pos=%s valid=%s saved_at=%.0f'
                               % (i, sq, d.get('file_position'),
                                  d.get('valid'), d.get('saved_at', 0.) or 0.)
                               for sq, i, d in gens))
        # счётчик продолжаем от самого свежего, иначе поколения столкнутся
        self.seq = gens[0][0]
        for seq, idx, data in gens:
            if validator is None or validator(data):
                logging.info('PFPRS: store chose slot%d seq=%s pos=%s',
                             idx, seq, data.get('file_position'))
                # писать будем в другой слот, чтобы выбранное поколение
                # пережило ещё одну запись
                self.next_slot = (idx + 1) % 2
                self.rejected = len(gens) - 1 if idx != gens[0][1] else 0
                return data
        self.next_slot = (gens[0][1] + 1) % 2
        return gens[0][2]

    def save(self, data):
        self.seq += 1
        payload = dict(data)
        payload['seq'] = self.seq
        blob = json.dumps(
            payload, ensure_ascii=False, sort_keys=True).encode('utf-8')
        slot = self.slots[self.next_slot]
        tmp = '%s.tmp' % (slot,)
        directory = os.path.dirname(slot) or '.'
        with open(tmp, 'wb') as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, slot)
        self._sync_dir(directory)
        self.next_slot ^= 1

    @staticmethod
    def _sync_dir(directory):
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


class PFPRS:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        self.debug_mode = config.getboolean('debug_mode', False)
        self.dual = config.getboolean('dual', False)

        # --- периодичность ---
        self.sample_interval = config.getfloat(
            'sample_interval', 1., minval=.25, maxval=10.)
        self.save_interval = config.getfloat(
            'save_interval', 5., minval=1., maxval=300.)
        # На сколько секунд назад откатывать точку возобновления, чтобы
        # компенсировать опережение look-ahead буфера Klipper. Перекрытие
        # безопаснее пропуска материала.
        self.resume_lag = config.getfloat(
            'resume_lag', 2., minval=0., maxval=60.)
        self.resume_timeout = config.getfloat(
            'resume_timeout', 1800., minval=60., maxval=7200.)

        # --- пути ---
        self.gcode_path = os.path.expanduser(
            config.get('gcode_path', '~/printer_data/gcodes'))
        state_path = os.path.expanduser(
            config.get('state_path', '~/printer_data/pfprs/state'))
        self._ensure_dir(os.path.dirname(state_path))
        self.store = AtomicStore(state_path)

        # --- имена макросов и объектов ---
        self.restart_macro = config.get('restart_macro', '_START_PRINT_RESTORE')
        self.prompt_macro = config.get('prompt_macro', '_PFPRS_PROMPT_RECOVERY')
        self.auto_macro = config.get('auto_resume_macro', '_PFPRS_AUTO_RESUME')
        # Необязательный крючок для машинно-специфичных действий: вызывается
        # последним, уже после восстановления контекста печати и прямо перед
        # тем, как файл пойдёт дальше. Сюда удобно вписать SKEW и подобное,
        # что на вашей машине делают макросы смены инструмента.
        self.post_restore_macro = config.get('post_restore_macro', '')
        self.chamber_heater = config.get('chamber_heater', 'chamber')
        self.part_fan = config.get('part_fan', 'fan')
        self.bed_mesh_fallback = config.get('bed_mesh_fallback_profile', '')

        # --- поведение восстановления ---
        self.verify_source = config.getboolean('verify_source', True)
        self.restore_gcode_offset = config.getboolean(
            'restore_gcode_offset', True)
        self.arm_emergency_homing = config.getboolean(
            'arm_emergency_homing', True)
        self.z_hop = config.getfloat('z_hop', 5., minval=0., maxval=50.)
        self.z_lift_speed = config.getfloat('z_lift_speed', 600., above=0.)
        self.travel_speed = config.getfloat('travel_speed', 6000., above=0.)
        self.prime_length = config.getfloat('prime_length', 20., minval=0.)
        self.prime_speed = config.getfloat('prime_speed', 2100., above=0.)
        # Ретракт после прайма: без него выдавленные миллиметры тянутся
        # ниткой через деталь при переезде в точку останова.
        self.prime_retract = config.getfloat(
            'prime_retract', 1., minval=0., maxval=20.)
        self.safe_home_temp = config.getfloat(
            'safe_home_temp', 200., minval=0., maxval=400.)
        self.min_active_temp = config.getfloat(
            'min_active_temp', 150., minval=0., maxval=400.)

        # --- автовозобновление ---
        self.auto_resume_default = config.getboolean('auto_resume', False)
        self.auto_resume_temp_delta = config.getfloat(
            'auto_resume_temp_delta', 20., minval=0., maxval=200.)
        self.auto_resume_delay = config.getfloat(
            'auto_resume_delay', 5., minval=0., maxval=600.)
        self.auto_resume_min_target = config.getfloat(
            'auto_resume_min_target', 100., minval=0., maxval=400.)
        self.auto_resume_after_shutdown = config.getboolean(
            'auto_resume_after_shutdown', False)
        self.auto_resume_max_age = config.getfloat(
            'auto_resume_max_age', 0., minval=0.)
        # Нажатие кнопки аварийного останова - почти всегда осознанное
        # решение оператора, а не сбой питания. Такую остановку не нужно
        # предлагать восстанавливать вовсе.
        self.estop_discards = config.getboolean(
            'emergency_stop_discards', True)
        self.estop_markers = [
            m.strip().lower()
            for m in config.get('emergency_stop_markers',
                                'M112, emergency stop').split(',')
            if m.strip()]

        # --- устаревшие опции (читаются, чтобы старые printer.cfg грузились) ---
        config.getint('history_size', 3, minval=1, maxval=200)
        config.getint('save_delay', 1, minval=0, maxval=200)
        config.get('restore_filename', 'restore.gcode')
        config.get('moonraker_url', '')

        # --- runtime ---
        self.state = None
        self.samples = deque()
        self.max_samples = int(self.resume_lag / self.sample_interval) + 4
        self.enabled = False
        self.resuming = False
        self.resume_armed = False
        self.resume_deadline = 0.
        self.resume_position = 0
        self.emergency_homing = False
        self.z_calibrated = False
        self.z_calibrated_value = 0.
        self.auto_resume = self.auto_resume_default
        self.print_active = False
        # Счётчик заморозки: пока > 0, точка восстановления не обновляется.
        # Нужен на время, когда голова уходит с траектории печати - смена
        # инструмента, парковка для таймлапса, замена филамента. Снимок,
        # сделанный в такой момент, указывает на парковку и невосстановим.
        self.hold = 0
        self.last_persist = 0.
        self.timer = None
        self.recovery_timer = None
        self.recovery_available = False
        self.recovery_auto = False
        self.recovery_reason = ''
        self.recovery_target = 0.
        self.recovery_actual = 0.
        self.recovery_drop = 0.
        self.last_macro_error = ''

        self.printer.register_event_handler(
            'klippy:connect', self._handle_connect)
        self.printer.register_event_handler(
            'klippy:ready', self._handle_ready)
        self.printer.register_event_handler(
            'klippy:disconnect', self._handle_disconnect)
        self.printer.register_event_handler(
            'klippy:shutdown', self._handle_shutdown)
        # RESTART / FIRMWARE_RESTART гасят нагреватели и паркуют голову ДО
        # завершения процесса. Очередной тик успел бы записать это как
        # точку восстановления - замораживаем сохранение сразу.
        self.printer.register_event_handler(
            'gcode:request_restart', self._handle_request_restart)

        for name in ('PFPRS_ENABLE', 'PFPRS_DISABLE', 'PFPRS_SAVE_STATE',
                     'PFPRS_STATUS', 'PFPRS_DISCARD', 'PFPRS_PREPARE_RESUME',
                     'PFPRS_BEGIN_RESUME', 'PFPRS_ABORT_RESUME',
                     'PFPRS_SET_AUTO_RESUME', 'PFPRS_SET_Z_CALIBRATED',
                     'PFPRS_SET_EMERGENCY_HOMING', 'PFPRS_CHECK_RECOVERY',
                     'PFPRS_HOLD', 'PFPRS_RELEASE'):
            self.gcode.register_command(
                name, getattr(self, 'cmd_%s' % (name,)),
                desc=getattr(self, 'cmd_%s_help' % (name,)))
        # Обратная совместимость с прежними именами команд.
        self.gcode.register_command(
            'PFPRS_QUERY_STATE', self.cmd_PFPRS_STATUS,
            desc='Псевдоним PFPRS_STATUS')
        self.gcode.register_command(
            'PFPRS_CLEAR', self.cmd_PFPRS_DISCARD,
            desc='Псевдоним PFPRS_DISCARD')

    # ------------------------------------------------------------------
    # Служебное
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_dir(path):
        if path:
            try:
                os.makedirs(path, exist_ok=True)
            except OSError:
                logging.exception('PFPRS: cannot create %s', path)

    def _log(self, msg):
        logging.info('PFPRS: %s', msg)
        if self.debug_mode:
            self.gcode.respond_info('[PFPRS] %s' % (msg,))

    def _lookup(self, name):
        return self.printer.lookup_object(name, None)

    def _run_macro(self, name, fallback=None, from_command=False):
        """Выполнить макрос. Если он не определён - хотя бы сказать словами:
        молчание в момент, когда печать сорвалась, недопустимо.

        from_command=True, если вызов идёт изнутри обработчика G-кода: там
        мьютекс gcode уже удерживается, и run_script() встал бы намертво.
        """
        self.last_macro_error = ''
        if name and self._lookup('gcode_macro %s' % (name,)) is not None:
            try:
                if from_command:
                    self.gcode.run_script_from_command(name)
                else:
                    self.gcode.run_script(name)
                return True
            except Exception as e:
                logging.exception('PFPRS: macro %s failed', name)
                self.last_macro_error = str(e).strip().splitlines()[0][:200]
                self.gcode.respond_info('PFPRS: ошибка в %s: %s' % (name, e))
                return False
        logging.warning('PFPRS: macro %s not found', name)
        self.last_macro_error = 'макрос %s не определён' % (name,)
        if fallback:
            self.gcode.respond_info(fallback)
        return False

    # ------------------------------------------------------------------
    # События Klipper
    # ------------------------------------------------------------------
    def _handle_connect(self):
        if not self.dual and self._lookup('extruder1') is not None:
            self.dual = True
            self._log('extruder1 detected, dual mode enabled')
        try:
            data = self.store.load(self._state_usable)
        except Exception:
            logging.exception('PFPRS: state load failed')
            data = None
        if data is not None:
            self.state = data
            self.auto_resume = bool(data.get('auto_resume',
                                             self.auto_resume_default))
            self._log('state loaded (seq=%s, valid=%s)' % (
                data.get('seq'), data.get('valid')))

    def _handle_ready(self):
        self.timer = self.reactor.register_timer(
            self._background_task, self.reactor.NOW)
        st = self.state
        if st and st.get('valid'):
            if self.arm_emergency_homing:
                # До явного решения пользователя ось Z парковать нельзя -
                # сопло стоит внутри детали.
                self.emergency_homing = True
            self.recovery_timer = self.reactor.register_timer(
                self._recovery_check,
                self.reactor.monotonic() + self.auto_resume_delay)

    def _handle_disconnect(self):
        for attr in ('timer', 'recovery_timer'):
            timer = getattr(self, attr)
            if timer is not None:
                self.reactor.unregister_timer(timer)
                setattr(self, attr, None)

    def _handle_request_restart(self, print_time):
        if self.enabled:
            self._log('restart requested, freezing recovery point')
        self.enabled = False

    def _handle_shutdown(self):
        # M112, ошибка MCU или иная аварийная остановка. Автовозобновление
        # после такого события по умолчанию запрещено: причина остановки
        # могла быть не связана с питанием.
        reason = 'klippy shutdown'
        try:
            msg = self.printer.get_state_message()
            if isinstance(msg, tuple):
                msg = msg[0]
            if msg:
                reason = str(msg).strip().splitlines()[0][:160]
        except Exception:
            pass
        st = dict(self.state or {})
        if not st:
            return
        st['blocked'] = True
        st['shutdown_reason'] = reason
        if self.estop_discards and self._is_operator_stop(reason):
            # Кнопка аварийного останова: восстанавливать нечего, оператор
            # остановил печать намеренно. Ни автомата, ни диалога.
            st['valid'] = False
            st['invalid_reason'] = 'аварийный останов оператором'
            self._log('emergency stop by operator (%s), '
                      'recovery point discarded' % (reason,))
        self._write(st)

    def _is_operator_stop(self, reason):
        low = (reason or '').lower()
        return any(m in low for m in self.estop_markers)

    # ------------------------------------------------------------------
    # Сбор состояния
    # ------------------------------------------------------------------
    def _print_state(self, eventtime):
        ps = self._lookup('print_stats')
        if ps is None:
            return 'standby'
        try:
            return ps.get_status(eventtime).get('state') or 'standby'
        except Exception:
            return 'standby'

    def _heater_pair(self, name, eventtime):
        obj = self._lookup(name)
        if obj is None:
            return 0., 0.
        try:
            status = obj.get_status(eventtime)
            return (float(status.get('target', 0.) or 0.),
                    float(status.get('temperature', 0.) or 0.))
        except Exception:
            return 0., 0.

    def _collect(self, eventtime):
        vsd = self._lookup('virtual_sdcard')
        ps = self._lookup('print_stats')
        gm = self._lookup('gcode_move')
        th = self._lookup('toolhead')
        if None in (vsd, ps, gm, th):
            return None
        try:
            sd = vsd.get_status(eventtime)
            stats = ps.get_status(eventtime)
            move = gm.get_status(eventtime)
        except Exception:
            logging.exception('PFPRS: status read failed')
            return None

        path = sd.get('file_path') or ''
        name = stats.get('filename') or ''
        if not path or not name:
            return None
        try:
            fstat = os.stat(path)
        except OSError:
            return None

        gpos = list(move.get('gcode_position') or (0., 0., 0., 0.))
        while len(gpos) < 4:
            gpos.append(0.)
        kpos = list(th.get_position())
        while len(kpos) < 4:
            kpos.append(0.)
        origin = list(move.get('homing_origin') or (0., 0., 0., 0.))
        while len(origin) < 4:
            origin.append(0.)

        # gcode_move.speed - мм/с с уже применённым M220; speed_factor в
        # статусе равен S/100. Восстанавливаем исходный F в мм/мин.
        speed_factor = float(move.get('speed_factor', 1.) or 1.)
        speed = float(move.get('speed', 0.) or 0.)
        feedrate = speed * 60. / speed_factor if speed_factor > 0 else 0.

        t_ext, t_ext_now = self._heater_pair('extruder', eventtime)
        t_ext1, t_ext1_now = (0., 0.)
        if self.dual:
            t_ext1, t_ext1_now = self._heater_pair('extruder1', eventtime)
        t_bed, _ = self._heater_pair('heater_bed', eventtime)
        t_chamb = 0.
        if self.chamber_heater:
            t_chamb, _ = self._heater_pair(
                'heater_generic %s' % (self.chamber_heater,), eventtime)

        fan_speed = 0.
        fan = self._lookup(self.part_fan)
        if fan is None and self.part_fan == 'fan':
            fan = self._lookup('fan_generic fan')
        if fan is not None:
            try:
                fan_speed = float(
                    fan.get_status(eventtime).get('speed', 0.) or 0.)
            except Exception:
                pass

        mesh_profile = ''
        bed_mesh = self._lookup('bed_mesh')
        if bed_mesh is not None:
            try:
                bms = bed_mesh.get_status(eventtime)
                mesh_name = bms.get('profile_name') or ''
                # BED_MESH_CALIBRATE ADAPTIVE=1 создаёт временный профиль с
                # именем вида adaptive-XXXXXXXX, которого нет среди
                # сохранённых: LOAD по нему завершится ошибкой. Берём имя,
                # только если профиль реально сохранён, иначе положимся на
                # mesh_comp.
                if mesh_name and mesh_name in (bms.get('profiles') or {}):
                    mesh_profile = mesh_name
                elif mesh_name:
                    self._log('bed mesh profile %s не сохранён, '
                              'будет применена поправка mesh_comp'
                              % (mesh_name,))
            except Exception:
                pass

        active = th.get_extruder().get_name()
        tool = 0
        if active.startswith('extruder') and active != 'extruder':
            try:
                tool = int(active[len('extruder'):])
            except ValueError:
                tool = 0

        info = stats.get('info') or {}

        return {
            'file_path': path,
            'file_name': name,
            'file_size': int(fstat.st_size),
            'file_mtime': float(fstat.st_mtime),
            'file_position': int(sd.get('file_position', 0) or 0),
            'x': round(float(gpos[0]), 4),
            'y': round(float(gpos[1]), 4),
            'z': round(float(gpos[2]), 4),
            'e': round(float(gpos[3]), 5),
            'kin_x': round(float(kpos[0]), 4),
            'kin_y': round(float(kpos[1]), 4),
            'kin_z': round(float(kpos[2]), 4),
            'offset_x': round(float(origin[0]), 4),
            'offset_y': round(float(origin[1]), 4),
            'offset_z': round(float(origin[2]), 4),
            # Поправка сетки стола в точке останова. toolhead.get_position()
            # уже прошёл трансформацию bed_mesh, gcode_position - нет.
            # Нужна, когда сетка адаптивная и восстановить её по имени нельзя.
            'mesh_comp': round(
                float(kpos[2]) - float(gpos[2]) - float(origin[2]), 4),
            'speed': round(feedrate, 1),
            'speed_factor': round(speed_factor, 4),
            'extrude_factor': round(
                float(move.get('extrude_factor', 1.) or 1.), 4),
            'fan': round(fan_speed, 4),
            't_ext': round(t_ext, 1),
            't_ext_actual': round(t_ext_now, 1),
            't_ext1': round(t_ext1, 1),
            't_ext1_actual': round(t_ext1_now, 1),
            't_bed': round(t_bed, 1),
            't_chamb': round(t_chamb, 1),
            'active_extruder': active,
            'tool': tool,
            'bed_mesh': mesh_profile,
            'total_layer': info.get('total_layer') or 0,
            'current_layer': info.get('current_layer') or 0,
            'absolute_coord': bool(move.get('absolute_coordinates', True)),
            'absolute_extrude': bool(move.get('absolute_extrude', False)),
        }

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------
    @staticmethod
    def _plausible(data):
        """Печать с выключенным соплом невозможна. Такой снимок означает,
        что состояние поймано в момент штатной остановки, когда Klipper уже
        погасил нагреватели, - восстанавливать по нему нечего."""
        name = data.get('active_extruder') or 'extruder'
        key = 't_ext1' if name == 'extruder1' else 't_ext'
        return float(data.get(key, 0.) or 0.) > 0.

    def _state_usable(self, data):
        # Снятую намеренно точку принимаем как есть: она и должна победить
        # более старое valid=True, иначе воскреснет отменённая печать.
        if not data.get('valid'):
            return True
        return self._plausible(data)

    def _write(self, data):
        payload = dict(data)
        payload['version'] = STATE_VERSION
        payload['auto_resume'] = bool(self.auto_resume)
        slot = self.store.next_slot
        try:
            self.store.save(payload)
        except Exception:
            logging.exception('PFPRS: failed to persist state')
            return False
        self.state = payload
        logging.info('PFPRS: state written slot%d seq=%d pos=%s valid=%s',
                     slot, self.store.seq, payload.get('file_position'),
                     payload.get('valid'))
        return True

    def _persist(self, snap):
        if not self._plausible(snap):
            self._log('снимок с нулевой целевой температурой не сохранён')
            return
        data = dict(snap)
        data['valid'] = True
        data['blocked'] = False
        data['shutdown_reason'] = ''
        data['invalid_reason'] = ''
        data['saved_at'] = time.time()
        if self._write(data):
            self.last_persist = self.reactor.monotonic()
            if self.debug_mode:
                self._log('saved X=%.2f Y=%.2f Z=%.2f pos=%d' % (
                    data['x'], data['y'], data['z'], data['file_position']))

    def _invalidate(self, reason):
        if not self.state or not self.state.get('valid'):
            return
        data = dict(self.state)
        data['valid'] = False
        data['invalid_reason'] = reason
        self._write(data)
        self._log('recovery point discarded: %s' % (reason,))

    def _pick_lagged(self, eventtime):
        if not self.samples:
            return None
        cutoff = eventtime - self.resume_lag
        chosen = self.samples[0][1]
        for ts, snap in self.samples:
            if ts > cutoff:
                break
            chosen = snap
        return chosen

    # ------------------------------------------------------------------
    # Фоновая задача
    # ------------------------------------------------------------------
    def _background_task(self, eventtime):
        try:
            self._tick(eventtime)
        except Exception:
            logging.exception('PFPRS: background task error')
        return eventtime + self.sample_interval

    def _tick(self, eventtime):
        if (self.resume_armed and self.resume_deadline
                and eventtime > self.resume_deadline):
            self._log('resume was not completed in time, disarming')
            self._clear_resume()

        state = self._print_state(eventtime)
        active = state in ('printing', 'paused')
        if active and not self.print_active:
            self.print_active = True
            self.samples.clear()
            if not self.resuming:
                self.enabled = True
                self.emergency_homing = False
                self._log('print started, protection enabled')
        elif not active and self.print_active:
            self.print_active = False
            self.enabled = False
            self.hold = 0
            # Если Klipper дожил до того, чтобы увидеть остановку печати,
            # значит питание не пропадало: это штатное завершение, отмена
            # или сброс файла оператором. Точку восстановления снимаем,
            # иначе принтер может сам возобновить отменённую печать.
            # Состояния paused и error сознательно не трогаем: из них
            # восстановление осмысленно.
            reasons = {'complete': 'печать завершена',
                       'cancelled': 'печать отменена',
                       'standby': 'печать остановлена оператором'}
            if state in reasons and not self.resuming:
                self._invalidate(reasons[state])
            self._log('print stopped (%s)' % (state,))

        if state != 'printing' or not self.enabled or self.resuming:
            return
        if self.hold > 0:
            # Голова сейчас не на траектории печати - точку не трогаем,
            # предыдущая остаётся действительной.
            return
        snap = self._collect(eventtime)
        if snap is None:
            return
        self.samples.append((eventtime, snap))
        while len(self.samples) > self.max_samples:
            self.samples.popleft()
        if eventtime - self.last_persist >= self.save_interval:
            lagged = self._pick_lagged(eventtime)
            if lagged is not None:
                self._persist(lagged)

    # ------------------------------------------------------------------
    # Проверка источника и позиции
    # ------------------------------------------------------------------
    def _verify_source(self, st):
        path = st.get('file_path') or ''
        if not path:
            return False, 'путь к файлу печати не сохранён'
        if not os.path.isfile(path):
            return False, 'файл %s не найден' % (os.path.basename(path),)
        size = int(st.get('file_size', 0) or 0)
        if self.verify_source:
            try:
                fstat = os.stat(path)
            except OSError as e:
                return False, 'нет доступа к файлу: %s' % (e,)
            if size and int(fstat.st_size) != size:
                return False, ('файл изменился после прерывания '
                               '(размер %d вместо %d)'
                               % (int(fstat.st_size), size))
            saved_mtime = float(st.get('file_mtime', 0.) or 0.)
            if saved_mtime and abs(float(fstat.st_mtime) - saved_mtime) > 1.:
                return False, 'файл изменился после прерывания (дата правки)'
        pos = int(st.get('file_position', 0) or 0)
        if pos <= 0:
            return False, 'позиция в файле не сохранена'
        if size and pos >= size:
            return False, 'сохранённая позиция за пределами файла'
        return True, ''

    def _align_offset(self, path, pos):
        """Сдвинуть смещение на начало следующей полной строки."""
        size = os.path.getsize(path)
        pos = max(0, min(int(pos), size))
        if pos == 0:
            return 0
        with open(path, 'rb') as f:
            f.seek(pos - 1)
            if f.read(1) == b'\n':
                return pos
            f.seek(pos)
            f.readline()
            return min(f.tell(), size)

    # ------------------------------------------------------------------
    # Решение о восстановлении после загрузки
    # ------------------------------------------------------------------
    def _recovery_check(self, eventtime):
        self.recovery_timer = None
        try:
            self._evaluate_recovery()
        except Exception:
            logging.exception('PFPRS: recovery evaluation failed')
        return self.reactor.NEVER

    def _recovery_temps(self, st):
        name = st.get('active_extruder') or 'extruder'
        key = 't_ext1' if name == 'extruder1' else 't_ext'
        target = float(st.get(key, 0.) or 0.)
        actual = 0.
        obj = self._lookup(name)
        if obj is not None:
            try:
                actual = float(obj.get_status(
                    self.reactor.monotonic()).get('temperature', 0.) or 0.)
            except Exception:
                pass
        return target, actual, max(0., target - actual)

    def _evaluate_recovery(self, from_command=False):
        self.recovery_available = False
        self.recovery_auto = False
        self.recovery_reason = ''
        st = self.state
        if not st or not st.get('valid'):
            return

        ok, why = self._verify_source(st)
        if not ok:
            self.recovery_reason = why
            self._log('recovery unavailable: %s' % (why,))
            self._run_macro(self.prompt_macro, self._fallback_text(),
                            from_command)
            return
        self.recovery_available = True

        (self.recovery_target, self.recovery_actual,
         self.recovery_drop) = self._recovery_temps(st)

        blockers = []
        if not self.auto_resume:
            blockers.append('автовозобновление выключено')
        if st.get('blocked') and not self.auto_resume_after_shutdown:
            blockers.append('перед перезапуском была аварийная остановка (%s)'
                            % (st.get('shutdown_reason') or 'причина неизвестна',))
        if self.recovery_target < self.auto_resume_min_target:
            blockers.append(
                'сохранённая целевая температура хотенда %.0f °C ниже порога '
                '%.0f °C' % (self.recovery_target, self.auto_resume_min_target))
        elif self.recovery_drop > self.auto_resume_temp_delta:
            blockers.append(
                'хотенд остыл на %.1f °C (допустимо не более %.1f °C)'
                % (self.recovery_drop, self.auto_resume_temp_delta))
        if self.auto_resume_max_age > 0:
            age = time.time() - float(st.get('saved_at', 0.) or 0.)
            if age > self.auto_resume_max_age:
                blockers.append('точка восстановления устарела (%.0f с)' % (age,))

        if blockers:
            self.recovery_reason = '; '.join(blockers)
            self._log('manual recovery required: %s' % (self.recovery_reason,))
            self._run_macro(self.prompt_macro, self._fallback_text(),
                            from_command)
            return

        self.recovery_auto = True
        self.recovery_reason = (
            'хотенд остыл всего на %.1f °C из допустимых %.1f °C'
            % (self.recovery_drop, self.auto_resume_temp_delta))
        self._log('auto resume: %s' % (self.recovery_reason,))
        if self._run_macro(
                self.auto_macro,
                'PFPRS: условие автовозобновления выполнено, но макрос %s не '
                'определён. Запустите SHOW_RESUME_INTERRUPTED вручную.'
                % (self.auto_macro,), from_command):
            return
        # Автоматика сорвалась на полпути. Нельзя оставлять принтер с
        # заблокированной парковкой Z и взведённым возобновлением - снимаем
        # всё и передаём управление оператору с указанием причины.
        self.recovery_auto = False
        self._abort_resume()
        self.recovery_reason = (
            'автовозобновление прервано: %s'
            % (self.last_macro_error or 'причина неизвестна',))
        self._log('auto resume failed, falling back to manual')
        self._run_macro(self.prompt_macro, self._fallback_text(), from_command)

    def _fallback_text(self):
        """Текст на случай, если макрос диалога не определён в конфиге."""
        st = self.state or {}
        lines = ['PFPRS: печать «%s» завершилась неудачно.'
                 % (st.get('file_name') or 'без имени',)]
        if self.recovery_reason:
            lines.append('Причина: %s.' % (self.recovery_reason,))
        if self.recovery_available:
            lines.append(
                'Осмотрите деталь: отслоение от стола и наплыв пластика в '
                'точке остановки. Наплыв срежьте, иначе сопло об него '
                'ударится.')
            lines.append(
                'Затем откройте панель «Действия» - «Движения» - «Аварийное '
                'перемещение», выполните действия по инструкции и запустите '
                'SHOW_RESUME_INTERRUPTED.')
        else:
            lines.append('Возобновление невозможно, точка восстановления '
                         'непригодна.')
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Возобновление
    # ------------------------------------------------------------------
    def _abort_resume(self):
        self._clear_resume()
        vsd = self._lookup('virtual_sdcard')
        if vsd is not None and hasattr(vsd, '_reset_file'):
            try:
                vsd._reset_file()
            except Exception:
                logging.exception('PFPRS: virtual_sdcard reset failed')

    def _clear_resume(self):
        self.resuming = False
        self.resume_armed = False
        self.resume_deadline = 0.
        self.resume_position = 0
        self.z_calibrated = False
        self.z_calibrated_value = 0.
        self.emergency_homing = False

    def _context_preamble(self, st):
        lines = ['G90' if st.get('absolute_coord', True) else 'G91']
        if st.get('absolute_extrude', False):
            # Абсолютный экструдер: счётчик обязан продолжиться с сохранённого
            # значения, иначе первая же строка G1 E<...> выдавит всю катушку.
            lines.append('M82')
            lines.append('G92 E%.5f' % (float(st.get('e', 0.) or 0.),))
        else:
            lines.append('M83')
            lines.append('G92 E0')
        lines.append('M220 S%.1f' % (
            float(st.get('speed_factor', 1.) or 1.) * 100.,))
        lines.append('M221 S%.1f' % (
            float(st.get('extrude_factor', 1.) or 1.) * 100.,))
        if self._lookup(self.part_fan) is not None:
            fan = float(st.get('fan', 0.) or 0.)
            if fan > 0:
                lines.append('M106 S%d' % (int(round(min(1., fan) * 255.)),))
            else:
                lines.append('M107')
        feed = float(st.get('speed', 0.) or 0.)
        if feed > 0:
            lines.append('G1 F%.1f' % (feed,))
        total = int(st.get('total_layer', 0) or 0)
        if total > 0:
            lines.append('SET_PRINT_STATS_INFO TOTAL_LAYER=%d CURRENT_LAYER=%d'
                         % (total, int(st.get('current_layer', 0) or 0)))
        return lines

    def _sd_load(self, gcmd, vsd, name):
        loader = getattr(vsd, '_load_file', None)
        if loader is None:
            raise gcmd.error(
                'PFPRS: несовместимая версия virtual_sdcard (нет _load_file)')
        try:
            loader(gcmd, name, check_subdirs=True)
        except TypeError:
            loader(gcmd, name)

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------
    cmd_PFPRS_ENABLE_help = 'Включить сохранение состояния печати'

    def cmd_PFPRS_ENABLE(self, gcmd):
        self.resuming = False
        self.enabled = True
        gcmd.respond_info('PFPRS: сохранение состояния включено')

    cmd_PFPRS_DISABLE_help = 'Выключить сохранение состояния печати'

    def cmd_PFPRS_DISABLE(self, gcmd):
        self.enabled = False
        gcmd.respond_info('PFPRS: сохранение состояния выключено')

    cmd_PFPRS_SAVE_STATE_help = 'Немедленно записать точку восстановления'

    def cmd_PFPRS_SAVE_STATE(self, gcmd):
        if self.resuming:
            self._log('save skipped: resume in progress')
            return
        # Параметры Z/LAYER принимаются для совместимости со старым _LOG_Z,
        # но игнорируются: точка возобновления всегда берётся с задержкой
        # resume_lag, иначе компенсация look-ahead буфера теряется.
        gcmd.get_float('Z', None)
        gcmd.get_int('LAYER', None)
        now = self.reactor.monotonic()
        snap = self._collect(now)
        if snap is None:
            raise gcmd.error('PFPRS: не удалось собрать состояние принтера')
        self.samples.append((now, snap))
        while len(self.samples) > self.max_samples:
            self.samples.popleft()
        lagged = self._pick_lagged(now) or snap
        self._persist(lagged)
        gcmd.respond_info('PFPRS: точка восстановления обновлена (байт %d)'
                          % (int(lagged['file_position']),))

    cmd_PFPRS_HOLD_help = (
        'Заморозить точку восстановления (на время ухода головы с траектории)')

    def cmd_PFPRS_HOLD(self, gcmd):
        self.hold += 1
        if self.debug_mode:
            self._log('recovery point frozen (hold=%d)' % (self.hold,))

    cmd_PFPRS_RELEASE_help = 'Снять заморозку точки восстановления'

    def cmd_PFPRS_RELEASE(self, gcmd):
        self.hold = max(0, self.hold - 1)
        if self.hold == 0:
            # История снимков относится к периоду до ухода головы. Начинаем
            # окно задержки заново, чтобы в точку не попал срез с парковки.
            self.samples.clear()
        if self.debug_mode:
            self._log('recovery point released (hold=%d)' % (self.hold,))

    cmd_PFPRS_STATUS_help = 'Показать сохранённую точку восстановления'

    def cmd_PFPRS_STATUS(self, gcmd):
        st = self.state
        if not st:
            gcmd.respond_info('PFPRS: сохранённого состояния нет')
            return
        ok, why = self._verify_source(st)
        age = time.time() - float(st.get('saved_at', 0.) or 0.)
        gcmd.respond_info(
            'PFPRS:\n'
            '  файл:        %s\n'
            '  позиция:     %d из %d\n'
            '  XYZ (gcode): %.2f / %.2f / %.2f\n'
            '  Z (кинем.):  %.2f, offset Z: %.3f\n'
            '  T сопло/стол/камера: %.0f / %.0f / %.0f\n'
            '  экструдер:   %s (T%d)\n'
            '  сетка стола: %s\n'
            '  сохранено:   %.0f с назад\n'
            '  валидна:     %s%s\n'
            '  автовозобновление: %s (порог %.1f °C)'
            % (st.get('file_name', '?'),
               int(st.get('file_position', 0) or 0),
               int(st.get('file_size', 0) or 0),
               float(st.get('x', 0.)), float(st.get('y', 0.)),
               float(st.get('z', 0.)), float(st.get('kin_z', 0.)),
               float(st.get('offset_z', 0.)),
               float(st.get('t_ext', 0.)), float(st.get('t_bed', 0.)),
               float(st.get('t_chamb', 0.)),
               st.get('active_extruder', '?'), int(st.get('tool', 0) or 0),
               st.get('bed_mesh') or '-',
               age,
               'да' if st.get('valid') else 'нет',
               '' if ok else ' (%s)' % (why,),
               'вкл' if self.auto_resume else 'выкл',
               self.auto_resume_temp_delta))

    cmd_PFPRS_DISCARD_help = 'Удалить сохранённую точку восстановления'

    def cmd_PFPRS_DISCARD(self, gcmd):
        self.samples.clear()
        self._clear_resume()
        self._invalidate('отменено пользователем')
        self.recovery_available = False
        self.recovery_auto = False
        self.recovery_reason = ''
        gcmd.respond_info('PFPRS: точка восстановления удалена')

    cmd_PFPRS_SET_AUTO_RESUME_help = 'Включить/выключить автовозобновление'

    def cmd_PFPRS_SET_AUTO_RESUME(self, gcmd):
        value = gcmd.get_int('ENABLE', None, minval=0, maxval=1)
        if value is None:
            value = 1 if gcmd.get('VALUE', 'true').lower() in (
                '1', 'true', 'yes', 'on') else 0
        self.auto_resume = bool(value)
        if self.state:
            self._write(dict(self.state))
        gcmd.respond_info('PFPRS: автовозобновление %s'
                          % ('включено' if self.auto_resume else 'выключено',))

    cmd_PFPRS_SET_Z_CALIBRATED_help = (
        'Отметить, что кинематический Z уже выставлен вручную или зондом')

    def cmd_PFPRS_SET_Z_CALIBRATED(self, gcmd):
        value = gcmd.get_int('VALUE', 1, minval=0, maxval=1)
        self.z_calibrated = bool(value)
        if self.z_calibrated:
            th = self._lookup('toolhead')
            default = th.get_position()[2] if th is not None else 0.
            self.z_calibrated_value = gcmd.get_float('Z', default)
            gcmd.respond_info('PFPRS: Z принят как %.3f'
                              % (self.z_calibrated_value,))
        else:
            self.z_calibrated_value = 0.
            gcmd.respond_info('PFPRS: отметка калибровки Z снята')

    cmd_PFPRS_SET_EMERGENCY_HOMING_help = (
        'Разрешить/запретить парковку оси Z (для homing_override)')

    def cmd_PFPRS_SET_EMERGENCY_HOMING(self, gcmd):
        self.emergency_homing = bool(
            gcmd.get_int('VALUE', 1, minval=0, maxval=1))
        gcmd.respond_info('PFPRS: аварийная парковка %s'
                          % ('включена' if self.emergency_homing else 'выключена',))

    cmd_PFPRS_CHECK_RECOVERY_help = (
        'Повторно оценить возможность восстановления и показать диалог')

    def cmd_PFPRS_CHECK_RECOVERY(self, gcmd):
        self._evaluate_recovery(from_command=True)
        if not self.recovery_available:
            gcmd.respond_info('PFPRS: восстановление невозможно%s'
                              % (': %s' % self.recovery_reason
                                 if self.recovery_reason else '',))

    cmd_PFPRS_PREPARE_RESUME_help = (
        'Проверить точку восстановления и подготовить файл к продолжению')

    def cmd_PFPRS_PREPARE_RESUME(self, gcmd):
        st = self.state
        if not st:
            raise gcmd.error('PFPRS: сохранённого состояния нет')
        st = dict(st)
        force = gcmd.get_int('FORCE', 0, minval=0, maxval=1)

        override = gcmd.get('GCODE_FILE', None)
        if override:
            path = str(override).strip().strip("'").strip('"')
            if not os.path.isabs(path):
                path = os.path.join(self.gcode_path, path)
            if not os.path.isfile(path):
                raise gcmd.error('PFPRS: файл не найден: %s' % (path,))
            if os.path.abspath(path) != os.path.abspath(
                    st.get('file_path') or ''):
                if not force:
                    raise gcmd.error(
                        'PFPRS: указанный файл не совпадает с сохранённым '
                        '(%s). Повторите с FORCE=1, если уверены.'
                        % (st.get('file_name') or '?',))
                st['file_path'] = path
                st['file_name'] = os.path.relpath(
                    path, self.gcode_path) if path.startswith(
                        self.gcode_path) else os.path.basename(path)
                st['file_size'] = 0
                st['file_mtime'] = 0.

        if not st.get('valid') and not force:
            raise gcmd.error(
                'PFPRS: точка восстановления помечена недействительной (%s). '
                'Повторите с FORCE=1, если уверены.'
                % (st.get('invalid_reason') or 'причина не указана',))

        ok, why = self._verify_source(st)
        if not ok and not force:
            raise gcmd.error('PFPRS: %s' % (why,))

        vsd = self._lookup('virtual_sdcard')
        if vsd is None:
            raise gcmd.error('PFPRS: [virtual_sdcard] не сконфигурирован')
        try:
            if vsd.get_status(self.reactor.monotonic()).get('is_active'):
                raise gcmd.error('PFPRS: печать уже идёт')
        except AttributeError:
            pass

        path = st['file_path']
        offset = self._align_offset(path, int(st.get('file_position', 0) or 0))
        if offset >= os.path.getsize(path):
            raise gcmd.error('PFPRS: позиция восстановления в конце файла')

        self._sd_load(gcmd, vsd, st.get('file_name') or os.path.basename(path))
        vsd.file_position = offset

        self.resume_position = offset
        self.resuming = True
        self.resume_armed = True
        self.enabled = False
        self.emergency_homing = True
        self.resume_deadline = self.reactor.monotonic() + self.resume_timeout
        self.state = st
        gcmd.respond_info(
            'PFPRS: подготовлено возобновление %s с байта %d (слой %s из %s)'
            % (st.get('file_name'), offset,
               st.get('current_layer', '?'), st.get('total_layer', '?')))

    cmd_PFPRS_BEGIN_RESUME_help = (
        'Восстановить контекст печати и запустить продолжение файла')

    def cmd_PFPRS_BEGIN_RESUME(self, gcmd):
        if not self.resume_armed:
            raise gcmd.error(
                'PFPRS: возобновление не подготовлено (нужен '
                'PFPRS_PREPARE_RESUME)')
        st = self.state or {}
        if self.dual:
            th = self._lookup('toolhead')
            active = th.get_extruder().get_name() if th is not None else ''
            want = st.get('active_extruder', 'extruder')
            if active != want:
                raise gcmd.error(
                    'PFPRS: активен %s, а печать велась с %s - смените '
                    'инструмент до PFPRS_BEGIN_RESUME' % (active, want))
        vsd = self._lookup('virtual_sdcard')
        if vsd is None:
            raise gcmd.error('PFPRS: [virtual_sdcard] не сконфигурирован')

        script = self._context_preamble(st)
        if script:
            self.gcode.run_script_from_command('\n'.join(script))

        if self.post_restore_macro:
            if self._lookup('gcode_macro %s'
                            % (self.post_restore_macro,)) is None:
                raise gcmd.error(
                    'PFPRS: post_restore_macro %s не определён'
                    % (self.post_restore_macro,))
            gcmd.respond_info('PFPRS: выполняю %s' % (self.post_restore_macro,))
            self.gcode.run_script_from_command(self.post_restore_macro)

        vsd.file_position = self.resume_position
        self.resuming = False
        self.resume_armed = False
        self.resume_deadline = 0.
        self.z_calibrated = False
        self.emergency_homing = False
        self.enabled = True
        self.samples.clear()
        self.last_persist = self.reactor.monotonic()
        vsd.do_resume()
        gcmd.respond_info('PFPRS: печать продолжена с байта %d'
                          % (self.resume_position,))

    cmd_PFPRS_ABORT_RESUME_help = 'Отменить подготовленное возобновление'

    def cmd_PFPRS_ABORT_RESUME(self, gcmd):
        self._abort_resume()
        gcmd.respond_info('PFPRS: подготовка возобновления отменена')

    # ------------------------------------------------------------------
    # Статус для макросов
    # ------------------------------------------------------------------
    def get_status(self, eventtime):
        st = self.state or {}
        kin_z = float(st.get('kin_z', 0.) or 0.)
        mesh = st.get('bed_mesh') or self.bed_mesh_fallback
        # Опорная высота для SET_KINEMATIC_POSITION.
        # Если сетку удаётся восстановить по имени - это истинная кинематика.
        # Если нет (адаптивная сетка), её поправку вносим сюда, в объявляемую
        # систему координат, а НЕ в SET_GCODE_OFFSET: макросы смены
        # инструмента на тулченджерах читают homing_origin.z как babystep
        # оператора и сохраняют его в постоянную калибровку машины.
        if mesh:
            z_ref = kin_z
        else:
            z_ref = round(float(st.get('z', 0.) or 0.)
                          + float(st.get('offset_z', 0.) or 0.), 4)
        if self.z_calibrated:
            kin_z = self.z_calibrated_value
            z_ref = self.z_calibrated_value
        saved_at = float(st.get('saved_at', 0.) or 0.)
        return {
            # состояние модуля
            'enabled': self.enabled,
            'resuming': self.resuming,
            'armed': self.resume_armed,
            'auto_resume': self.auto_resume,
            'auto_resume_temp_delta': self.auto_resume_temp_delta,
            'emergency_homing': self.emergency_homing,
            'hold': self.hold,
            'z_calibrated': self.z_calibrated,
            'dual': self.dual,
            # результат проверки после загрузки
            'has_state': bool(self.state),
            'valid': bool(st.get('valid')),
            'blocked': bool(st.get('blocked')),
            'shutdown_reason': st.get('shutdown_reason', ''),
            'invalid_reason': st.get('invalid_reason', ''),
            'recovery_available': self.recovery_available,
            'recovery_auto': self.recovery_auto,
            'recovery_reason': self.recovery_reason,
            'temp_target': round(self.recovery_target, 1),
            'temp_actual': round(self.recovery_actual, 1),
            'temp_drop': round(self.recovery_drop, 1),
            # сохранённая точка
            'file': st.get('file_name', ''),
            'file_path': st.get('file_path', ''),
            'file_position': int(st.get('file_position', 0) or 0),
            'file_size': int(st.get('file_size', 0) or 0),
            'resume_position': int(self.resume_position),
            'x': float(st.get('x', 0.) or 0.),
            'y': float(st.get('y', 0.) or 0.),
            'z': float(st.get('z', 0.) or 0.),
            'e': float(st.get('e', 0.) or 0.),
            'kin_x': float(st.get('kin_x', 0.) or 0.),
            'kin_y': float(st.get('kin_y', 0.) or 0.),
            'kin_z': kin_z,
            'z_ref': z_ref,
            'offset_x': float(st.get('offset_x', 0.) or 0.),
            'offset_y': float(st.get('offset_y', 0.) or 0.),
            'offset_z': float(st.get('offset_z', 0.) or 0.),
            'mesh_comp': float(st.get('mesh_comp', 0.) or 0.),
            'speed': float(st.get('speed', 0.) or 0.),
            'speed_factor': float(st.get('speed_factor', 1.) or 1.),
            'extrude_factor': float(st.get('extrude_factor', 1.) or 1.),
            'fan': float(st.get('fan', 0.) or 0.),
            't_ext': float(st.get('t_ext', 0.) or 0.),
            't_ext1': float(st.get('t_ext1', 0.) or 0.),
            't_bed': float(st.get('t_bed', 0.) or 0.),
            't_chamb': float(st.get('t_chamb', 0.) or 0.),
            'active_extruder': st.get('active_extruder', 'extruder'),
            'tool': int(st.get('tool', 0) or 0),
            'bed_mesh': mesh,
            'total_layer': int(st.get('total_layer', 0) or 0),
            'current_layer': int(st.get('current_layer', 0) or 0),
            'absolute_coord': bool(st.get('absolute_coord', True)),
            'absolute_extrude': bool(st.get('absolute_extrude', False)),
            'saved_at': saved_at,
            'age': max(0., time.time() - saved_at) if saved_at else 0.,
            # параметры для макросов
            'z_hop': self.z_hop,
            'z_lift_speed': self.z_lift_speed,
            'travel_speed': self.travel_speed,
            'prime_length': self.prime_length,
            'prime_speed': self.prime_speed,
            'prime_retract': self.prime_retract,
            'safe_home_temp': self.safe_home_temp,
            'min_active_temp': self.min_active_temp,
            'restore_gcode_offset': self.restore_gcode_offset,
            'chamber_heater': self.chamber_heater,
            'post_restore_macro': self.post_restore_macro,
        }
