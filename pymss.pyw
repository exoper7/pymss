import sys
import os
import csv
import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import serial
import serial.tools.list_ports # Do wykrywania portów COM
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QLineEdit, QPushButton, QComboBox, QLabel, QCheckBox,
    QTableWidget, QTableWidgetItem, QDialog, QDialogButtonBox, QAbstractItemView,
    QMessageBox, QSpinBox, QPlainTextEdit, QFileDialog
)
from PyQt6.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt6.QtGui import QIcon, QColor, QFont, QTextCursor, QTextCharFormat, QKeySequence, QShortcut


def resource_path(name):
    """Ścieżka do zasobu - działa zarówno ze skryptu, jak i z exe zbudowanego przez PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# --- Definicje tabel, typów danych i kolejności bajtów ---

TABLES = { # kod: etykieta (nazwy jak w pymms)
    "CO": "0x01 (Coils)",
    "DI": "0x02 (Discrete Inputs)",
    "HR": "0x03 (Holding Registers)",
    "IR": "0x04 (Input Registers)",
}

UNDEFINED_MODES = { # kod: etykieta - reakcja na zapytanie o niezdefiniowane rejestry
    "zeros": "Respond with zeros",
    "ignore": "Ignore request (no response)",
    "error": "Respond with exception 02 (Illegal Data Address)",
}
BIT_TABLES = ("CO", "DI")
TABLE_ORDER = list(TABLES.keys())

TABLE_CSV_NAMES = {"CO": "coil", "DI": "discrete_input", "IR": "input_register", "HR": "holding_register"}
TABLE_ALIASES = {
    "coil": "CO", "coils": "CO", "co": "CO", "0x": "CO", "0": "CO",
    "discrete_input": "DI", "discrete_inputs": "DI", "discrete input": "DI", "di": "DI", "1x": "DI", "1": "DI",
    "input_register": "IR", "input_registers": "IR", "input register": "IR", "ir": "IR", "3x": "IR", "3": "IR",
    "holding_register": "HR", "holding_registers": "HR", "holding register": "HR", "hr": "HR", "4x": "HR", "4": "HR",
}

DATA_TYPES = { # kod: (etykieta, liczba rejestrów, format struct)
    "UINT16": ("UINT16 (16-bit Unsigned)", 1, ">H"),
    "INT16": ("INT16 (16-bit Signed)", 1, ">h"),
    "UINT32": ("UINT32 (32-bit Unsigned)", 2, ">I"),
    "INT32": ("INT32 (32-bit Signed)", 2, ">i"),
    "FLOAT32": ("FLOAT32 (32-bit Float)", 2, ">f"),
    "UINT64": ("UINT64 (64-bit Unsigned)", 4, ">Q"),
    "INT64": ("INT64 (64-bit Signed)", 4, ">q"),
    "FLOAT64": ("FLOAT64 (64-bit Double)", 4, ">d"),
}

BYTE_ORDERS = { # kod: etykieta (te same nazwy co w pymms)
    "ABCD": "ABCD (No Swap)",
    "CDAB": "CDAB (Word Swap)",
    "BADC": "BADC (Byte Swap within Words)",
    "DCBA": "DCBA (Full Byte Reverse)",
}

FUNCTION_NAMES = {
    0x01: "Read Coils",
    0x02: "Read Discrete Inputs",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x05: "Write Single Coil",
    0x06: "Write Single Register",
    0x0F: "Write Multiple Coils",
    0x10: "Write Multiple Registers",
    0x16: "Mask Write Register",
    0x17: "Read/Write Multiple Registers",
}

EXCEPTION_NAMES = {
    0x01: "Illegal Function",
    0x02: "Illegal Data Address",
    0x03: "Illegal Data Value",
    0x04: "Slave Device Failure",
}


# --- Funkcje pomocnicze do konwersji danych ---
# Kolejność bajtów zgodna z pymms, dzięki czemu oba narzędzia interpretują dane identycznie.

def apply_byte_order(data, order):
    """Przestawia bajty 32/64-bitowej wartości. Operacja jest swoją własną odwrotnością (odczyt = zapis)."""
    n = len(data)
    if n <= 2 or order == "ABCD":
        return data
    if order == "CDAB": # zamiana połówek (32-bit: W1 W0, 64-bit: W2 W3 W0 W1)
        half = n // 2
        return data[half:] + data[:half]
    if order == "BADC": # zamiana bajtów w każdym słowie
        return b"".join(data[i:i+2][::-1] for i in range(0, n, 2))
    if order == "DCBA": # pełne odwrócenie
        return data[::-1]
    return data

def register_count(dtype):
    return 1 if dtype == "BOOL" else DATA_TYPES[dtype][1]

def parse_value(dtype, text):
    """Zamienia tekst na wartość danego typu. Rzuca ValueError przy błędnych danych."""
    text = str(text).strip()
    if dtype == "BOOL":
        t = text.lower()
        if t in ("1", "true", "on", "yes", "t"):
            return True
        if t in ("0", "false", "off", "no", "f", ""):
            return False
        raise ValueError(f"Invalid boolean value: '{text}'")
    if text == "":
        return 0.0 if dtype.startswith("FLOAT") else 0
    if dtype.startswith("FLOAT"):
        return float(text.replace(",", "."))
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(text, 0) # 0x..., 0b...
    except ValueError:
        f = float(text.replace(",", "."))
        if f.is_integer():
            return int(f)
        raise ValueError(f"'{text}' is not an integer value")

def value_to_registers(dtype, order, value):
    try:
        raw = struct.pack(DATA_TYPES[dtype][2], value)
    except struct.error:
        raise ValueError(f"Value {value} is out of range for {dtype}")
    raw = apply_byte_order(raw, order)
    return [int.from_bytes(raw[i:i+2], "big") for i in range(0, len(raw), 2)]

def registers_to_value(dtype, order, registers):
    raw = b"".join(r.to_bytes(2, "big") for r in registers)
    raw = apply_byte_order(raw, order)
    return struct.unpack(DATA_TYPES[dtype][2], raw)[0]

def format_value(dtype, value):
    if dtype == "BOOL":
        return "True" if value else "False"
    if dtype == "FLOAT32":
        return f"{value:.7g}"
    if dtype == "FLOAT64":
        return repr(value)
    return str(value)

def pack_bits(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, bit in enumerate(bits):
        if bit:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)

def unpack_bits(data, count):
    return [bool((data[i // 8] >> (i % 8)) & 1) for i in range(count)]

def to_hex(data):
    return " ".join(f"{b:02X}" for b in data)


# --- CRC16 Modbus (RTU) ---

def _make_crc_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        table.append(crc)
    return table

_CRC_TABLE = _make_crc_table()

def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc

def crc_ok(frame):
    return len(frame) >= 4 and crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))

def rtu_request_length(buf):
    """Oczekiwana długość ramki zapytania RTU na podstawie kodu funkcji (None - nieznana/za mało danych)."""
    if len(buf) < 2:
        return None
    fc = buf[1]
    if fc in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06):
        return 8
    if fc in (0x0F, 0x10):
        return 9 + buf[6] if len(buf) >= 7 else None
    if fc == 0x16:
        return 10
    if fc == 0x17:
        return 13 + buf[10] if len(buf) >= 11 else None
    return None


# --- Model danych slave'a ---

class ModbusError(Exception):
    def __init__(self, code):
        super().__init__(EXCEPTION_NAMES.get(code, f"Exception {code}"))
        self.code = code


class IgnoreRequest(Exception):
    """Zapytanie ma pozostać bez odpowiedzi (niezdefiniowane rejestry w trybie 'ignore')."""


@dataclass(eq=False)
class RegisterDef:
    table: str          # CO / DI / IR / HR
    address: int
    dtype: str          # BOOL / UINT16 / ... / FLOAT64
    order: str = "ABCD"
    description: str = ""

    @property
    def count(self):
        return register_count(self.dtype)

    @property
    def addresses(self):
        return range(self.address, self.address + self.count)


class DataStore:
    """Pamięć slave'a (thread-safe). Definicje rejestrów są widokami na surową pamięć."""
    def __init__(self):
        self.lock = threading.RLock()
        self.mem = {t: {} for t in TABLE_ORDER}
        self.defs = []
        self.undefined_mode = "zeros" # klucz z UNDEFINED_MODES
        self.written = {}   # (tabela, adres) -> czas ostatniego zapisu przez mastera
        self.dirty = False

    # --- Zarządzanie definicjami (GUI) ---

    def find_overlap(self, new, ignore=None):
        with self.lock:
            for d in self.defs:
                if d is ignore or d.table != new.table:
                    continue
                if d.address < new.address + new.count and new.address < d.address + d.count:
                    return d
        return None

    def _encode(self, d, value):
        if d.table in BIT_TABLES:
            return [bool(value)]
        return value_to_registers(d.dtype, d.order, value)

    def _add(self, d, value):
        if d.address + d.count > 65536:
            raise ValueError(f"Address {d.address} + {d.count} registers exceeds 65535")
        words = self._encode(d, value)
        conflict = self.find_overlap(d)
        if conflict:
            raise ValueError(f"{TABLES[d.table]} {d.address} overlaps with existing definition at address {conflict.address} ({conflict.dtype})")
        mem = self.mem[d.table]
        for i, w in enumerate(words):
            mem[d.address + i] = w
        self.defs.append(d)
        self.defs.sort(key=lambda x: (TABLE_ORDER.index(x.table), x.address))

    def _remove(self, d):
        mem = self.mem[d.table]
        for a in d.addresses:
            mem.pop(a, None)
            self.written.pop((d.table, a), None)
        self.defs = [x for x in self.defs if x is not d]

    def add_definition(self, d, value):
        with self.lock:
            self._add(d, value)

    def replace_definition(self, old, new, value):
        with self.lock:
            self._encode(new, value) # walidacja przed zmianą
            conflict = self.find_overlap(new, ignore=old)
            if conflict:
                raise ValueError(f"Address range overlaps with existing definition at address {conflict.address} ({conflict.dtype})")
            self._remove(old)
            self._add(new, value)

    def remove_definitions(self, defs):
        with self.lock:
            for d in defs:
                self._remove(d)

    def clear(self):
        with self.lock:
            self.mem = {t: {} for t in TABLE_ORDER}
            self.defs = []
            self.written = {}

    def get_value(self, d):
        with self.lock:
            mem = self.mem[d.table]
            if d.table in BIT_TABLES:
                return bool(mem.get(d.address, False))
            return registers_to_value(d.dtype, d.order, [mem.get(a, 0) for a in d.addresses])

    def set_value(self, d, value):
        with self.lock:
            words = self._encode(d, value)
            for i, w in enumerate(words):
                self.mem[d.table][d.address + i] = w

    def raw_text(self, d):
        with self.lock:
            mem = self.mem[d.table]
            if d.table in BIT_TABLES:
                return "1" if mem.get(d.address, False) else "0"
            return " ".join(f"0x{mem.get(a, 0):04X}" for a in d.addresses)

    def last_write_time(self, d):
        with self.lock:
            return max(self.written.get((d.table, a), 0.0) for a in d.addresses)

    def consume_dirty(self):
        with self.lock:
            dirty, self.dirty = self.dirty, False
            return dirty

    # --- Dostęp z protokołu Modbus (wątki serwera) ---

    def _check(self, table, address, count):
        if address + count > 65536:
            raise ModbusError(0x02)
        if self.undefined_mode != "zeros":
            mem = self.mem[table]
            for a in range(address, address + count):
                if a not in mem:
                    if self.undefined_mode == "ignore":
                        raise IgnoreRequest()
                    raise ModbusError(0x02)

    def _mark_written(self, table, address, count):
        now = time.monotonic()
        for a in range(address, address + count):
            self.written[(table, a)] = now
        self.dirty = True

    def read_bits(self, table, address, count):
        with self.lock:
            self._check(table, address, count)
            mem = self.mem[table]
            return [bool(mem.get(a, False)) for a in range(address, address + count)]

    def read_registers(self, table, address, count):
        with self.lock:
            self._check(table, address, count)
            mem = self.mem[table]
            return [mem.get(a, 0) for a in range(address, address + count)]

    def write_coils(self, address, values):
        with self.lock:
            self._check("CO", address, len(values))
            for i, v in enumerate(values):
                self.mem["CO"][address + i] = bool(v)
            self._mark_written("CO", address, len(values))

    def write_registers(self, address, values):
        with self.lock:
            self._check("HR", address, len(values))
            for i, v in enumerate(values):
                self.mem["HR"][address + i] = v & 0xFFFF
            self._mark_written("HR", address, len(values))

    def mask_write_register(self, address, and_mask, or_mask):
        with self.lock:
            self._check("HR", address, 1)
            current = self.mem["HR"].get(address, 0)
            self.mem["HR"][address] = (current & and_mask) | (or_mask & ~and_mask & 0xFFFF)
            self._mark_written("HR", address, 1)


# --- Obsługa protokołu (PDU) ---

def process_pdu(store, pdu):
    """Przetwarza PDU zapytania i zwraca PDU odpowiedzi (normalnej lub wyjątku) albo None - brak odpowiedzi."""
    fc = pdu[0]
    try:
        if fc in (0x01, 0x02):
            if len(pdu) != 5:
                raise ModbusError(0x03)
            address, count = struct.unpack(">HH", pdu[1:5])
            if not 1 <= count <= 2000:
                raise ModbusError(0x03)
            bits = store.read_bits("CO" if fc == 0x01 else "DI", address, count)
            data = pack_bits(bits)
            return bytes([fc, len(data)]) + data

        if fc in (0x03, 0x04):
            if len(pdu) != 5:
                raise ModbusError(0x03)
            address, count = struct.unpack(">HH", pdu[1:5])
            if not 1 <= count <= 125:
                raise ModbusError(0x03)
            regs = store.read_registers("HR" if fc == 0x03 else "IR", address, count)
            return bytes([fc, 2 * count]) + struct.pack(f">{count}H", *regs)

        if fc == 0x05:
            if len(pdu) != 5:
                raise ModbusError(0x03)
            address, value = struct.unpack(">HH", pdu[1:5])
            if value not in (0x0000, 0xFF00):
                raise ModbusError(0x03)
            store.write_coils(address, [value == 0xFF00])
            return bytes(pdu)

        if fc == 0x06:
            if len(pdu) != 5:
                raise ModbusError(0x03)
            address, value = struct.unpack(">HH", pdu[1:5])
            store.write_registers(address, [value])
            return bytes(pdu)

        if fc == 0x0F:
            if len(pdu) < 6:
                raise ModbusError(0x03)
            address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= count <= 1968 or byte_count != (count + 7) // 8 or len(pdu) != 6 + byte_count:
                raise ModbusError(0x03)
            store.write_coils(address, unpack_bits(pdu[6:], count))
            return bytes(pdu[:5])

        if fc == 0x10:
            if len(pdu) < 6:
                raise ModbusError(0x03)
            address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
            if not 1 <= count <= 123 or byte_count != 2 * count or len(pdu) != 6 + byte_count:
                raise ModbusError(0x03)
            store.write_registers(address, list(struct.unpack(f">{count}H", pdu[6:])))
            return bytes(pdu[:5])

        if fc == 0x16:
            if len(pdu) != 7:
                raise ModbusError(0x03)
            address, and_mask, or_mask = struct.unpack(">HHH", pdu[1:7])
            store.mask_write_register(address, and_mask, or_mask)
            return bytes(pdu)

        if fc == 0x17:
            if len(pdu) < 10:
                raise ModbusError(0x03)
            r_addr, r_count, w_addr, w_count, byte_count = struct.unpack(">HHHHB", pdu[1:10])
            if (not 1 <= r_count <= 125 or not 1 <= w_count <= 121
                    or byte_count != 2 * w_count or len(pdu) != 10 + byte_count):
                raise ModbusError(0x03)
            with store.lock: # zapis wykonywany przed odczytem (zgodnie ze specyfikacją)
                store._check("HR", r_addr, r_count)
                store.write_registers(w_addr, list(struct.unpack(f">{w_count}H", pdu[10:])))
                regs = store.read_registers("HR", r_addr, r_count)
            return bytes([fc, 2 * r_count]) + struct.pack(f">{r_count}H", *regs)

        raise ModbusError(0x01)
    except IgnoreRequest:
        return None
    except ModbusError as e:
        return bytes([fc | 0x80, e.code])
    except Exception:
        return bytes([fc | 0x80, 0x04])


def _short_list(values, limit=16):
    text = ", ".join(str(v) for v in values[:limit])
    return f"[{text}{', ...' if len(values) > limit else ''}]"

def describe_pdu(pdu, is_request):
    """Czytelny opis PDU do okna logów."""
    if not pdu:
        return "Empty PDU"
    fc = pdu[0]
    if fc & 0x80:
        code = pdu[1] if len(pdu) > 1 else 0
        return (f"EXCEPTION FC{fc & 0x7F:02X} ({FUNCTION_NAMES.get(fc & 0x7F, 'Unknown')}) -> "
                f"code {code:02X} {EXCEPTION_NAMES.get(code, 'Unknown')}")
    text = f"FC{fc:02X} {FUNCTION_NAMES.get(fc, 'Unknown Function')}"
    try:
        if is_request:
            if fc in (0x01, 0x02, 0x03, 0x04):
                address, count = struct.unpack(">HH", pdu[1:5])
                text += f" | addr={address} (0x{address:04X}) qty={count}"
            elif fc == 0x05:
                address, value = struct.unpack(">HH", pdu[1:5])
                state = {0xFF00: "ON", 0x0000: "OFF"}.get(value, f"INVALID 0x{value:04X}")
                text += f" | addr={address} (0x{address:04X}) value={state}"
            elif fc == 0x06:
                address, value = struct.unpack(">HH", pdu[1:5])
                text += f" | addr={address} (0x{address:04X}) value={value} (0x{value:04X})"
            elif fc == 0x0F:
                address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
                bits = [int(b) for b in unpack_bits(pdu[6:6 + byte_count], count)]
                text += f" | addr={address} (0x{address:04X}) qty={count} values={_short_list(bits, 32)}"
            elif fc == 0x10:
                address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
                values = list(struct.unpack(f">{byte_count // 2}H", pdu[6:6 + byte_count]))
                text += f" | addr={address} (0x{address:04X}) qty={count} values={_short_list(values)}"
            elif fc == 0x16:
                address, and_mask, or_mask = struct.unpack(">HHH", pdu[1:7])
                text += f" | addr={address} (0x{address:04X}) AND=0x{and_mask:04X} OR=0x{or_mask:04X}"
            elif fc == 0x17:
                r_addr, r_count, w_addr, w_count, byte_count = struct.unpack(">HHHHB", pdu[1:10])
                values = list(struct.unpack(f">{byte_count // 2}H", pdu[10:10 + byte_count]))
                text += (f" | read addr={r_addr} qty={r_count}, write addr={w_addr} qty={w_count} "
                         f"values={_short_list(values)}")
        else:
            if fc in (0x01, 0x02):
                byte_count = pdu[1]
                bits = [int(b) for b in unpack_bits(pdu[2:2 + byte_count], byte_count * 8)]
                text += f" | {byte_count} bytes, bits={_short_list(bits, 32)}"
            elif fc in (0x03, 0x04, 0x17):
                byte_count = pdu[1]
                values = list(struct.unpack(f">{byte_count // 2}H", pdu[2:2 + byte_count]))
                text += f" | {byte_count} bytes, values={_short_list(values)}"
            elif fc in (0x05, 0x06, 0x0F, 0x10):
                address, value = struct.unpack(">HH", pdu[1:5])
                label = "qty" if fc in (0x0F, 0x10) else "value"
                text += f" | addr={address} (0x{address:04X}) {label}={value} (echo)"
            elif fc == 0x16:
                text += " | (echo)"
    except (struct.error, IndexError):
        text += " | (malformed)"
    return text


# --- Serwery (TCP / RTU) ---

class SlaveServerBase:
    def __init__(self, store, log, slave_id=1, respond_all=False, delay_ms=0):
        self.store = store
        self.log = log # log(kind, channel, data, text) - wywoływane z wątków roboczych
        self.slave_id = slave_id
        self.respond_all = respond_all
        self.delay_ms = delay_ms
        self.running = False
        self.thread = None
        self.stats_lock = threading.Lock()
        self.stats = {"requests": 0, "responses": 0, "exceptions": 0, "errors": 0, "ignored": 0, "clients": 0}

    def count(self, key, delta=1):
        with self.stats_lock:
            self.stats[key] += delta

    def get_stats(self):
        with self.stats_lock:
            return dict(self.stats)

    def is_alive(self):
        return self.thread is not None and self.thread.is_alive()

    def handle_request(self, unit, pdu, channel, broadcast_allowed):
        """Zwraca PDU odpowiedzi lub None, jeśli odpowiedź nie powinna zostać wysłana."""
        self.count("requests")
        if not pdu:
            self.count("errors")
            self.log("ERROR", channel, b"", "Empty PDU - ignored")
            return None
        if broadcast_allowed and unit == 0:
            response = process_pdu(self.store, pdu)
            self.log("INFO", channel, b"", "Broadcast (Unit ID 0) processed - no response sent"
                     + (f" ({describe_pdu(response, False)})" if response and response[0] & 0x80 else ""))
            return None
        if unit != self.slave_id and not self.respond_all:
            self.count("ignored")
            self.log("INFO", channel, b"", f"Unit ID {unit} does not match slave ID {self.slave_id} - ignored")
            return None
        response = process_pdu(self.store, pdu)
        if response is None:
            self.count("ignored")
            self.log("INFO", channel, b"", "Request refers to undefined registers - ignored (no response)")
            return None
        if response[0] & 0x80:
            self.count("exceptions")
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)
        self.count("responses")
        return response


class TcpSlaveServer(SlaveServerBase):
    def __init__(self, store, log, host, port, **kwargs):
        super().__init__(store, log, **kwargs)
        self.host = host
        self.port = port
        self.sock = None
        self.clients = set()
        self.clients_lock = threading.Lock()

    def describe(self):
        return f"TCP {self.host}:{self.port}"

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind((self.host, self.port))
            self.sock.listen(16)
        except OSError:
            self.sock.close()
            raise
        self.sock.settimeout(0.3)
        self.running = True
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()
        self.log("INFO", "TCP", b"", f"Listening on {self.host}:{self.port}")

    def stop(self):
        self.running = False
        with self.clients_lock:
            for conn in list(self.clients):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
        if self.thread:
            self.thread.join(2.0)
        if self.sock:
            self.sock.close()
        self.log("INFO", "TCP", b"", "Server stopped")

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError as e:
                if self.running:
                    self.log("ERROR", "TCP", b"", f"Accept error: {e}")
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.clients_lock:
                self.clients.add(conn)
            threading.Thread(target=self._client_loop, args=(conn, addr), daemon=True).start()

    def _client_loop(self, conn, addr):
        channel = f"{addr[0]}:{addr[1]}"
        self.count("clients")
        self.log("INFO", channel, b"", "Client connected")
        conn.settimeout(0.3)
        buf = b""
        try:
            while self.running:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 7:
                    tid, pid, length, uid = struct.unpack(">HHHB", buf[:7])
                    if not 2 <= length <= 254:
                        self.count("errors")
                        self.log("ERROR", channel, buf, f"Invalid MBAP length field ({length}) - buffer discarded")
                        buf = b""
                        break
                    total = 6 + length
                    if len(buf) < total:
                        break
                    adu, buf = buf[:total], buf[total:]
                    pdu = adu[7:]
                    mbap = f"TID={tid} PID={pid} LEN={length} UID={uid}"
                    self.log("RX", channel, adu, f"{mbap} | {describe_pdu(pdu, True)}")
                    if pid != 0:
                        self.count("errors")
                        self.log("ERROR", channel, b"", f"Protocol ID {pid} is not Modbus (0) - ignored")
                        continue
                    response = self.handle_request(uid, pdu, channel, broadcast_allowed=False)
                    if response is None:
                        continue
                    out = struct.pack(">HHHB", tid, 0, len(response) + 1, uid) + response
                    conn.sendall(out)
                    self.log("TX", channel, out, f"TID={tid} PID=0 LEN={len(response) + 1} UID={uid} | {describe_pdu(response, False)}")
        except OSError as e:
            if self.running:
                self.log("ERROR", channel, b"", f"Connection error: {e}")
        finally:
            with self.clients_lock:
                self.clients.discard(conn)
            conn.close()
            self.count("clients", -1)
            self.log("INFO", channel, b"", "Client disconnected")


class RtuSlaveServer(SlaveServerBase):
    def __init__(self, store, log, port, baudrate, parity, bytesize, stopbits, **kwargs):
        super().__init__(store, log, **kwargs)
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.bytesize = bytesize
        self.stopbits = stopbits
        self.ser = None
        # Czas 3.5 znaku (dla > 19200 bps stała 1.75 ms wg specyfikacji).
        bits_per_char = 1 + bytesize + (0 if parity == "N" else 1) + stopbits
        self.t35 = 3.5 * bits_per_char / baudrate if baudrate <= 19200 else 0.00175
        # Cisza kończąca niepełną/nieznaną ramkę - z zapasem na opóźnienia konwerterów USB-RS485.
        self.silence = max(self.t35, 0.03)

    def describe(self):
        return f"RTU {self.port} {self.baudrate} {self.bytesize}{self.parity}{self.stopbits}"

    def start(self):
        self.ser = serial.Serial(
            port=self.port, baudrate=self.baudrate, parity=self.parity,
            bytesize=self.bytesize, stopbits=self.stopbits, timeout=0.005
        )
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.log("INFO", self.port, b"", f"Port opened: {self.describe()} (t3.5 = {self.t35 * 1000:.2f} ms)")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(2.0)
        if self.ser:
            self.ser.close()
        self.log("INFO", self.port, b"", "Port closed")

    def _loop(self):
        buf = bytearray()
        last_rx = 0.0
        while self.running:
            try:
                waiting = self.ser.in_waiting
                chunk = self.ser.read(waiting if waiting else 1)
            except (serial.SerialException, OSError) as e:
                self.count("errors")
                self.log("ERROR", self.port, b"", f"Serial port error: {e}")
                break
            now = time.perf_counter()
            if chunk:
                buf += chunk
                last_rx = now
                self._parse(buf, final=False)
            elif buf and now - last_rx >= self.silence:
                self._parse(buf, final=True)

    def _find_frame(self, buf):
        """Szuka poprawnej ramki od przesunięcia > 0 (resynchronizacja po śmieciach na linii)."""
        for i in range(1, len(buf) - 3):
            n = rtu_request_length(buf[i:])
            if n is not None and len(buf) - i >= n and crc_ok(buf[i:i + n]):
                return i
        return None

    def _parse(self, buf, final):
        while buf:
            n = rtu_request_length(buf)
            if n is not None and len(buf) >= n and crc_ok(buf[:n]):
                frame = bytes(buf[:n])
                del buf[:n]
                self._handle_frame(frame)
                continue
            if n is None or len(buf) >= n: # na początku bufora nie ma poprawnej ramki - resynchronizacja
                idx = self._find_frame(buf)
                if idx is not None:
                    self.count("errors")
                    self.log("ERROR", self.port, bytes(buf[:idx]), "Discarded bytes before valid frame")
                    del buf[:idx]
                    continue
            if final:
                if crc_ok(buf): # np. nieznany kod funkcji - ramka zakończona ciszą na linii
                    self._handle_frame(bytes(buf))
                else:
                    self.count("errors")
                    self.log("ERROR", self.port, bytes(buf), "Invalid frame (CRC error or incomplete frame)")
                buf.clear()
            break
        if len(buf) > 512:
            self.count("errors")
            self.log("ERROR", self.port, bytes(buf), "Receive buffer overflow - discarded")
            buf.clear()

    def _handle_frame(self, frame):
        uid = frame[0]
        pdu = frame[1:-2]
        self.log("RX", self.port, frame, f"UID={uid} | {describe_pdu(pdu, True)}")
        response = self.handle_request(uid, pdu, self.port, broadcast_allowed=True)
        if response is None:
            return
        out = bytes([uid]) + response
        out += crc16(out).to_bytes(2, "little")
        try:
            self.ser.write(out)
        except (serial.SerialException, OSError) as e:
            self.count("errors")
            self.log("ERROR", self.port, b"", f"Write error: {e}")
            return
        self.log("TX", self.port, out, f"UID={uid} | {describe_pdu(response, False)}")


# --- GUI ---

class LogBus(QObject):
    """Przekazuje wpisy logu z wątków serwera do wątku GUI (połączenie kolejkowane)."""
    entry = pyqtSignal(object)

    def log(self, kind, channel, data=b"", text=""):
        self.entry.emit((datetime.now(), kind, channel, bytes(data), text))


class LogWindow(QWidget):
    """Okno pokazujące dokładną transmisję (surowe ramki HEX + dekodowanie)."""
    COLORS = {"RX": "#0050B4", "TX": "#007A00", "INFO": "#606060", "ERROR": "#C00000"}

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("pymss - Transmission Log")
        self.resize(1100, 500)
        self.entry_count = 0
        self.dropped_count = 0

        layout = QVBoxLayout(self)

        options_layout = QHBoxLayout()
        self.show_rx_cb = QCheckBox("RX")
        self.show_tx_cb = QCheckBox("TX")
        self.show_info_cb = QCheckBox("Info")
        self.show_errors_cb = QCheckBox("Errors")
        self.decode_cb = QCheckBox("Decode frames")
        self.autoscroll_cb = QCheckBox("Auto-scroll")
        self.pause_cb = QCheckBox("Pause")
        for cb in (self.show_rx_cb, self.show_tx_cb, self.show_info_cb, self.show_errors_cb,
                   self.decode_cb, self.autoscroll_cb):
            cb.setChecked(True)
        for cb in (self.show_rx_cb, self.show_tx_cb, self.show_info_cb, self.show_errors_cb):
            options_layout.addWidget(cb)
        options_layout.addSpacing(20)
        options_layout.addWidget(self.decode_cb)
        options_layout.addWidget(self.autoscroll_cb)
        options_layout.addWidget(self.pause_cb)
        options_layout.addSpacing(20)
        options_layout.addWidget(QLabel("Max lines:"))
        self.max_lines_sb = QSpinBox()
        self.max_lines_sb.setRange(100, 1000000)
        self.max_lines_sb.setSingleStep(1000)
        self.max_lines_sb.setValue(20000)
        self.max_lines_sb.valueChanged.connect(lambda v: self.text.setMaximumBlockCount(v))
        options_layout.addWidget(self.max_lines_sb)
        options_layout.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self.clear)
        options_layout.addWidget(self.clear_btn)
        self.save_btn = QPushButton("Save...")
        self.save_btn.clicked.connect(self.save)
        options_layout.addWidget(self.save_btn)
        layout.addLayout(options_layout)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.text.setMaximumBlockCount(self.max_lines_sb.value())
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.text.setFont(font)
        layout.addWidget(self.text)

        self.info_label = QLabel()
        layout.addWidget(self.info_label)
        self.update_info()

        self.formats = {}
        for kind, color in self.COLORS.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.formats[kind] = fmt

    def update_info(self):
        text = f"Entries: {self.entry_count}"
        if self.dropped_count:
            text += f" | Skipped while paused: {self.dropped_count}"
        self.info_label.setText(text)

    def add_entry(self, entry):
        timestamp, kind, channel, data, text = entry
        if self.pause_cb.isChecked():
            self.dropped_count += 1
            self.update_info()
            return
        visible = {"RX": self.show_rx_cb, "TX": self.show_tx_cb,
                   "INFO": self.show_info_cb, "ERROR": self.show_errors_cb}[kind].isChecked()
        if not visible:
            return

        line = f"{timestamp:%H:%M:%S}.{timestamp.microsecond // 1000:03d}  {kind:<5} [{channel}]"
        if data:
            line += f"  ({len(data):>3} B)  {to_hex(data)}"
            if self.decode_cb.isChecked() and text:
                line += f"\n{'':<20}-> {text}" # dekodowanie w osobnej, wciętej linii
        else:
            line += f"  {text}"

        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.text.document().isEmpty():
            cursor.insertBlock()
        cursor.insertText(line, self.formats[kind])
        if self.autoscroll_cb.isChecked():
            bar = self.text.verticalScrollBar()
            bar.setValue(bar.maximum())
        self.entry_count += 1
        self.update_info()

    def clear(self):
        self.text.clear()
        self.entry_count = 0
        self.dropped_count = 0
        self.update_info()

    def save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Transmission Log", f"pymss_log_{datetime.now():%Y%m%d_%H%M%S}.txt",
            "Text files (*.txt *.log);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text.toPlainText())
        except OSError as e:
            QMessageBox.critical(self, "Save Error", f"Cannot save log file: {e}")


class ModbusValueEditor(QDialog):
    """Dialog to edit register value."""
    def __init__(self, definition, current_value, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Edit {TABLES[definition.table]} {definition.address}")
        self.setModal(True)
        self.definition = definition
        self.new_value = None

        layout = QVBoxLayout()
        layout.addWidget(QLabel(f"Address: {definition.address} (0x{definition.address:04X})"))
        dtype_label = "BOOL" if definition.dtype == "BOOL" else DATA_TYPES[definition.dtype][0]
        layout.addWidget(QLabel(f"Data Type: {dtype_label}"))
        if definition.count > 1:
            layout.addWidget(QLabel(f"Byte Order: {BYTE_ORDERS[definition.order]}"))

        if definition.dtype == "BOOL":
            self.input_value = QComboBox()
            self.input_value.addItems(["True", "False"])
            self.input_value.setCurrentIndex(0 if current_value else 1)
        else:
            self.input_value = QLineEdit(format_value(definition.dtype, current_value))
            self.input_value.setPlaceholderText("Enter new value")
            self.input_value.selectAll()
        layout.addWidget(self.input_value)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal, self
        )
        buttons.accepted.connect(self.accept_value)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def accept_value(self):
        try:
            d = self.definition
            if d.dtype == "BOOL":
                self.new_value = self.input_value.currentText() == "True"
            else:
                self.new_value = parse_value(d.dtype, self.input_value.text())
                value_to_registers(d.dtype, d.order, self.new_value) # sprawdzenie zakresu
            self.accept()
        except ValueError as e:
            QMessageBox.warning(self, "Error", f"Enter a valid value for the selected data type. {e}")


class RegisterDefDialog(QDialog):
    """Dialog dodawania (wielu) lub edycji definicji rejestru."""
    def __init__(self, parent=None, definition=None, value=None, default_table="HR", default_address=0):
        super().__init__(parent)
        self.edit_mode = definition is not None
        self.setWindowTitle("Edit Register Definition" if self.edit_mode else "Add Registers")
        self.setModal(True)
        self.result_data = None

        form = QFormLayout()
        self.table_cb = QComboBox()
        for code, label in TABLES.items():
            self.table_cb.addItem(label, code)
        form.addRow("Table:", self.table_cb)

        self.address_sb = QSpinBox()
        self.address_sb.setRange(0, 65535)
        form.addRow("Start Address:" if not self.edit_mode else "Address:", self.address_sb)

        self.count_sb = QSpinBox()
        self.count_sb.setRange(1, 10000)
        self.count_sb.setValue(1)
        if not self.edit_mode:
            form.addRow("Quantity (items):", self.count_sb)

        self.dtype_cb = QComboBox()
        form.addRow("Data Type:", self.dtype_cb)

        self.order_cb = QComboBox()
        for code, label in BYTE_ORDERS.items():
            self.order_cb.addItem(label, code)
        form.addRow("Byte Order:", self.order_cb)

        self.value_le = QLineEdit("0")
        self.value_le.setPlaceholderText("Number, 0x hex, or True/False for bits")
        form.addRow("Value:", self.value_le)

        self.desc_le = QLineEdit()
        form.addRow("Description:", self.desc_le)

        self.table_cb.currentIndexChanged.connect(self.on_table_changed)
        self.dtype_cb.currentIndexChanged.connect(self.on_dtype_changed)

        table = definition.table if self.edit_mode else default_table
        self.table_cb.setCurrentIndex(TABLE_ORDER.index(table))
        self.on_table_changed()
        if self.edit_mode:
            self.address_sb.setValue(definition.address)
            self.dtype_cb.setCurrentIndex(max(0, self.dtype_cb.findData(definition.dtype)))
            self.order_cb.setCurrentIndex(max(0, self.order_cb.findData(definition.order)))
            self.value_le.setText(format_value(definition.dtype, value))
            self.desc_le.setText(definition.description)
        else:
            self.address_sb.setValue(default_address)
        self.on_dtype_changed()

        layout = QVBoxLayout()
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal, self
        )
        buttons.accepted.connect(self.accept_values)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)
        self.setMinimumWidth(380)

    def on_table_changed(self):
        previous = self.dtype_cb.currentData()
        self.dtype_cb.blockSignals(True)
        self.dtype_cb.clear()
        if self.table_cb.currentData() in BIT_TABLES:
            self.dtype_cb.addItem("BOOL (Boolean)", "BOOL")
            if self.value_le.text().strip() not in ("0", "1", "True", "False"):
                self.value_le.setText("False")
        else:
            for code, (label, _, _) in DATA_TYPES.items():
                self.dtype_cb.addItem(label, code)
            index = self.dtype_cb.findData(previous)
            self.dtype_cb.setCurrentIndex(index if index >= 0 else 0)
        self.dtype_cb.blockSignals(False)
        self.on_dtype_changed()

    def on_dtype_changed(self):
        dtype = self.dtype_cb.currentData()
        self.order_cb.setEnabled(dtype is not None and register_count(dtype) > 1)

    def accept_values(self):
        table = self.table_cb.currentData()
        dtype = self.dtype_cb.currentData()
        order = self.order_cb.currentData()
        try:
            value = parse_value(dtype, self.value_le.text())
            if dtype != "BOOL":
                value_to_registers(dtype, order, value) # sprawdzenie zakresu
        except ValueError as e:
            QMessageBox.warning(self, "Error", f"Invalid value: {e}")
            return
        self.result_data = {
            "table": table, "address": self.address_sb.value(), "count": self.count_sb.value(),
            "dtype": dtype, "order": order, "value": value, "description": self.desc_le.text().strip(),
        }
        self.accept()


class ModbusSlaveApp(QMainWindow):
    # Wersja aplikacji, która będzie inkrementowana
    __version__ = "1.01"

    # Kolumny tabeli rejestrów
    COL_TABLE, COL_ADDR, COL_ADDR_HEX, COL_TYPE, COL_ORDER, COL_RAW, COL_VALUE, COL_DESC = range(8)
    HIGHLIGHT_SECONDS = 1.5 # Jak długo podświetlać rejestry zapisane przez mastera

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"pymss (Python Modbus Slave Simulator) v{self.__version__}")
        self.setGeometry(100, 100, 1050, 700) # x, y, width, height

        icon_path = resource_path("icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        self.store = DataStore()
        self.server = None
        self.connection_type = "RTU" # Default connection type
        self.visible_defs = [] # Definicje widoczne w tabeli (indeks = wiersz)
        self.highlighted_rows = set()

        self.log_bus = LogBus()
        self.log_window = LogWindow(self)
        self.log_bus.entry.connect(self.log_window.add_entry)

        self.init_ui()

        # Odświeżanie wartości zmienionych przez mastera i statystyk
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.on_refresh_timer)
        self.refresh_timer.start(250)

    def init_ui(self):
        """Initializes the user interface."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout()
        central_widget.setLayout(main_layout)

        # --- Connection Configuration Section ---
        connection_group = QGroupBox("Connection Configuration")
        connection_layout = QHBoxLayout()
        connection_group.setLayout(connection_layout)

        connection_layout.addWidget(QLabel("Type:"))
        self.connection_type_cb = QComboBox()
        self.connection_type_cb.addItems(["RTU (Serial)", "TCP/IP"])
        self.connection_type_cb.currentIndexChanged.connect(self.on_connection_type_changed)
        connection_layout.addWidget(self.connection_type_cb)

        # RTU Parameters
        self.rtu_params_widget = QWidget()
        self.rtu_params_layout = QHBoxLayout(self.rtu_params_widget)
        self.rtu_params_layout.setContentsMargins(0, 0, 0, 0)
        self.rtu_params_layout.addWidget(QLabel("COM Port:"))
        self.com_port_cb = QComboBox()
        self.com_port_cb.setMinimumWidth(80)
        self.rtu_params_layout.addWidget(self.com_port_cb)
        self.refresh_ports_btn = QPushButton("↻")
        self.refresh_ports_btn.setToolTip("Refresh COM port list")
        self.refresh_ports_btn.setFixedWidth(28)
        self.refresh_ports_btn.clicked.connect(self.populate_com_ports)
        self.rtu_params_layout.addWidget(self.refresh_ports_btn)
        self.populate_com_ports()
        self.rtu_params_layout.addWidget(QLabel("Baud Rate:"))
        self.baud_rate_cb = QComboBox()
        self.baud_rate_cb.addItems(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"])
        self.baud_rate_cb.setCurrentText("9600")
        self.rtu_params_layout.addWidget(self.baud_rate_cb)
        self.rtu_params_layout.addWidget(QLabel("Parity:"))
        self.parity_cb = QComboBox()
        self.parity_cb.addItems(["N", "E", "O"]) # None, Even, Odd
        self.rtu_params_layout.addWidget(self.parity_cb)
        self.rtu_params_layout.addWidget(QLabel("Data Bits:"))
        self.data_bits_cb = QComboBox()
        self.data_bits_cb.addItems(["7", "8"])
        self.data_bits_cb.setCurrentText("8")
        self.rtu_params_layout.addWidget(self.data_bits_cb)
        self.rtu_params_layout.addWidget(QLabel("Stop Bits:"))
        self.stop_bits_cb = QComboBox()
        self.stop_bits_cb.addItems(["1", "2"])
        self.rtu_params_layout.addWidget(self.stop_bits_cb)

        # TCP Parameters
        self.tcp_params_widget = QWidget()
        self.tcp_params_layout = QHBoxLayout(self.tcp_params_widget)
        self.tcp_params_layout.setContentsMargins(0, 0, 0, 0)
        self.tcp_params_layout.addWidget(QLabel("Listen IP:"))
        self.ip_address_le = QLineEdit("0.0.0.0")
        self.ip_address_le.setToolTip("0.0.0.0 - all network interfaces, 127.0.0.1 - local only")
        self.tcp_params_layout.addWidget(self.ip_address_le)
        self.tcp_params_layout.addWidget(QLabel("Port:"))
        self.port_le = QLineEdit("502")
        self.port_le.setMaximumWidth(70)
        self.tcp_params_layout.addWidget(self.port_le)

        connection_layout.addWidget(self.rtu_params_widget)
        connection_layout.addWidget(self.tcp_params_widget)
        self.tcp_params_widget.hide() # Hide TCP at start
        connection_layout.addStretch()

        self.start_btn = QPushButton("Start Server")
        self.start_btn.clicked.connect(self.start_server)
        connection_layout.addWidget(self.start_btn)
        self.stop_btn = QPushButton("Stop Server")
        self.stop_btn.clicked.connect(self.stop_server)
        self.stop_btn.setEnabled(False)
        connection_layout.addWidget(self.stop_btn)

        main_layout.addWidget(connection_group)

        # --- Slave Settings Section ---
        slave_group = QGroupBox("Slave Settings")
        slave_layout = QHBoxLayout()
        slave_group.setLayout(slave_layout)

        slave_layout.addWidget(QLabel("Slave ID:"))
        self.slave_id_sb = QSpinBox()
        self.slave_id_sb.setRange(1, 247) # Modbus slave ID range
        self.slave_id_sb.setValue(1)
        self.slave_id_sb.valueChanged.connect(self.apply_slave_settings)
        slave_layout.addWidget(self.slave_id_sb)

        self.respond_all_cb = QCheckBox("Respond to any Unit ID")
        self.respond_all_cb.toggled.connect(self.apply_slave_settings)
        slave_layout.addWidget(self.respond_all_cb)

        slave_layout.addWidget(QLabel("Response Delay (ms):"))
        self.delay_sb = QSpinBox()
        self.delay_sb.setRange(0, 30000)
        self.delay_sb.setValue(0)
        self.delay_sb.valueChanged.connect(self.apply_slave_settings)
        slave_layout.addWidget(self.delay_sb)

        slave_layout.addWidget(QLabel("Undefined Registers:"))
        self.undefined_mode_cb = QComboBox()
        for code, label in UNDEFINED_MODES.items():
            self.undefined_mode_cb.addItem(label, code)
        self.undefined_mode_cb.setToolTip("Response to requests that refer to registers not defined in the Register Map")
        self.undefined_mode_cb.currentIndexChanged.connect(self.apply_slave_settings)
        slave_layout.addWidget(self.undefined_mode_cb)
        slave_layout.addStretch()

        self.show_log_btn = QPushButton("Transmission Log")
        self.show_log_btn.clicked.connect(self.show_log_window)
        slave_layout.addWidget(self.show_log_btn)

        main_layout.addWidget(slave_group)

        # --- Register Map Section ---
        registers_group = QGroupBox("Register Map")
        registers_layout = QVBoxLayout()
        registers_group.setLayout(registers_layout)

        buttons_layout = QHBoxLayout()
        self.add_btn = QPushButton("Add...")
        self.add_btn.clicked.connect(self.add_registers)
        buttons_layout.addWidget(self.add_btn)
        self.edit_btn = QPushButton("Edit...")
        self.edit_btn.clicked.connect(self.edit_selected)
        buttons_layout.addWidget(self.edit_btn)
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.clicked.connect(self.remove_selected)
        buttons_layout.addWidget(self.remove_btn)
        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.clicked.connect(self.clear_all)
        buttons_layout.addWidget(self.clear_btn)
        buttons_layout.addSpacing(20)
        self.import_btn = QPushButton("Import CSV...")
        self.import_btn.clicked.connect(self.import_csv)
        buttons_layout.addWidget(self.import_btn)
        self.export_btn = QPushButton("Export CSV...")
        self.export_btn.clicked.connect(self.export_csv)
        buttons_layout.addWidget(self.export_btn)
        buttons_layout.addStretch()
        buttons_layout.addWidget(QLabel("Show:"))
        self.filter_cb = QComboBox()
        self.filter_cb.addItem("All", None)
        for code, label in TABLES.items():
            self.filter_cb.addItem(label, code)
        self.filter_cb.currentIndexChanged.connect(self.rebuild_table)
        buttons_layout.addWidget(self.filter_cb)
        registers_layout.addLayout(buttons_layout)

        self.registers_table = QTableWidget()
        self.registers_table.setColumnCount(8)
        self.registers_table.setHorizontalHeaderLabels([
            "Table", "Address", "Address (HEX)", "Data Type", "Byte Order", "Raw Value (HEX/Bool)", "Value", "Description"
        ])
        self.registers_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.registers_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.registers_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.registers_table.verticalHeader().setVisible(False)
        self.registers_table.cellDoubleClicked.connect(self.on_table_double_click)
        for col, width in enumerate([150, 70, 100, 80, 85, 190, 150]):
            self.registers_table.setColumnWidth(col, width)
        self.registers_table.horizontalHeader().setStretchLastSection(True) # Last column stretches
        registers_layout.addWidget(self.registers_table)

        delete_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Delete), self.registers_table)
        delete_shortcut.activated.connect(self.remove_selected)

        main_layout.addWidget(registers_group)

        # Status Bar
        self.status_message_label = QLabel("Ready")
        self.statusBar().addWidget(self.status_message_label, 1)
        self.stats_label = QLabel("")
        self.statusBar().addPermanentWidget(self.stats_label)
        self.update_status_bar_stats()

    # --- Konfiguracja połączenia ---

    def populate_com_ports(self):
        """Populates the QComboBox with available COM ports."""
        current = self.com_port_cb.currentText()
        self.com_port_cb.clear()
        ports = serial.tools.list_ports.comports()
        if not ports:
            self.com_port_cb.addItem("No COM Ports")
            self.com_port_cb.setEnabled(False)
            return
        for port in sorted(ports, key=lambda p: p.device):
            self.com_port_cb.addItem(port.device)
            self.com_port_cb.setItemData(self.com_port_cb.count() - 1, port.description, Qt.ItemDataRole.ToolTipRole)
        self.com_port_cb.setEnabled(True)
        index = self.com_port_cb.findText(current)
        if index >= 0:
            self.com_port_cb.setCurrentIndex(index)

    def on_connection_type_changed(self, index):
        """Toggles visibility of RTU/TCP parameters based on selected connection type."""
        if index == 0: # RTU (Serial)
            self.connection_type = "RTU"
            self.rtu_params_widget.show()
            self.tcp_params_widget.hide()
        else: # TCP/IP
            self.connection_type = "TCP"
            self.rtu_params_widget.hide()
            self.tcp_params_widget.show()

    def set_status(self, text, color=""):
        self.status_message_label.setText(text)
        self.status_message_label.setStyleSheet(f"color: {color};" if color else "")

    def update_server_buttons(self):
        running = self.server is not None
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.connection_type_cb.setEnabled(not running)
        self.rtu_params_widget.setEnabled(not running)
        self.tcp_params_widget.setEnabled(not running)

    def apply_slave_settings(self):
        """Ustawienia slave'a można zmieniać w trakcie pracy serwera."""
        self.store.undefined_mode = self.undefined_mode_cb.currentData()
        if self.server:
            self.server.slave_id = self.slave_id_sb.value()
            self.server.respond_all = self.respond_all_cb.isChecked()
            self.server.delay_ms = self.delay_sb.value()

    def start_server(self):
        if self.server:
            return
        common = dict(
            slave_id=self.slave_id_sb.value(),
            respond_all=self.respond_all_cb.isChecked(),
            delay_ms=self.delay_sb.value(),
        )
        try:
            if self.connection_type == "RTU":
                port = self.com_port_cb.currentText()
                if not port or port == "No COM Ports":
                    QMessageBox.warning(self, "Port Error", "No COM port selected.")
                    return
                server = RtuSlaveServer(
                    self.store, self.log_bus.log, port,
                    baudrate=int(self.baud_rate_cb.currentText()),
                    parity=self.parity_cb.currentText(),
                    bytesize=int(self.data_bits_cb.currentText()),
                    stopbits=int(self.stop_bits_cb.currentText()),
                    **common)
            else:
                host = self.ip_address_le.text().strip() or "0.0.0.0"
                port = int(self.port_le.text())
                if not 1 <= port <= 65535:
                    raise ValueError("TCP port must be in range 1-65535")
                server = TcpSlaveServer(self.store, self.log_bus.log, host, port, **common)
            self.store.undefined_mode = self.undefined_mode_cb.currentData()
            server.start()
        except (OSError, ValueError, serial.SerialException) as e:
            self.set_status(f"Cannot start server: {e}", "red")
            self.log_bus.log("ERROR", self.connection_type, b"", f"Cannot start server: {e}")
            return
        self.server = server
        self.set_status(f"Server running: {server.describe()}, slave ID {self.slave_id_sb.value()}.", "green")
        self.update_server_buttons()
        self.update_status_bar_stats()

    def stop_server(self, failed=False):
        if self.server:
            self.server.stop()
            self.server = None
        if failed:
            self.set_status("Server stopped because of a communication error (see Transmission Log).", "red")
        else:
            self.set_status("Server stopped.")
        self.update_server_buttons()

    def show_log_window(self):
        self.log_window.show()
        self.log_window.raise_()
        self.log_window.activateWindow()

    # --- Tabela rejestrów ---

    def rebuild_table(self):
        """Pełna przebudowa tabeli po zmianie definicji lub filtra."""
        table_filter = self.filter_cb.currentData()
        with self.store.lock:
            self.visible_defs = [d for d in self.store.defs if table_filter is None or d.table == table_filter]
        self.highlighted_rows = set()
        self.registers_table.setRowCount(len(self.visible_defs))
        for row, d in enumerate(self.visible_defs):
            items = [
                TABLES[d.table],
                str(d.address),
                f"0x{d.address:04X}",
                d.dtype,
                d.order if d.count > 1 else "-",
                "",
                "",
                d.description,
            ]
            for col, text in enumerate(items):
                self.registers_table.setItem(row, col, QTableWidgetItem(text))
        self.refresh_values()
        self.update_status_bar_stats()

    def refresh_values(self):
        """Aktualizuje kolumny wartości i podświetla rejestry zapisane niedawno przez mastera."""
        now = time.monotonic()
        highlighted = set()
        for row, d in enumerate(self.visible_defs):
            raw_item = self.registers_table.item(row, self.COL_RAW)
            value_item = self.registers_table.item(row, self.COL_VALUE)
            if raw_item is None or value_item is None:
                continue
            raw_item.setText(self.store.raw_text(d))
            value_item.setText(format_value(d.dtype, self.store.get_value(d)))
            if now - self.store.last_write_time(d) < self.HIGHLIGHT_SECONDS:
                highlighted.add(row)
        for row in highlighted | self.highlighted_rows:
            color = QColor(255, 236, 150) if row in highlighted else None
            for col in range(self.registers_table.columnCount()):
                item = self.registers_table.item(row, col)
                if item:
                    item.setData(Qt.ItemDataRole.BackgroundRole, color)
        self.highlighted_rows = highlighted

    def on_refresh_timer(self):
        if self.server and not self.server.is_alive():
            self.stop_server(failed=True)
        if self.store.consume_dirty() or self.highlighted_rows:
            self.refresh_values()
        self.update_status_bar_stats()

    def update_status_bar_stats(self):
        text = f"Registers: {len(self.store.defs)}"
        if self.server:
            s = self.server.get_stats()
            text += (f" | Requests: {s['requests']} | Responses: {s['responses']}"
                     f" | Exceptions: {s['exceptions']} | Errors: {s['errors']} | Ignored: {s['ignored']}")
            if isinstance(self.server, TcpSlaveServer):
                text += f" | Clients: {s['clients']}"
        self.stats_label.setText(text)

    def selected_defs(self):
        rows = sorted({index.row() for index in self.registers_table.selectionModel().selectedRows()})
        return [self.visible_defs[r] for r in rows if r < len(self.visible_defs)]

    def next_free_address(self, table):
        with self.store.lock:
            ends = [d.address + d.count for d in self.store.defs if d.table == table]
        return min(max(ends, default=0), 65535)

    def add_registers(self):
        selected = self.selected_defs()
        table = selected[-1].table if selected else (self.filter_cb.currentData() or "HR")
        dialog = RegisterDefDialog(self, default_table=table, default_address=self.next_free_address(table))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        r = dialog.result_data
        width = register_count(r["dtype"])
        added, errors = 0, []
        for i in range(r["count"]):
            d = RegisterDef(r["table"], r["address"] + i * width, r["dtype"], r["order"], r["description"])
            try:
                self.store.add_definition(d, r["value"])
                added += 1
            except ValueError as e:
                errors.append(str(e))
        self.rebuild_table()
        if errors:
            self.set_status(f"Added {added} register(s), {len(errors)} skipped.", "orange")
            QMessageBox.warning(self, "Add Registers", f"Added {added}, skipped {len(errors)}:\n\n" + "\n".join(errors[:15])
                                + ("\n..." if len(errors) > 15 else ""))
        else:
            self.set_status(f"Added {added} register(s).")

    def edit_definition(self, d):
        dialog = RegisterDefDialog(self, definition=d, value=self.store.get_value(d))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        r = dialog.result_data
        new = RegisterDef(r["table"], r["address"], r["dtype"], r["order"], r["description"])
        try:
            self.store.replace_definition(d, new, r["value"])
        except ValueError as e:
            QMessageBox.warning(self, "Edit Register", str(e))
            return
        self.rebuild_table()
        self.set_status(f"Updated {TABLES[new.table]} {new.address}.")

    def edit_selected(self):
        selected = self.selected_defs()
        if len(selected) != 1:
            QMessageBox.information(self, "Edit Register", "Select exactly one register to edit.")
            return
        self.edit_definition(selected[0])

    def edit_value(self, d):
        dialog = ModbusValueEditor(d, self.store.get_value(d), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.new_value is not None:
            try:
                self.store.set_value(d, dialog.new_value)
            except ValueError as e:
                QMessageBox.warning(self, "Error", str(e))
                return
            self.refresh_values()
            self.set_status(f"{TABLES[d.table]} {d.address} = {format_value(d.dtype, dialog.new_value)}")

    def on_table_double_click(self, row, column):
        """Podwójne kliknięcie na wartości - edycja wartości, w pozostałych kolumnach - edycja definicji."""
        if row >= len(self.visible_defs):
            return
        d = self.visible_defs[row]
        if column in (self.COL_VALUE, self.COL_RAW):
            self.edit_value(d)
        else:
            self.edit_definition(d)

    def remove_selected(self):
        selected = self.selected_defs()
        if not selected:
            return
        self.store.remove_definitions(selected)
        self.rebuild_table()
        self.set_status(f"Removed {len(selected)} register(s).")

    def clear_all(self):
        if not self.store.defs:
            return
        if QMessageBox.question(self, "Clear All", "Remove all register definitions?") != QMessageBox.StandardButton.Yes:
            return
        self.store.clear()
        self.rebuild_table()
        self.set_status("Register map cleared.")

    # --- Import / eksport CSV ---
    # Format: table;address;data_type;byte_order;value;description
    # Import automatycznie rozpoznaje separator (',' ';' lub tab) i akceptuje przecinek dziesiętny.

    CSV_COLUMNS = ["table", "address", "data_type", "byte_order", "value", "description"]

    def export_csv(self):
        if not self.store.defs:
            QMessageBox.information(self, "Export CSV", "Register map is empty.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Register Configuration", "registers.csv",
                                              "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            with self.store.lock:
                rows = [[TABLE_CSV_NAMES[d.table], d.address, d.dtype, d.order,
                         format_value(d.dtype, self.store.get_value(d)), d.description] for d in self.store.defs]
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(self.CSV_COLUMNS)
                writer.writerows(rows)
        except OSError as e:
            QMessageBox.critical(self, "Export CSV", f"Cannot write file: {e}")
            return
        self.set_status(f"Exported {len(rows)} register(s) to {path}")

    @staticmethod
    def parse_csv_rows(text):
        """Zwraca (lista (RegisterDef, wartość, nr_linii), lista błędów)."""
        try:
            delimiter = csv.Sniffer().sniff(text.split("\n", 1)[0], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ";"
        reader = csv.reader(text.splitlines(), delimiter=delimiter)
        rows = list(reader)
        if not rows:
            return [], ["File is empty"]
        header = [h.strip().lower() for h in rows[0]]
        if "table" not in header or "address" not in header:
            return [], ["Missing header row (required columns: table, address)"]
        index = {name: header.index(name) for name in ModbusSlaveApp.CSV_COLUMNS if name in header}

        def cell(row, name):
            i = index.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""

        result, errors = [], []
        for line_no, row in enumerate(rows[1:], start=2):
            if not any(c.strip() for c in row) or row[0].strip().startswith("#"):
                continue
            try:
                table = TABLE_ALIASES.get(cell(row, "table").lower())
                if table is None:
                    raise ValueError(f"unknown table '{cell(row, 'table')}'")
                address = parse_value("UINT16", cell(row, "address")) # dziesiętnie lub 0x...
                if not 0 <= address <= 65535:
                    raise ValueError(f"address {address} out of range")
                dtype = cell(row, "data_type").upper() or ("BOOL" if table in BIT_TABLES else "UINT16")
                dtype = dtype.split(" ")[0] # akceptuje również pełne etykiety, np. "FLOAT32 (32-bit Float)"
                if table in BIT_TABLES:
                    if dtype not in ("BOOL", "BOOLEAN", "BIT"):
                        raise ValueError(f"data type for {TABLES[table]} must be BOOL")
                    dtype = "BOOL"
                elif dtype not in DATA_TYPES:
                    raise ValueError(f"unknown data type '{dtype}'")
                order = (cell(row, "byte_order").upper() or "ABCD").split(" ")[0]
                if order in ("-", ""):
                    order = "ABCD"
                if order not in BYTE_ORDERS:
                    raise ValueError(f"unknown byte order '{order}'")
                value = parse_value(dtype, cell(row, "value"))
                if dtype != "BOOL":
                    value_to_registers(dtype, order, value)
                result.append((RegisterDef(table, address, dtype, order, cell(row, "description")), value, line_no))
            except ValueError as e:
                errors.append(f"Line {line_no}: {e}")
        return result, errors

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Register Configuration", "",
                                              "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="cp1250") as f:
                text = f.read()
        except OSError as e:
            QMessageBox.critical(self, "Import CSV", f"Cannot read file: {e}")
            return

        parsed, errors = self.parse_csv_rows(text)
        if not parsed and errors:
            QMessageBox.warning(self, "Import CSV", "Nothing imported:\n\n" + "\n".join(errors[:20]))
            return

        replace = True
        if self.store.defs:
            answer = QMessageBox.question(
                self, "Import CSV",
                "Replace the current register map?\n\nYes - replace, No - merge with existing registers.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
            if answer == QMessageBox.StandardButton.Cancel:
                return
            replace = answer == QMessageBox.StandardButton.Yes

        imported = 0
        with self.store.lock:
            if replace:
                self.store.clear()
            for d, value, line_no in parsed:
                try:
                    self.store.add_definition(d, value)
                    imported += 1
                except ValueError as e:
                    errors.append(f"Line {line_no}: {e}")
        self.rebuild_table()
        if errors:
            self.set_status(f"Imported {imported} register(s) from {os.path.basename(path)}, {len(errors)} row(s) skipped.", "orange")
            QMessageBox.warning(self, "Import CSV", f"Imported {imported} register(s). Skipped {len(errors)} row(s):\n\n"
                                + "\n".join(errors[:20]) + ("\n..." if len(errors) > 20 else ""))
        else:
            self.set_status(f"Imported {imported} register(s) from {os.path.basename(path)}.")

    def closeEvent(self, event):
        if self.server:
            self.server.stop()
            self.server = None
        self.log_window.close()
        super().closeEvent(event)


if __name__ == "__main__":
    try: # Własna ikona na pasku zadań Windows (zamiast ikony pythonw)
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("pymss.modbus.slave.simulator")
    except Exception:
        pass
    app = QApplication(sys.argv)
    window = ModbusSlaveApp()
    window.show()
    sys.exit(app.exec())
