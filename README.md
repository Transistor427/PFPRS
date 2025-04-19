# PFPRS - Power Failure Print Recovery System

Конфигурационный и исполняемый файлы для добавления фунции восстановления печати после отключения питания или аварийной остановки.

## Установка
 ```
cd ~
git clone https://github.com/Transistor427/PFPRS/
cd PFPRS
sudo chmod 777 pfprs.sh
sudo cp -r ./* ~/printer_data/config/klipper-config
```

Веб-интерфейс > Конфигурация > printer.cfg

```
[include klipper-config/pfprs.cfg]
```
## Дополнительные действия
```
./kiauh-zb/kiauh.sh
```
Далее выбираем:
```
4) [Advanced]
8) [G-Code Shell Command]
```
Соглашаемся с рисками (y).
Отказываемся от создания файла-примера (n). 

## Настройка слайсера
При смене слоев нужно добавить макрос `_LOG_Z`.
![изображение](https://github.com/user-attachments/assets/6b2c2790-d9e0-4363-9f62-3de80d8da48d)
