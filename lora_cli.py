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

    lora.setSyncWord(args.syncword)

    print("[init] freq={} Hz  SF={}  BW={} Hz  CR=4/{}  preamble={}  CRC={}  sync=0x{:04X}  power={} dBm"
          .format(args.freq, args.sf, args.bw, args.cr,
                  cfg.PREAMBLE_LENGTH, cfg.CRC_ON, args.syncword, args.power))
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
#    ""Sendet ein einzelnes LoRa-Paket.
#
#    Wichtig: Wechsel RX_CONTINUOUS -> TX braucht ein explizites standby(),
#    sonst startet die TX-Statemachine u.U. nicht sauber und wait() haengt
#    fuer immer.
    """Sendet ein einzelnes LoRa-Paket per SPI-Polling (kein DIO1 noetig).

    Returns True bei TX done, False bei Timeout.
    """

    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{now}] TX ({len(message)} B): {message!r}")

    # sauber in Standby, RXEN aus, IRQ-Register loeschen
    lora.standby()
    rxen_set(False)
    lora.clearIrqStatus(0x03FF)

    lora.beginPacket()
    lora.write(list(message), len(message))
    lora.endPacket()


    t0 = time.time()
    last_dump = 0.0
    while (time.time() - t0) < tx_timeout_s:
        irq = lora.getIrqStatus()
        if trace and (time.time() - last_dump) > 0.25:
            st = lora.getStatus()
            mode = (st >> 4) & 0x07
            mode_name = {0x2: "STBY_RC", 0x3: "STBY_XOSC", 0x4: "FS",
                         0x5: "RX", 0x6: "TX"}.get(mode, f"0x{mode:X}")
            print(f"    [trace] t+{time.time()-t0:5.2f}s  status=0x{st:02X} "
                  f"mode={mode_name}  irq=0x{irq:04X}")
            last_dump = time.time()
        if irq & 0x0001:  # IRQ_TX_DONE
            lora.clearIrqStatus(0x03FF)
            now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            toa = (time.time() - t0) * 1000
            print(f"[{now}] TX done  (~{toa:.1f} ms)")
            return True
        if irq & 0x0200:  # IRQ_TIMEOUT
            lora.clearIrqStatus(0x03FF)
            print(f"[{now}] TX TIMEOUT gemeldet vom Chip.")
            return False
        time.sleep(0.02)

    print(f"[{now}] TX TIMEOUT nach {tx_timeout_s:.1f}s (kein TX_DONE / kein Chip-Timeout).")
    return False


# SX126x IRQ Bits
_IRQ_TX_DONE = 0x0001
_IRQ_RX_DONE = 0x0002
_IRQ_PREAMBLE_DETECTED = 0x0004
_IRQ_SYNC_WORD_VALID = 0x0008
_IRQ_HEADER_VALID = 0x0010
_IRQ_HEADER_ERR = 0x0020
_IRQ_CRC_ERR = 0x0040
_IRQ_TIMEOUT = 0x0200

_IRQ_NAMES = [
    (0x0001, "TX_DONE"),
    (0x0002, "RX_DONE"),
    (0x0004, "PREAMBLE"),
    (0x0008, "SYNC_VALID"),
    (0x0010, "HDR_VALID"),
    (0x0020, "HDR_ERR"),
    (0x0040, "CRC_ERR"),
    (0x0200, "TIMEOUT"),
]


def _decode_irq(irq: int) -> str:
    parts = [name for bit, name in _IRQ_NAMES if irq & bit]
    return "+".join(parts) if parts else "-"


def start_rx_continuous(lora: SX126x, sniff: bool = False) -> None:
    """Setzt das Modul in kontinuierlichen Empfang - ohne LoRaRF-IRQ-Callbacks.

    Wir umgehen lora.request() bewusst, damit LoRaRF nicht versucht,
    add_event_detect() auf DIO1 zu registrieren (funktioniert mit
    rpi-lgpio unzuverlaessig). Stattdessen konfigurieren wir DIO1 nur als
    IRQ-Quelle im Chip und pollen getIrqStatus() per SPI.
    Im Sniff-Modus werden alle relevanten IRQs aktiviert, damit man auch
    Preamble-/Sync-Trigger sieht (Diagnose bei Sync-Word-Mismatch etc.).
    """
    lora.standby()
    rxen_set(True)                  # RX-Frontend-LNA an
    lora.clearIrqStatus(0x03FF)
    # DIO1 als IRQ fuer RX_DONE / HEADER_ERR / CRC_ERR / TIMEOUT (Chip-intern,
    # wir lesen es aber per SPI aus, nicht ueber DIO1-GPIO)
    if sniff:
        irq_mask = (_IRQ_RX_DONE | _IRQ_PREAMBLE_DETECTED | _IRQ_SYNC_WORD_VALID
                    | _IRQ_HEADER_VALID | _IRQ_HEADER_ERR | _IRQ_CRC_ERR | _IRQ_TIMEOUT)
    else:
        irq_mask = _IRQ_RX_DONE | _IRQ_HEADER_ERR | _IRQ_CRC_ERR | _IRQ_TIMEOUT
    lora.setDioIrqParams(irq_mask, irq_mask, 0x0000, 0x0000)
    # RX continuous mode
    lora.setRx(0xFFFFFF)


def poll_rx(lora: SX126x, show_errors: bool, sniff: bool = False) -> None:
    """Prueft per SPI, ob ein RX-Ereignis vorliegt, und liest ggf. das Paket.
    Wird von der Main-Loop alle paar ms aufgerufen. Keine GPIO-Interrupts.
    """
    irq = lora.getIrqStatus()
    if irq == 0x0000:
        return

    now = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    # Wenn ein RX_DONE dabei ist -> normale Verarbeitung + Buffer lesen
    if irq & _IRQ_RX_DONE:
        length, offset = lora.getRxBufferStatus()
        payload = bytes(lora.readBuffer(offset, length)) if length else b""
        rssi = lora.packetRssi()
        snr = lora.snr()
        # IRQ-Register loeschen, damit naechste RX-Aktion sauber startet
        lora.clearIrqStatus(0x03FF)

        try:
            text = payload.decode("utf-8")
            printable = all(32 <= b < 127 or b in (9, 10, 13) for b in payload)
        except UnicodeDecodeError:
            text = ""
            printable = False
        hex_dump = " ".join(f"{b:02X}" for b in payload)

        crc_ok = not (irq & _IRQ_CRC_ERR)
        hdr_ok = not (irq & _IRQ_HEADER_ERR)
        tag = "RX" if crc_ok and hdr_ok else "RX-BAD"
        print(f"\n[{now}] {tag} ({length} B)  RSSI={rssi} dBm  SNR={snr} dB  irq=0x{irq:04X} [{_decode_irq(irq)}]")
        print(f"        hex : {hex_dump}")
        if printable and text:
            print(f"        text: {text}")

        return

    # Kein RX_DONE - Sub-Events (Preamble, Sync, HeaderErr, CrcErr, Timeout)
    if sniff or show_errors:
        # bei HeaderErr / CrcErr kann trotzdem was im Buffer stehen -> hex dumpen
        extra = ""
        if irq & (_IRQ_HEADER_ERR | _IRQ_CRC_ERR):
            try:
                length, offset = lora.getRxBufferStatus()
                if length:
                    payload = bytes(lora.readBuffer(offset, length))
                    extra = "  hex=" + " ".join(f"{b:02X}" for b in payload)
            except Exception:  # noqa: BLE001
                pass
            rssi = lora.packetRssi()
            snr = lora.snr()
            print(f"[{now}] RX-ERR irq=0x{irq:04X} [{_decode_irq(irq)}]  RSSI={rssi} dBm  SNR={snr} dB{extra}")
        elif sniff:
            # nur Preamble / Sync / HdrValid -> nur kurz melden, ohne Buffer
            rssi = lora.rssiInst()
            print(f"[{now}] sniff irq=0x{irq:04X} [{_decode_irq(irq)}]  RSSI(inst)={rssi} dBm")
        lora.clearIrqStatus(0x03FF)


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

_stop = False


def _handle_sigint(signum, frame):
    global _stop
    _stop = True
    print("\
[main] Abbruch angefordert...")


def send_burst(lora: SX126x, message: bytes, repeat: int, interval_s: float,
               tx_timeout_s: float, trace: bool, show_errors: bool, sniff: bool) -> None:
    """Sendet die Nachricht mehrmals im Abstand von interval_s Sekunden.

    Zwischen den Aussendungen wird das Modul wieder in RX_CONTINUOUS gesetzt
    und dort weiter gepollt, so dass Empfangsereignisse und Ctrl-C sichtbar
    bleiben. Praktisch, um mit dem RTL-SDR bei 868 MHz zu sehen, ob wirklich
    gesendet wird.
    """
    for i in range(repeat):
        if _stop:
            break
        if repeat > 1:
            print(f"[burst] {i + 1}/{repeat}")
        send_message(lora, message, tx_timeout_s, trace=trace)

        if i == repeat - 1 or _stop:
            break

        # zurueck in RX_CONTINUOUS und interval_s abwarten, dabei RX pollen
        start_rx_continuous(lora, sniff=sniff)
        t_end = time.time() + interval_s
        print(f"[burst] warte {interval_s:.1f}s bis zur naechsten Wiederholung "
              f"(RX bleibt aktiv)")
        while time.time() < t_end and not _stop:
            poll_rx(lora, show_errors, sniff=sniff)
            time.sleep(0.02)


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
                        help="Auch Rauschtrigger mit Header-/CRC-Fehler anzeigen (mit Buffer-Dump).")
    parser.add_argument("--sniff", action="store_true",
                        help="Sniff-Modus: alle RX-IRQs auflisten (Preamble, Sync, HdrValid, "
                             "HdrErr, CrcErr, RxDone) - inkl. Buffer-Hex bei jedem Trigger.")
    parser.add_argument("--tx-timeout", type=float, default=10.0,
                        help="TX-Timeout in Sekunden (default 10).")
    parser.add_argument("--no-irq", action="store_true", default=True,
                        help="[Default] DIO1 nicht als GPIO-Interrupt verwenden - "
                             "es wird durchgehend per SPI gepollt. Zuverlaessig auf Pi 5 / rpi-lgpio.")
    parser.add_argument("--use-irq", dest="no_irq", action="store_false",
                        help="DIO1 als GPIO-Interrupt registrieren (nicht empfohlen).")
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
    parser.add_argument("--repeat", type=int, default=5,
                        help="Wie oft eine TX-Nachricht wiederholt wird (default 5). "
                             "Praktisch fuer RTL-SDR-Beobachtung.")
    parser.add_argument("--repeat-interval", type=float, default=10.0,
                        help="Sekunden zwischen den Wiederholungen (default 10).")
    parser.add_argument("--syncword", type=lambda s: int(s, 0), default=cfg.SYNC_WORD,
                        help=f"LoRa Sync-Word (default 0x{cfg.SYNC_WORD:04X}). "
                             "Typische Werte: 0x1424 (RadioLib/ESPHome private), "
                             "0x3444 (public/LoRaWAN).")
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
        send_burst(lora, args.tx_only.encode("utf-8"),
                   repeat=args.repeat, interval_s=args.repeat_interval,
                   tx_timeout_s=args.tx_timeout, trace=args.trace_tx,
                   show_errors=args.show_errors, sniff=args.sniff)
        lora.end()
        return 0

    # --- Optional Burst vor RX ---
    if args.send is not None:
        send_burst(lora, args.send.encode("utf-8"),
                   repeat=args.repeat, interval_s=args.repeat_interval,
                   tx_timeout_s=args.tx_timeout, trace=args.trace_tx,
                   show_errors=args.show_errors, sniff=args.sniff)

    print("[main] gehe in RX_CONTINUOUS. Tippe Text + ENTER zum Senden. Ctrl-C zum Beenden.")
    start_rx_continuous(lora, sniff=args.sniff)
    stdin_is_tty = sys.stdin.isatty()

    try:
        while not _stop:
            # 1) empfangenes Paket abholen
            #try_read_packet(lora, args.show_errors)
            poll_rx(lora, args.show_errors, sniff=args.sniff)

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
