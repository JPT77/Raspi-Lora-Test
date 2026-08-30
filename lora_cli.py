#!/usr/bin/env python3
"""
LoRa CLI fuer Raspberry Pi + DX-LR30-900M22S (SX1261/SX1262).

Verhalten:
  - Startet das Radio, konfiguriert LoRa (868 MHz / SF7 / BW125 / CR4/6 / CRC on)
  - Laeuft dauerhaft im Empfangsmodus (continuous RX)
  - Empfangene Pakete werden mit Zeitstempel, RSSI und SNR ausgegeben
  - Ueber die Konsole (stdin) kann jederzeit Text eingegeben werden;
    bei ENTER wird die Zeile als LoRa-Paket gesendet und danach automatisch
    wieder in RX gewechselt.

Aufruf:
    sudo python3 lora_cli.py
    sudo python3 lora_cli.py --send "HALLO"        # einmalig senden, dann RX
    sudo python3 lora_cli.py --tx-only "PING"      # einmalig senden, dann Exit
    sudo python3 lora_cli.py --freq 868000000 --sf 7 --bw 125000 --cr 6

Abbruch mit Ctrl-C.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import time
from datetime import datetime

# Preflight: sicherstellen, dass NICHT das originale RPi.GPIO geladen wird.
# LoRaRF importiert intern "import RPi.GPIO". Auf Pi 5 / Bookworm funktioniert
# das nur mit dem Drop-in "rpi-lgpio". Wir pruefen ueber die Package-Metadaten,
# ob rpi-lgpio installiert ist (RPi.GPIO und rpi-lgpio sind muturally exclusive).
try:
    from importlib.metadata import distribution, PackageNotFoundError
    try:
        distribution("rpi-lgpio")
        _has_rpi_lgpio = True
    except PackageNotFoundError:
        _has_rpi_lgpio = False
    try:
        distribution("RPi.GPIO")
        _has_rpi_gpio = True
    except PackageNotFoundError:
        _has_rpi_gpio = False
    if _has_rpi_gpio and not _has_rpi_lgpio:
        print("[warn] Es scheint das originale 'RPi.GPIO' installiert zu sein.")
        print("[warn] Auf Pi 5 / Bookworm oder ohne sudo bitte umstellen:")
        print("[warn]   pip uninstall -y RPi.GPIO && pip install rpi-lgpio")
except Exception:
    pass

from LoRaRF import SX126x
import RPi.GPIO as GPIO  # via rpi-lgpio Drop-in

import config as cfg


# ---------------------------------------------------------------------------
# RXEN manuell steuern
# ---------------------------------------------------------------------------
# LoRaRF toggelt RXEN nur, wenn BEIDE (TXEN und RXEN) gesetzt sind. Weil DIO2
# hier intern mit TXEN gebrueckt ist, uebergeben wir txenPin=-1 und muessen
# RXEN deshalb selbst treiben: HIGH waehrend RX, LOW waehrend TX/Standby.

_rxen_configured = False


def rxen_set(level_high: bool) -> None:
    """Setzt RXEN (GPIO22) auf HIGH (RX) oder LOW (TX/Standby)."""
    global _rxen_configured
    if not _rxen_configured:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(cfg.PIN_RXEN, GPIO.OUT, initial=GPIO.LOW)
        _rxen_configured = True
    GPIO.output(cfg.PIN_RXEN, GPIO.HIGH if level_high else GPIO.LOW)

# ---------------------------------------------------------------------------
# Radio-Setup
# ---------------------------------------------------------------------------

def build_radio(args) -> SX126x:
    """Initialisiert den SX126x mit den gewuenschten Parametern."""
    lora = SX126x()

    # Wenn --no-irq: DIO1 nicht als GPIO-Event registrieren, sondern LoRaRF
    # per SPI-Polling arbeiten lassen. Sehr nuetzlich zum Diagnostizieren, ob
    # DIO1 physisch/logisch am Pi ankommt.
    irq_pin = -1 if args.no_irq else cfg.PIN_IRQ

    print("[init] SPI bus={} cs={}  RESET=GPIO{}  BUSY=GPIO{}  DIO1={}  RXEN=GPIO{}   (mode: {})"
          .format(cfg.SPI_BUS, cfg.SPI_CS,
                  cfg.PIN_RESET, cfg.PIN_BUSY,
                  f"GPIO{cfg.PIN_IRQ}" if irq_pin != -1 else "polled (irq=-1)",
                  cfg.PIN_RXEN,
                  "polling" if args.no_irq else "IRQ"))

    if not lora.begin(cfg.SPI_BUS, cfg.SPI_CS,
                      cfg.PIN_RESET, cfg.PIN_BUSY, irq_pin,
                      cfg.PIN_TXEN, cfg.PIN_RXEN):
        raise RuntimeError("SX126x konnte nicht initialisiert werden "
                           "(SPI/GPIO pruefen, sudo verwenden).")

    # DIO2 ist auf dem Modul mit TXEN gebrueckt -> DIO2 als RF-Switch nutzen.
    lora.setDio2RfSwitch(True)

    # TCXO: viele DX-LR30-Module haben einen TCXO an DIO3. LoRaRF schaltet den
    # bei Bedarf ein. Manche Klone haben aber KEINEN TCXO -> --no-tcxo.
    if not args.no_tcxo:
        lora.setDio3TcxoCtrl(lora.DIO3_OUTPUT_1_8, lora.TCXO_DELAY_10)
        print("[init] TCXO ctrl an DIO3 aktiviert (1.8V, 10ms)")
    else:
        print("[init] TCXO ctrl deaktiviert (--no-tcxo)")

    lora.setFrequency(args.freq)

    # PA-Config je nach Chip-Variante:
    #   SX1261 -> max +15 dBm, TX_POWER_SX1261
    #   SX1262 -> max +22 dBm, TX_POWER_SX1262
    # ESPHome hat auf dem ESP32-Node "SX1261 V2D 2D02" gemeldet, aber das Modul
    # heisst "DX-LR30-900M22S" (22 dBm) -> das spricht fuer SX1262.
    # Deshalb hier per --chip umschaltbar.
    if args.chip == "sx1261":
        power = min(args.power, 15)
        lora.setTxPower(power, lora.TX_POWER_SX1261)
        print(f"[init] PA-Config = SX1261, TX power auf {power} dBm geklemmt")
    else:
        power = min(args.power, 22)
        lora.setTxPower(power, lora.TX_POWER_SX1262)
        print(f"[init] PA-Config = SX1262, TX power = {power} dBm")

    # LoRa Modulation: SF, BW, CR
    lora.setLoRaModulation(args.sf, args.bw, args.cr)

    # LoRa Packet: headerType, preambleLength, payloadLength, crc, invertIq
    header_type = lora.HEADER_EXPLICIT if cfg.HEADER_EXPLICIT else lora.HEADER_IMPLICIT
    lora.setLoRaPacket(header_type, cfg.PREAMBLE_LENGTH, 255, cfg.CRC_ON)

    lora.setSyncWord(cfg.SYNC_WORD)

    print("[init] freq={} Hz  SF={}  BW={} Hz  CR=4/{}  preamble={}  CRC={}  sync=0x{:04X}  power={} dBm"
          .format(args.freq, args.sf, args.bw, args.cr,
                  cfg.PREAMBLE_LENGTH, cfg.CRC_ON, cfg.SYNC_WORD, args.power))
    return lora


def run_diag(lora: SX126x) -> None:
    """Diagnose-Ausgabe: Chip-Status, IRQ-Register, DIO1-Pinlevel."""
    print("\n=== DIAG ===")
    try:
        st = lora.getStatus()
        print(f"  getStatus()        = 0x{st:02X}")
    except Exception as e:  # noqa: BLE001
        print(f"  getStatus()        -> ERROR: {e}")
    try:
        irq = lora.getIrqStatus()
        print(f"  getIrqStatus()     = 0x{irq:04X}")
    except Exception as e:  # noqa: BLE001
        print(f"  getIrqStatus()     -> ERROR: {e}")
    try:
        err = lora.getError()
        print(f"  getError()         = 0x{err:04X}")
    except Exception as e:  # noqa: BLE001
        print(f"  getError()         -> ERROR: {e}")
    # DIO1 GPIO-Level direkt lesen
    try:
        import RPi.GPIO as GPIO  # rpi-lgpio
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(cfg.PIN_IRQ, GPIO.IN)
        lvl = GPIO.input(cfg.PIN_IRQ)
        print(f"  GPIO{cfg.PIN_IRQ} (DIO1)     = {'HIGH' if lvl else 'LOW'}")
        GPIO.setup(cfg.PIN_BUSY, GPIO.IN)
        lvlb = GPIO.input(cfg.PIN_BUSY)
        print(f"  GPIO{cfg.PIN_BUSY} (BUSY)     = {'HIGH' if lvlb else 'LOW'}")
    except Exception as e:  # noqa: BLE001
        print(f"  GPIO-Read          -> ERROR: {e}")
    print("============\n")


# ---------------------------------------------------------------------------
# TX / RX Helpers
# ---------------------------------------------------------------------------

def send_message(lora: SX126x, message: bytes, tx_timeout_s: float = 10.0,
                 trace: bool = False) -> bool:
    """Sendet ein einzelnes LoRa-Paket.

    Wichtig: Wechsel RX_CONTINUOUS -> TX braucht ein explizites standby(),
    sonst startet die TX-Statemachine u.U. nicht sauber und wait() haengt
    fuer immer.

    Returns True bei TX done, False bei Timeout.
    """
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{now}] TX ({len(message)} B): {message!r}")

    # sauber in Standby, RXEN aus, dann senden
    lora.standby()
    rxen_set(False)

    lora.beginPacket()
    lora.write(list(message), len(message))
    lora.endPacket()

    if trace:
        # Live-Poll des Chip-Modus: zeigt, ob der Chip ueberhaupt in TX gegangen ist
        t0 = time.time()
        last_dump = 0.0
        while (time.time() - t0) < tx_timeout_s:
            st = lora.getStatus()
            irq = lora.getIrqStatus()
            mode = (st >> 4) & 0x07   # 0x2=STBY_RC 0x3=STBY_XOSC 0x4=FS 0x5=RX 0x6=TX
            mode_name = {0x2: "STBY_RC", 0x3: "STBY_XOSC", 0x4: "FS",
                         0x5: "RX", 0x6: "TX"}.get(mode, f"0x{mode:X}")
            if (time.time() - last_dump) > 0.25:
                print(f"    [trace] t+{time.time()-t0:5.2f}s  status=0x{st:02X} "
                      f"mode={mode_name}  irq=0x{irq:04X}  BUSY-via-SPI-ok")
                last_dump = time.time()
            if irq & 0x0001:  # IRQ_TX_DONE
                print(f"    [trace] TX_DONE gesetzt (irq=0x{irq:04X})")
                lora.clearIrqStatus(0x03FF)
                return True
            if irq & 0x0200:  # IRQ_TIMEOUT
                print(f"    [trace] TX_TIMEOUT gesetzt (irq=0x{irq:04X})")
                lora.clearIrqStatus(0x03FF)
                return False
            time.sleep(0.05)
        print(f"    [trace] Zeit abgelaufen ohne TX_DONE oder TIMEOUT-IRQ.")
        return False

    ok = lora.wait(timeout=tx_timeout_s)  # LoRaRF-timeout ist in SEKUNDEN
    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    if ok:
        print(f"[{now}] TX done  time-on-air ~ {lora.transmitTime():.1f} ms")
    else:
        print(f"[{now}] TX TIMEOUT nach {tx_timeout_s:.1f}s (kein TX_DONE-IRQ). "
              f"DIO1 verkabelt? Modul in korrektem State?")
    return ok

    print(f"[{now}] TX done  time-on-air ~ {lora.transmitTime():.1f} ms")


def start_rx_continuous(lora: SX126x) -> None:
    """Setzt das Modul in kontinuierlichen Empfang und aktiviert RXEN."""
    lora.standby()
    rxen_set(True)   # RX-Frontend-LNA an
    lora.request(lora.RX_CONTINUOUS)


def try_read_packet(lora: SX126x, show_errors: bool) -> None:
    """Prueft, ob ein Paket empfangen wurde und gibt es aus.

    LoRaRF feuert die RX-IRQ auch bei HEADER_ERR und CRC_ERR. Solche
    "Pakete" sind reine Rauschtrigger und werden per Default unterdrueckt.
    """
    if not lora.available():
        return

    # Erst Status pruefen (LoRaRF loescht ihn beim naechsten wait()-Zyklus)
    status = lora.status()

    length = lora.available()
    payload = bytearray()
    while lora.available():
        payload.append(lora.read())

    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    # Fehlerhafte Pakete (Header-/CRC-Error) sind meistens Rauschen
    if status in (lora.STATUS_HEADER_ERR, lora.STATUS_CRC_ERR):
        if show_errors:
            print(f"[{now}] RX-ERR ({length} B) status=0x{status:02X} "
                  f"({'HEADER_ERR' if status == lora.STATUS_HEADER_ERR else 'CRC_ERR'}) "
                  f"RSSI={lora.packetRssi()} dBm SNR={lora.snr()} dB  -- vermutlich Rauschen")
        return

    rssi = lora.packetRssi()
    snr = lora.snr()

    # Als Text darstellen wenn druckbar, sonst als hex
    try:
        text = payload.decode("utf-8")
        printable = all(32 <= b < 127 or b in (9, 10, 13) for b in payload)
    except UnicodeDecodeError:
        text = ""
        printable = False

    hex_dump = " ".join(f"{b:02X}" for b in payload)

    print(f"\
[{now}] RX ({length} B)  RSSI={rssi} dBm  SNR={snr} dB  status=0x{status:02X}")
    print(f"        hex : {hex_dump}")
    if printable and text:
        print(f"        text: {text}")


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    print("\
[main] Abbruch angefordert...")


def main() -> int:
    parser = argparse.ArgumentParser(description="LoRa CLI (SX126x / LoRaRF)")
    parser.add_argument("--freq", type=int, default=cfg.FREQUENCY_HZ,
                        help=f"Frequenz in Hz (default {cfg.FREQUENCY_HZ})")
    parser.add_argument("--sf", type=int, default=cfg.SPREADING_FACTOR,
                        help=f"Spreading Factor 5..12 (default {cfg.SPREADING_FACTOR})")
    parser.add_argument("--bw", type=int, default=cfg.BANDWIDTH_HZ,
                        help=f"Bandbreite in Hz (default {cfg.BANDWIDTH_HZ})")
    parser.add_argument("--cr", type=int, default=cfg.CODING_RATE,
                        help=f"Coding Rate 5..8 (default {cfg.CODING_RATE})")
    parser.add_argument("--power", type=int, default=cfg.TX_POWER_DBM,
                        help=f"TX Power in dBm (default {cfg.TX_POWER_DBM})")
    parser.add_argument("--send", type=str, default=None,
                        help="Einmalig senden und dann in RX gehen.")
    parser.add_argument("--tx-only", type=str, default=None,
                        help="Einmalig senden und danach beenden (kein RX).")
    parser.add_argument("--show-errors", action="store_true",
                        help="Auch Rauschtrigger mit Header-/CRC-Fehler anzeigen.")
    parser.add_argument("--tx-timeout", type=float, default=10.0,
                        help="TX-Timeout in Sekunden (default 10).")
    parser.add_argument("--no-irq", action="store_true",
                        help="DIO1 nicht als GPIO-Interrupt verwenden. LoRaRF pollt "
                             "stattdessen die IRQ-Register per SPI. Diagnose-Modus, "
                             "wenn TX/RX-Done nie ankommt.")
    parser.add_argument("--diag", action="store_true",
                        help="Vor dem Start Chip-Diagnose ausgeben (Status/IRQ/DIO1).")
    parser.add_argument("--chip", choices=["sx1261", "sx1262"], default=cfg.CHIP_VARIANT,
                        help="PA-Config-Variante. SX1261 = max 15 dBm, SX1262 = max 22 dBm. "
                             "Falsche Wahl kann verhindern, dass TX_DONE feuert!")
    parser.add_argument("--no-tcxo", action="store_true", default=(not cfg.USE_TCXO),
                        help="TCXO-Konfiguration nicht setzen (Modul hat XTAL statt TCXO). "
                             "Default: aus. Mit --tcxo explizit einschalten.")
    parser.add_argument("--tcxo", dest="no_tcxo", action="store_false",
                        help="TCXO-Konfiguration explizit einschalten.")
    parser.add_argument("--trace-tx", action="store_true",
                        help="Waehrend TX den Chip-Modus alle 250ms per SPI pollen und ausgeben.")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_sigint)
    signal.signal(signal.SIGTERM, _handle_sigint)

    try:
        lora = build_radio(args)
    except Exception as e:  # noqa: BLE001
        print(f"[fatal] Radio-Init fehlgeschlagen: {e}", file=sys.stderr)
        return 2

    if args.diag:
        run_diag(lora)

    # --- TX-only Modus ---
    if args.tx_only is not None:
        send_message(lora, args.tx_only.encode("utf-8"), args.tx_timeout, trace=args.trace_tx)
        lora.end()
        return 0

    # --- Optional einmalig senden vor RX ---
    if args.send is not None:
        send_message(lora, args.send.encode("utf-8"), args.tx_timeout, trace=args.trace_tx)

    print("[main] gehe in RX_CONTINUOUS. Tippe Text + ENTER zum Senden. Ctrl-C zum Beenden.")
    start_rx_continuous(lora)

    stdin_is_tty = sys.stdin.isatty()

    try:
        while not _stop:
            # 1) empfangenes Paket abholen
            try_read_packet(lora, args.show_errors)

            # 2) stdin nicht-blockierend pruefen
            if stdin_is_tty:
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if ready:
                    line = sys.stdin.readline()
                    if line == "":  # EOF
                        break
                    line = line.rstrip("\r\n")
                    if not line:
                        continue
                    # TX -> danach wieder RX
                    try:
                        send_message(lora, line.encode("utf-8"), args.tx_timeout, trace=args.trace_tx)
                    except KeyboardInterrupt:
                        print("\n[main] TX unterbrochen.")
                        break
                    if _stop:
                        break
            else:
                time.sleep(0.05)

    finally:
        try:
            lora.end()
        except Exception:  # noqa: BLE001
            pass
        print("[main] Radio geschlossen. Bye.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
