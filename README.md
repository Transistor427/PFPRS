# PFPRS — Power Failure Print Recovery System

Восстановление печати после отключения питания для Klipper.  
Поддержка OrcaSlicer, одноголовые и двухголовые принтеры.

Сохраняет координаты XY/Z, позицию в файле и температуры; по кнопке собирает `restore.gcode` и продолжает печать.

## Установка

```bash
cd ~ && git clone -b v5 https://github.com/Transistor427/PFPRS/

# Python-модуль
sudo ln -sf ~/PFPRS/pfprs.py ~/klipper/klippy/extras/pfprs.py

# Конфиг (один из двух)
cp ~/PFPRS/pfprs.cfg ~/printer_data/config/pfprs.cfg
# cp ~/PFPRS/pfprs_dual.cfg ~/printer_data/config/pfprs_dual.cfg

sudo systemctl restart klipper
```

В `printer.cfg`:
```
[include pfprs.cfg]
```
или
```
[include pfprs_dual.cfg]
```

Нужен `[save_variables]` (в dual-конфиге уже есть).  
Для калибровки Z зондом нужен настроенный `[probe]`.

**Dual:** скопируйте `[homing_override]` из `~/PFPRS/homing.cfg` в `homing.cfg` принтера.

## Использование

### OrcaSlicer (опционально)

```
_LOG_Z Z=[layer_z]
```
Основное сохранение — по интервалу `save_interval` (15 с).

### Восстановление (вручную по Z)

1. При необходимости: `SET_Z_MAX_POSITION` → поднять стол → `SET_Z_ZERO_POSITION`
2. `SHOW_RESUME_INTERRUPTED`

### Восстановление с зондом и пластиной (v5)

1. Подведите голову над деталью, положите пластину известной толщины (по умолчанию **1 мм**)
2. Вызовите `RESUME_WITH_PROBE` или `RESUME_WITH_PROBE PLATE=1.5`

Что делает:
- собирает `restore.gcode`;
- `SET_KINEMATIC_POSITION` в центр стола, Z = max−5;
- `PROBE` по пластине;
- задаёт Z = высота_детали (`pr_z`) + толщина пластины;
- поднимается, вызывает `_START_PRINT_RESTORE`, запускает печать.

Только калибровка Z + старт восстановления (без сборки файла): `PROBE_Z_FOR_RESTORE PLATE=1`

## Удаление

```bash
sudo rm -f ~/klipper/klippy/extras/pfprs.py
rm -f ~/printer_data/config/pfprs.cfg ~/printer_data/config/pfprs_dual.cfg
sudo rm -rf ~/PFPRS
```
Уберите `[include …]` из `printer.cfg` и перезапустите Klipper.
