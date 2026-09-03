
"""
LoRa Radio-Konfiguration fuer das DX-LR30-900M22S Modul (SX1261/SX1262)
am Raspberry Pi.

Pinbelegung (BCM):
  BUSY  = GPIO23  (Pin 16)
  NRST  = GPIO24  (Pin 18)
  DIO1  = GPIO25  (Pin 22)
  RXEN  = GPIO22  (Pin 15)
  TXEN  = via DIO2 (intern am Modul gebrueckt)  -> txenPin = -1
  NSS   = GPIO8   (Pin 24) via /dev/spidev0.0

SPI:
  Bus = 0
  CS  = 0
"""

# --- SPI ---
SPI_BUS = 0
SPI_CS = 0

# --- GPIO (BCM) ---
PIN_RESET = 24   # NRST
PIN_BUSY = 23    # BUSY
PIN_IRQ = 25     # DIO1
PIN_TXEN = -1    # DIO2 ist mit TXEN gebrueckt -> intern schalten
PIN_RXEN = 22    # RXEN

# --- Chip-/PA-Variante ---
# Modul "DX-LR30-900M22S" -> 22 dBm -> SX1262. Falls dein Modul wirklich
# ein SX1261 ist (max 15 dBm), per CLI --chip sx1261 umschalten.
CHIP_VARIANT = "sx1262"

# --- TCXO an DIO3 ---
# WICHTIG: viele DX-LR30-Klone haben KEINEN TCXO, nur einen XTAL. Wenn
# setDio3TcxoCtrl(...) auf so ein Modul angewendet wird, wartet der Chip
# auf einen TCXO-Start, der nie kommt -> keine TX-Frequenz -> kein TX_DONE.
# -> Default ist deshalb aus. Nur auf True setzen, wenn du sicher weisst,
#    dass dein Modul einen TCXO hat.
USE_TCXO = False

# --- Funkparameter ---
FREQUENCY_HZ = 868_000_000     # 868 MHz
TX_POWER_DBM = 22              # SX1262 bis 22 dBm, SX1261 bis 15 dBm
SPREADING_FACTOR = 7           # SF7
BANDWIDTH_HZ = 125_000         # 125 kHz
CODING_RATE = 6                # 4/6  -> LoRaRF: 5=4/5, 6=4/6, 7=4/7, 8=4/8
PREAMBLE_LENGTH = 8
CRC_ON = True
HEADER_EXPLICIT = True         # explicit header
SYNC_WORD = 0x1424             # Public LoRaWAN sync = 0x3444, privat = 0x1424
