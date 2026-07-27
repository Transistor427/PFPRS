# PFPRS - Power Failure Print Recovery System

Конфигурация и Python-расширение Klipper для восстановления печати после отключения питания или аварийной остановки.

Поддерживается **OrcaSlicer** (рекомендуется **2.4.2**; проверено также на 2.3.x). Восстановление идёт по сохранённой позиции в файле и координатам **gcode XY до skew_correction**, а не по хрупкому поиску слоя через bash/`sed`.

## Что изменилось в v4

- Bash-скрипты (`curr_layer.sh` / `next_layer.sh`) и расширение **G-Code Shell Command** больше не нужны
- Логика обрезки G-кода перенесена в Python-модуль `pfprs.py` (`[pfprs]`)
- Сохраняются X/Y/Z из `gcode_move.gcode_position` (координаты до skew / bed mesh)
- Сохраняется byte-offset в исходном файле — корректный resume при dual/toolchange
- Макросы панелей KlipperScreen (`SHOW_RESUME_INTERRUPTED`, `_RESUME_INTERRUPTED_CURR`, …) сохранены; выбор «текущий/следующий слой» убран — resume всегда с сохранённой точки
- Аварийный homing dual по-прежнему только в `homing.cfg` принтера (не в Python)
- Z+ калибровка по датчикам снизу стола (как в klipper-plr) **не используется**

## Установка

```bash
cd ~ && git clone -b v4 https://github.com/Transistor427/PFPRS/

# Python-модуль в Klipper extras
sudo ln -sf ~/PFPRS/pfprs.py ~/klipper/klippy/extras/pfprs.py

# Конфиги — копируем нужный файл в config
# Одноголовый:
cp ~/PFPRS/pfprs.cfg ~/printer_data/config/klipper-config/pfprs.cfg
# Двухголовый (вместо предыдущей строки):
# cp ~/PFPRS/pfprs_dual.cfg ~/printer_data/config/klipper-config/pfprs_dual.cfg

sudo systemctl restart klipper
```

Веб-интерфейс → Конфигурация → `printer.cfg`

Для одноголового принтера:
```
[include pfprs.cfg]
```

Для двухголового принтера:
```
[include pfprs_dual.cfg]
```

Также нужен `[save_variables]` (в `pfprs_dual.cfg` уже есть; для single добавьте в `printer.cfg`, если его ещё нет):
```
[save_variables]
filename: ~/printer_data/config/variables.cfg
```

> Расширение Kiauh **G-Code Shell Command** для PFPRS больше не требуется.

## Настройка Dual (homing.cfg)

Логику аварийной парковки XY **нельзя** переносить в Python — она остаётся в конфиге принтера.

1. Откройте `~/PFPRS/homing.cfg`  
2. Скопируйте содержимое  
3. В `homing.cfg` принтера (`~/printer_data/config/...`) замените секцию `[homing_override]` скопированным содержимым  
4. «Сохранить и перезапустить»

Файл задаёт `emergency_homing` → `HOMING_EMERGENCY` (Y затем X) при восстановлении dual-печати.

## Настройка OrcaSlicer

В G-коде принтера при **смене слоя** добавьте:

```
_LOG_Z Z=[layer_z]
```

Макрос вызывает `PFPRS_SAVE_STATE` и сохраняет Z, XY (до skew), температуры, активную голову (dual) и позицию в файле.

Рекомендуется OrcaSlicer **2.4.2**. Маркеры `;LAYER_CHANGE` / `;Z:` — запасной путь, если в старых сохранениях нет `pr_file_pos`.

## Команды Python-модуля

| Команда | Назначение |
|--------|------------|
| `PFPRS_SAVE_STATE [Z=]` | Сохранить состояние сейчас |
| `PFPRS_BUILD_RESTORE` | Собрать `restore.gcode` с сохранённой позиции |
| `PFPRS_QUERY_STATE` | Показать сохранённые данные |
| `PFPRS_ENABLE` / `PFPRS_DISABLE` | Вкл/выкл автосохранение |
| `PFPRS_CLEAR` | Очистить историю состояний в памяти |

Во время печати модуль сам включает сохранение (интервал `save_interval`, по умолчанию 15 с) и дополнительно сохраняет на каждом `_LOG_Z`.

## Алгоритм восстановления

### Подготовка
1. Печатайте G-код с `_LOG_Z` в layer change  
2. Аварийная остановка или кратковременное отключение питания  
3. После загрузки убедитесь, что стол не ушёл вниз слишком сильно  

### Через KlipperScreen
1. Если стол сместился: нагрев → «Движение» → «Действия» → «Аварийные перемещения» → «Установить Z в максимум» → поднять стол к соплу → «Установить позицию Z в 0» → «Возобновить»  
2. Если стол на месте: «Аварийные перемещения» → «Возобновить»  

### Через Fluidd
Макросы `SET_Z_MAX_POSITION` → `SET_Z_ZERO_POSITION` → `SHOW_RESUME_INTERRUPTED` (те же шаги по Z при смещении стола).

Печать продолжается с сохранённого byte-offset и координат **X/Y/Z** (gcode до skew). Выбор слоя больше не нужен.

Даже если визуально стол не опустился, обычно есть просадка 0.1–0.3 мм — лучше слегка поднять стол перед resume.

## Dual / две головы

- Сохраняются `pr_t_ext`, `pr_t_ext1`, `pr_act_ext`  
- При сборке `restore.gcode` в префикс добавляется последний `T0`/`T1` до точки обрыва (в т.ч. mid-layer toolchange / wipe tower)  
- `_START_PRINT_RESTORE` греет активную голову до рабочей температуры и подогревает вторую, если она использовалась  
- Парковка XY идёт через `emergency_homing` из `homing.cfg`  

Одновременная dual-печать с переключениями в пределах слоя продолжается за счёт file offset + tool context + XY, а не по одному только Z.

## Почему старые bash-скрипты ломались на OrcaSlicer

1. `;Z:1` в файле при сохранённом `1.0` — `sed` не находил слой  
2. Компактные ходы `G1 Z.2` вместо `Z0.2` — не совпадали с шаблоном ` Z${height}`  
3. Dual toolchange внутри слоя — обрезка «по Z» теряла активную голову и контекст  

v4 сравнивает Z как float и опирается на `file_position` + `gcode_position`.

## Описание работы

1. Во время печати PFPRS периодически и на `_LOG_Z` пишет в `save_variables`: `pr_x/y/z`, `pr_file`, `pr_file_pos`, температуры  
2. Пользователь вручную вызывает `SHOW_RESUME_INTERRUPTED` (или кнопку на панели) → `PFPRS_BUILD_RESTORE` создаёт `~/printer_data/gcodes/restore.gcode`  
3. В начало файла вставляется `_START_PRINT_RESTORE` (нагрев, homing XY, возврат в сохранённые XY)  
4. `SDCARD_PRINT_FILE FILENAME=restore.gcode` продолжает печать  

## Ограничения

- После долгого простоя деталь может отлипнуть — функция рассчитана на кратковременные отключения  
- Нет аппаратной Z+ калибровки по концевикам снизу/сверху стола (в отличие от klipper-plr) — Z восстанавливается через `SET_KINEMATIC_POSITION` и ручную подстройку  
- Точность XY зависит от того, что оси не потеряли шаги; после power loss нужен homing XY  

## Удаление

```bash
sudo rm -f ~/klipper/klippy/extras/pfprs.py
rm -f ~/printer_data/config/pfprs.cfg ~/printer_data/config/pfprs_dual.cfg
sudo rm -rf ~/PFPRS
```

Удалите `[include pfprs.cfg]` / `[include pfprs_dual.cfg]` из `printer.cfg` и перезапустите Klipper.

## Обновление

1. Обновите репозиторий: `cd ~/PFPRS && git pull`
2. Заново скопируйте нужный конфиг (`pfprs.cfg` или `pfprs_dual.cfg`) в `~/printer_data/config/`
3. Обновите symlink модуля: `sudo ln -sf ~/PFPRS/pfprs.py ~/klipper/klippy/extras/pfprs.py`
4. Перезапустите Klipper
