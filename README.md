# OpenWrt для NETGEAR Stora (MS2000 / MS2110)

Патчер для официального образа **OpenWrt 25.12.6** (`kirkwood/generic`, `netgear_stora`). Исправляет аппаратное выключение NETGEAR Stora и позволяет задать MAC-адрес Ethernet при сборке прошивки.

Патчер **не прошивает устройство самостоятельно**: он создаёт новый `sysupgrade`-образ на компьютере. Прошивка выполняется отдельно на Stora.

**Протестировано на ПК:** Debian 12.15.0.

## Что исправляет патчер

- **Выключение питания:** исправляет параметр `gpio-poweroff` во встроенном Device Tree: ошибочный `gpio0/40` (active-low) заменяется на `gpio1/8` (active-high). После штатного `poweroff` Stora может полностью отключать питание.
- **MAC-адрес:** запрашивает MAC при запуске (либо принимает `--mac`) и добавляет скрипт `/etc/uci-defaults/99-stora-mac`, который настраивает `eth0` при первоначальной загрузке. MAC не зашит в исходный код патчера; окружение U-Boot не изменяется.

Патчер также пересчитывает CRC заголовка `uImage`, пересобирает SquashFS, проверяет ограничения размеров UBI-томов, сохраняет метаданные `fwtool` и правильную структуру TAR для `nand_do_upgrade`.

## Требования

- ПК с **Linux** и Python 3 (стандартная библиотека; `pip install` не требуется).
- `squashfs-tools` (`unsquashfs` и `mksquashfs`) и `tar`.
- Оригинальный образ OpenWrt **строго версии 25.12.6** для `netgear_stora`.
- Доступ к Stora по SSH/SCP; для восстановления при проблемах рекомендуется UART-консоль.

### Установка зависимостей на ПК

**Ubuntu / Debian:**

```bash
sudo apt update
sudo apt install -y python3 squashfs-tools tar openssh-client git
```

**Fedora:**

```bash
sudo dnf install -y python3 squashfs-tools tar openssh-clients git
```

**Arch Linux:**

```bash
sudo pacman -S --needed python squashfs-tools tar openssh git
```

## Подготовка и сборка прошивки

1. Склонируйте репозиторий:

   ```bash
   git clone https://github.com/redn1ghtz/openwrt_netgear_stora_ms2000.git
   cd openwrt_netgear_stora_ms2000
   ```

2. Поместите рядом с `stora_patcher.py` оригинальный файл:

   ```text
   openwrt-25.12.6-kirkwood-generic-netgear_stora-squashfs-sysupgrade.bin
   ```

   Ожидаемый SHA-256 оригинала:

   ```text
   491827739fb920663e6ea5f772981f6bc861211f3c1d60f565ba7b8d21c066be
   ```

   Патчер самостоятельно проверяет эту контрольную сумму и откажется обрабатывать другой образ.

3. Запустите патчер:

   ```bash
   python3 stora_patcher.py \
     openwrt-25.12.6-kirkwood-generic-netgear_stora-squashfs-sysupgrade.bin
   ```

   Введите **собственный заводской MAC-адрес** Stora в формате `XX:XX:XX:XX:XX:XX` по запросу. Не используйте MAC другого устройства.

   Альтернативно MAC можно указать параметром `--mac`, но тогда он сохранится в истории команд оболочки.

4. После успешной сборки появится файл:

   ```text
   openwrt-25.12.6-kirkwood-generic-netgear_stora-squashfs-sysupgrade-patched.bin
   ```

   Скрипт выведет его SHA-256. Имя выходного файла можно задать через `-o`.

## Установка на NETGEAR Stora

> **Внимание:** прошивка NAND — рискованная операция. Используйте образ только для указанной модели и версии OpenWrt. Обеспечьте стабильное питание; желательно иметь подключённую UART-консоль. Перед установкой сохраните нужные настройки.

Передайте созданный файл на устройство (замените `192.168.1.1` на фактический IP Stora):

```bash
scp openwrt-25.12.6-kirkwood-generic-netgear_stora-squashfs-sysupgrade-patched.bin \
  root@192.168.1.1:/tmp/
```

Подключитесь по SSH:

```bash
ssh root@192.168.1.1
```

Проверьте образ **на Stora**:

```sh
sysupgrade -T /tmp/openwrt-25.12.6-kirkwood-generic-netgear_stora-squashfs-sysupgrade-patched.bin
```

Для **чистой установки без сохранения настроек**:

```sh
sysupgrade -n /tmp/openwrt-25.12.6-kirkwood-generic-netgear_stora-squashfs-sysupgrade-patched.bin
```

`-n` удаляет пользовательские настройки и дополнительно установленные пакеты. Разделы SATA HDD не форматируются; U-Boot и его окружение не перезаписываются. После перезагрузки IP устройства может вернуться к стандартному `192.168.1.1`.

## Проверка после загрузки

```sh
# Исправленный GPIO: phandle gpio1, линия 8, флаги 0
hexdump -C /sys/firmware/devicetree/base/gpio-poweroff/gpios

# Драйвер выключения должен быть привязан
ls -l /sys/bus/platform/devices/gpio-poweroff/driver

# Должен отображаться MAC, введённый при сборке
cat /sys/class/net/eth0/address
```

Для протестированной конфигурации значение `gpios` выглядит так:

```text
00 00 00 1a 00 00 00 08 00 00 00 00
```

Первое число (`phandle`) зависит от Device Tree; важны контроллер `gpio1`, линия `8` и флаги `0`.

При штатном выключении ядро Linux само синхронизирует кэш и останавливает HDD; затем драйвер `poweroff-gpio` отключает питание устройства.

## Ограничения

- Патчер рассчитан на **конкретный оригинальный образ OpenWrt 25.12.6** с указанной SHA-256, а не на произвольные версии.
- Проверка `sysupgrade -T` обязательна, но сама по себе не гарантирует успешную запись и загрузку; рекомендуется UART.
- Не используйте `sysupgrade -F`, `mtd erase` или `ubiformat` для обхода проверок.
