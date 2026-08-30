# LoRa CLI (Raspberry Pi + SX126x via LoRaRF)

Schlankes Python-CLI-Tool für den Raspberry Pi mit einem
`DX-LR30-900M22S`-Modul (Semtech SX1261/SX1262), das die
`LoRaRF`-Bibliothek direkt verwendet.

Verhalten:

* Startet das Modul, konfiguriert LoRa (868 MHz / SF7 / BW125 / CR4/6 / CRC on)
* Läuft **dauerhaft im Empfang** (RX continuous)
* Empfangene Pakete werden mit Zeitstempel, RSSI und SNR ausgegeben
* Über die Konsole kann jederzeit Text eingegeben werden – bei `ENTER`
  wird die Zeile als LoRa-Paket gesendet, danach geht das Modul
  automatisch wieder in RX

## Hardware / Pinout (BCM)

| Pin RPi | Funktion | GPIO   | Farbe   |
|---------|----------|--------|---------|
| 15      | RXEN     | GPIO22 | Grau    |
| 16      | BUSY     | GPIO23 | Braun   |
| 17      | 3V3      | -      | Rot     |
| 18      | NRST     | GPIO24 | Orange  |
| 19      | SPI MOSI | GPIO10 | Blau    |
| 20      | GND      | -      | Schwarz |
| 21      | SPI MISO | GPIO9  | Lila    |
| 22      | DIO1     | GPIO25 | Weiß    |
| 23      | SPI SCLK | GPIO11 | Grün    |
| 24      | NSS/CS   | GPIO8  | Gelb    |

`DIO2` ist auf dem Modul mit `TXEN` gebrückt und wird per
`setDio2RfSwitch(True)` als interner TX/RX-Switch konfiguriert
(deshalb `txenPin = -1`).

## Voraussetzungen auf dem Raspberry Pi

1. SPI aktivieren – `/boot/firmware/config.txt`:

   ```
   dtparam=spi=on
   ```

   Danach reboot. Prüfen:

   ```bash
   ls -l /dev/spidev0.0 /dev/gpiochip0
   ```

2. Python-Abhängigkeiten:

   ```bash
   cd /app/lora
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   `requirements.txt` installiert `LoRaRF` **und** `rpi-lgpio`.
   `rpi-lgpio` ist ein Drop-in-Ersatz für `RPi.GPIO`, der intern
   auf `libgpiod`/`lgpio` (`/dev/gpiochip*`) aufsetzt und
   – im Gegensatz zum originalen `RPi.GPIO` – auch auf **Raspberry Pi 5**
   und **ohne `sudo`** funktioniert.

   ⚠️ **Wichtig:** `RPi.GPIO` und `rpi-lgpio` können nicht parallel
   installiert sein. Falls im venv schon `RPi.GPIO` liegt (z. B. weil
   LoRaRF es als Dependency mitgezogen hat), erst deinstallieren:

   ```bash
   pip uninstall -y RPi.GPIO
   pip install rpi-lgpio
   ```

3. Zugriff **ohne `sudo`** – Dein User muss in den passenden Gruppen sein:

   ```bash
   sudo usermod -aG gpio,spi $USER
   # danach einmal ab- und wieder anmelden (oder: newgrp gpio)
   ```

   Prüfen:

   ```bash
   groups            # muss "gpio" und "spi" enthalten
   ls -l /dev/spidev0.0 /dev/gpiochip0
   ```

   Auf Raspberry Pi OS existieren die udev-Regeln für `/dev/gpiochip*`
   und `/dev/spidev*` bereits. Falls nicht (z. B. anderes Debian):

   ```
   # /etc/udev/rules.d/99-gpio.rules
   SUBSYSTEM=="gpio*", KERNEL=="gpiochip*", GROUP="gpio", MODE="0660"
   KERNEL=="spidev*", GROUP="spi", MODE="0660"
   ```

## Verwendung

Kein `sudo` nötig, sofern der User in `gpio` und `spi` ist (s. o.).

```bash
# Standard: dauerhaft empfangen, Eingabezeilen senden
python3 lora_cli.py

# Einmal beim Start senden, danach in RX gehen
python3 lora_cli.py --send "HALLO vom RPi"

# Nur einmal senden und beenden (kein RX)
python3 lora_cli.py --tx-only "PING"

# Andere Funkparameter
python3 lora_cli.py --freq 868000000 --sf 7 --bw 125000 --cr 6 --power 22
```

Abbruch mit `Ctrl-C`.

## Funkparameter (Default, kompatibel mit dem ESP32-Gegenstück)

| Parameter        | Wert       |
|------------------|------------|
| Frequenz         | 868 MHz    |
| Bandbreite       | 125 kHz    |
| Spreading Factor | SF7        |
| Coding Rate      | 4/6        |
| Preamble         | 8          |
| CRC              | ON         |
| Sync Word        | 0x3444 (public) |

Alle Werte sind in `config.py` änderbar bzw. per CLI-Flag überschreibbar.

## Ausgabeformat

```
[15:22:41.183] RX (5 B)  RSSI=-72 dBm  SNR=9 dB  status=0x00
        hex : 48 41 4C 4C 4F
        text: HALLO
```

TX:

```
[15:22:55.902] TX (11 B): b'HALLO vom RPi'
[15:22:55.902] TX done  time-on-air ~ 61.7 ms
```

## Troubleshooting

* **`add_event_detect(...)` schlägt fehl / verlangt root / Pi 5** –
  Das originale `RPi.GPIO` ist im venv. Fix:
  `pip uninstall -y RPi.GPIO && pip install rpi-lgpio`.
* **`SX126x konnte nicht initialisiert werden`** – SPI nicht aktiv oder
  falsche Pins, oder LoRaRF findet das Modul nicht (BUSY bleibt HIGH).
  Prüfen: Verkabelung, `dtparam=spi=on`, User in `gpio,spi` Gruppe,
  `/dev/spidev0.0` und `/dev/gpiochip*` lesbar.
* **`PermissionError` auf `/dev/gpiochip*` oder `/dev/spidev0.0`** –
  User nicht in Gruppe `gpio`/`spi`, oder nach `usermod -aG` nicht
  ab-/angemeldet.
* **Immer CRC-Errors / `status != 0`** – Sync Word, SF, BW oder CR passen
  nicht zur Gegenstelle.
* **Sendet, aber keiner empfängt** – Antenne? Frequenzabweichung? Am
  besten mit dem RTL-SDR bei 868 MHz gegenchecken.
* **Kein TCXO** – Wenn dein DX-LR30 keinen TCXO hat, die Zeile
  `lora.setDio3TcxoCtrl(...)` in `lora_cli.py` auskommentieren.


## Kann man LoRaRF „richtig" auf libgpiod migrieren?

Ja, aber es ist viel Aufwand für wenig Ertrag: man müsste LoRaRF forken
und jeden `GPIO.setup`, `GPIO.output`, `GPIO.input`, `GPIO.add_event_detect`
in `LoRaRF/SX126x.py` durch `gpiod`-Aufrufe (v1 oder v2 API) ersetzen und
sich um Event-Threads, Line-Requests und die Freigabe kümmern.

**`rpi-lgpio` erreicht dasselbe Ziel ohne LoRaRF-Änderungen**: es
implementiert die komplette `RPi.GPIO`-API auf Basis von `lgpio` /
`libgpiod`. Damit läuft LoRaRF unverändert auf Pi 5 und ohne root.
Deshalb ist das hier der empfohlene Weg. Ein echter libgpiod-Fork
lohnt sich erst, wenn wir später den eigenen SX126x-Treiber laut
Ziel-Architektur bauen.
