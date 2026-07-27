# PFPRS — Power Failure Print Recovery System

Восстановление печати после отключения питания для Klipper.  
Поддержка OrcaSlicer (рекомендуется 2.4.2), одноголовые и двухголовые принтеры.

Сохраняет координаты XY/Z (до skew), позицию в файле и температуры; по кнопке собирает `restore.gcode` и продолжает печать.

## Установка

```bash
cd ~ && git clone -b v4 https://github.com/Transistor427/PFPRS/

# Python-модуль
sudo ln -sf ~/PFPRS/pfprs.py ~/klipper/klippy/extras/pfprs.py

# Конфиг (один из двух)
cp ~/PFPRS/pfprs.cfg ~/printer_data/config/klipper-config/pfprs.cfg
# cp ~/PFPRS/pfprs_dual.cfg ~/printer_data/config/klipper-config/pfprs_dual.cfg

sudo systemctl restart klipper
```

В `printer.cfg`:
```
[include klipper-config/pfprs.cfg]
```
или
```
[include klipper-config/pfprs_dual.cfg]
```

Нужен `[save_variables]` (в dual-конфиге уже есть).

**Dual:** используйте секцию `[homing_override]` из `~/PFPRS/homing.cfg` в `homing.cfg` принтера, чтобы иметь возможность корректно парковать оси.

## Использование

### OrcaSlicer (опционально)

В G-коде смены слоя можно добавить для доп. сохранения:
```
_LOG_Z Z=[layer_z]
```
Основное сохранение идёт по интервалу (`save_interval`, по умолчанию 15 с) и без этого макроса.

### Восстановление

1. При необходимости поправьте Z (`SET_Z_MAX_POSITION` → поднять стол → `SET_Z_ZERO_POSITION`)
2. Вызовите `SHOW_RESUME_INTERRUPTED` (кнопка на панели KlipperScreen / макрос в Fluidd)

Будет собран `restore.gcode` и запущена печать с сохранённой точки.  
Во время печати restore сохранение координат продолжается — повторное отключение тоже можно восстановить.  
Превью копируется из оригинала; Moonraker обновляет метаданные через `metascan` (`moonraker_url` в конфиге).

## Удаление

```bash
sudo rm -f ~/klipper/klippy/extras/pfprs.py
rm -f ~/printer_data/config/klipper-config/pfprs.cfg ~/printer_data/config/klipper-config/pfprs_dual.cfg
sudo rm -rf ~/PFPRS
```
Уберите `[include …]` из `printer.cfg` и перезапустите Klipper.
