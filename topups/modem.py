import logging
import re
import threading
import time
from dataclasses import dataclass

from django.conf import settings


logger = logging.getLogger("topups.modem")
_modem_lock = threading.Lock()


class ModemError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModemResult:
    successful: bool
    response: str


@dataclass(frozen=True)
class ModemMessage:
    index: int
    status: str
    sender: str
    timestamp: str
    text: str


@dataclass(frozen=True)
class DetectedPort:
    device: str
    description: str
    responsive: bool
    operator: str = ""
    operator_name: str = ""
    manufacturer: str = ""
    model: str = ""
    imei: str = ""
    sim_status: str = ""
    signal: str = ""
    error: str = ""


OPERATOR_ALIASES = {
    "grameenphone": ("grameenphone", "grameenphone", "gp", "47001"),
    "robi": ("robi", "robi axiata", "47002"),
    "airtel": ("airtel", "airtel bd", "47007"),
    "banglalink": ("banglalink", "banglalink digital", "47003"),
        "teletalk": ("teletalk", "47004"),

}

SIM_NUMBER_CODES = {
    "grameenphone": "*2#",
    "banglalink": "*511#",
    "airtel": "*282#",
    "robi": "*140*2*4#",
     "teletalk": "*551#",
}

SIM_NUMBER_FALLBACK_CODES = {
    "robi": ("*140*2*4#", "*2#"),
    "airtel": ("*282#", "*2#"),
}


class MockModem:
    def send_ussd(self, code, dcs=15, port=None):
        logger.info("Mock modem accepted USSD request")
        number_responses = {
            "*2#": '+CUSD: 0,"Your number is 01700000000",15',
            "*511#": '+CUSD: 0,"Your number is 01900000000",15',
            "*282#": '+CUSD: 0,"Your number is 01600000000",15',
            "*140*2*4#": '+CUSD: 0,"Your number is 01800000000",15',
        }
        return ModemResult(
            True,
            number_responses.get(code, "MOCK: request accepted"),
        )

    def send_ussd_session(self, code, replies=None, dcs=15, port=None, reply_strategy=None):
        logger.info("Mock modem accepted interactive USSD request")
        conversation = [
            '+CUSD: 1,"0:Default Recharge (2Tk)",15',
            '+CUSD: 1,"Enter mobile number",15',
            '+CUSD: 1,"Enter amount",15',
            '+CUSD: 1,"Enter PIN",15',
            (
                '+CUSD: 2,"Recharge request accepted, transaction ID '
                'R260611.0000.000001 is successful.",15'
            ),
        ]
        return ModemResult(True, "\n".join(conversation))

    def read_messages(self, port=None):
        return [
            ModemMessage(
                index=1,
                status="REC READ",
                sender="Banglalink",
                timestamp="26/06/13,10:30:00+24",
                text=(
                    "\u0986\u09aa\u09a8\u09be\u09b0 "
                    "\u09b0\u09bf\u099a\u09be\u09b0\u09cd\u099c "
                    "\u0985\u09a8\u09c1\u09b0\u09cb\u09a7 "
                    "\u0997\u09cd\u09b0\u09b9\u09a3 \u0995\u09b0\u09be "
                    "\u09b9\u09af\u09bc\u09c7\u099b\u09c7\u0964"
                ),
            )
        ]

    def clear_messages(self, port=None):
        return None

    def delete_message(self, index, port=None, expected_text=None):
        logger.info("Mock modem accepted delete request for index %s", index)
        return True


class SerialModem:
    def send_ussd(self, code, dcs=15, port=None):
        return self._run_ussd(code, dcs=dcs, port=port, replies=[])

    def send_ussd_session(self, code, replies=None, dcs=15, port=None, reply_strategy=None):
        return self._run_ussd(
            code,
            dcs=dcs,
            port=port,
            replies=list(replies or []),
            reply_strategy=reply_strategy,
        )

    def read_messages(self, port=None):
        serial_port = port or settings.MODEM_PORT
        try:
            import serial
        except ImportError as exc:
            raise ModemError("pyserial is required for the serial modem backend") from exc

        with _modem_lock:
            try:
                with serial.Serial(
                    serial_port,
                    settings.MODEM_BAUDRATE,
                    timeout=1,
                    write_timeout=5,
                ) as modem:
                    if "OK" not in _query(modem, "AT", timeout=2):
                        raise ModemError(f"No AT response from {serial_port}")
                    if "OK" not in _query(modem, "AT+CMGF=1", timeout=2):
                        raise ModemError("The modem does not support SMS text mode.")
                    
                    storage_capabilities = _query(modem, "AT+CPMS=?", timeout=4)
                    storage_status = _query(modem, "AT+CPMS?", timeout=4)
                    collected = []
                    storage_errors = []
                    successful_storages = set()
                    
                    # Try different storage configurations
                    # Some modems need specific storage names in quotes
                    storage_options = [
                        ('SM', 'SM', 'SM'),      # SIM card
                        ('ME', 'ME', 'ME'),      # Modem memory
                        ('MT', 'MT', 'MT'),      # Mobile terminal
                        ('SM', 'ME', 'SM'),      # Mixed
                        ('ME', 'SM', 'ME'),      # Mixed
                        ('"SM"', '"SM"', '"SM"'), # Quoted
                        ('"ME"', '"ME"', '"ME"'), # Quoted
                        ('"MT"', '"MT"', '"MT"'), # Quoted
                    ]
                    
                    for charset in ("GSM", "UCS2"):
                        charset_response = _query(
                            modem,
                            f'AT+CSCS="{charset}"',
                            timeout=3,
                        )
                        if "ERROR" in charset_response or "+CME ERROR" in charset_response:
                            continue

                        for mem1, mem2, mem3 in storage_options:
                            # Try different CPMS command formats
                            cpms_commands = [
                                f'AT+CPMS="{mem1}","{mem2}","{mem3}"',
                                f'AT+CPMS={mem1},{mem2},{mem3}',
                                f'AT+CPMS="{mem1}"',
                            ]
                            
                            for cpms_cmd in cpms_commands:
                                storage_response = _query(modem, cpms_cmd, timeout=4)
                                if (
                                    "ERROR" not in storage_response
                                    and "+CME ERROR" not in storage_response
                                ):
                                    successful_storages.add(f"{mem1}/{mem2}/{mem3}")
                                    
                                    # Set new message indication
                                    _query(modem, "AT+CNMI=2,1,0,0,0", timeout=3)
                                    
                                    # Try to list messages with different formats
                                    list_commands = [
                                        'AT+CMGL="ALL"',
                                        'AT+CMGL=4',
                                        'AT+CMGL=0',
                                        'AT+CMGL="REC READ"',
                                        'AT+CMGL="REC UNREAD"',
                                    ]
                                    
                                    for list_cmd in list_commands:
                                        response = _query(modem, list_cmd, timeout=12)
                                        if (
                                            "ERROR" not in response
                                            and "+CME ERROR" not in response
                                        ):
                                            parsed_messages = parse_sms_messages(response)
                                            if parsed_messages:
                                                collected.extend(parsed_messages)
                                                break
                                            parsed_messages = parse_pdu_messages(response)
                                            if parsed_messages:
                                                collected.extend(parsed_messages)
                                                break
                                    
                                    if collected:
                                        break
                                
                                storage_errors.append(
                                    f"{charset}/{mem1}/{mem2}/{mem3}: {storage_response}"
                                )
                            
                            if collected:
                                break
                        
                        if collected:
                            break

                    # If still no messages, try PDU mode directly
                    if not collected:
                        pdu_mode_response = _query(modem, "AT+CMGF=0", timeout=3)
                        if (
                            "ERROR" not in pdu_mode_response
                            and "+CME ERROR" not in pdu_mode_response
                        ):
                            for mem1, mem2, mem3 in storage_options:
                                for cpms_cmd in [
                                    f'AT+CPMS="{mem1}","{mem2}","{mem3}"',
                                    f'AT+CPMS={mem1},{mem2},{mem3}',
                                ]:
                                    storage_response = _query(modem, cpms_cmd, timeout=4)
                                    if (
                                        "ERROR" in storage_response
                                        or "+CME ERROR" in storage_response
                                    ):
                                        continue
                                    
                                    pdu_response = _query(modem, "AT+CMGL=4", timeout=12)
                                    if (
                                        "ERROR" not in pdu_response
                                        and "+CME ERROR" not in pdu_response
                                    ):
                                        parsed_messages = parse_pdu_messages(pdu_response)
                                        collected.extend(parsed_messages)
                                        if parsed_messages:
                                            break
                                
                                if collected:
                                    break
                        
                        # Switch back to text mode
                        _query(modem, "AT+CMGF=1", timeout=2)

                    # If still no messages, try a simpler approach
                    if not collected:
                        # Try to just list with SM storage
                        try:
                            # Try AT+CPMS="SM" only
                            _query(modem, 'AT+CPMS="SM"', timeout=4)
                            response = _query(modem, 'AT+CMGL="ALL"', timeout=12)
                            if "ERROR" not in response and "+CME ERROR" not in response:
                                parsed = parse_sms_messages(response)
                                if parsed:
                                    collected.extend(parsed)
                                else:
                                    parsed = parse_pdu_messages(response)
                                    if parsed:
                                        collected.extend(parsed)
                        except Exception:
                            pass

                    # Reset to text mode
                    _query(modem, "AT+CMGF=1", timeout=2)
                    
            except (OSError, serial.SerialException) as exc:
                raise ModemError(f"Serial modem error on {serial_port}: {exc}") from exc

        if not collected and not successful_storages:
            # Don't raise error - just return empty list
            logger.info(
                "No SMS found on %s; CPMS supported=%s current=%s",
                serial_port,
                storage_capabilities,
                storage_status,
            )
            return []

        if not collected:
            logger.info(
                "No SMS found on %s; CPMS supported=%s current=%s",
                serial_port,
                storage_capabilities,
                storage_status,
            )

        # Remove duplicates
        unique = {}
        for message in collected:
            key = (message.sender, message.timestamp, message.text)
            unique[key] = message
        return list(unique.values())
    def clear_messages(self, port=None):
        serial_port = port or settings.MODEM_PORT
        try:
            import serial
        except ImportError as exc:
            raise ModemError("pyserial is required for the serial modem backend") from exc

        with _modem_lock:
            try:
                with serial.Serial(
                    serial_port,
                    settings.MODEM_BAUDRATE,
                    timeout=1,
                    write_timeout=5,
                ) as modem:
                    if "OK" not in _query(modem, "AT", timeout=2):
                        raise ModemError(f"No AT response from {serial_port}")
                    response = _query(modem, "AT+CMGD=1,4", timeout=5)
            except (OSError, serial.SerialException) as exc:
                raise ModemError(f"Serial modem error on {serial_port}: {exc}") from exc

        if "OK" not in response:
            raise ModemError(f"Could not clear SMS messages from {serial_port}: {response}")

    def delete_message(self, index, port=None, expected_text=None):
        """Delete a single SMS from the modem by its storage index.

        This is called only AFTER the message has been safely stored in the
        database. When ``expected_text`` is supplied it is used as a safety
        check: the slot is re-read with AT+CMGR and the delete only proceeds
        when the slot still holds that same message. If the slot is empty,
        holds a different message, or cannot be verified, the delete is
        skipped and ``False`` is returned so the SMS is left on the modem for
        the next cycle instead of risking deletion of the wrong message.
        """
        serial_port = port or settings.MODEM_PORT
        try:
            import serial
        except ImportError as exc:
            raise ModemError("pyserial is required for the serial modem backend") from exc

        with _modem_lock:
            try:
                with serial.Serial(
                    serial_port,
                    settings.MODEM_BAUDRATE,
                    timeout=1,
                    write_timeout=5,
                ) as modem:
                    if "OK" not in _query(modem, "AT", timeout=2):
                        raise ModemError(f"No AT response from {serial_port}")
                    _query(modem, "AT+CMGF=1", timeout=2)

                    if expected_text:
                        read_back = _query(modem, f"AT+CMGR={index}", timeout=5)
                        if not _cmgr_matches_expected(read_back, expected_text):
                            logger.warning(
                                "Skipping modem delete on %s index %s: slot content "
                                "did not match the stored message.",
                                serial_port,
                                index,
                            )
                            return False

                    response = _query(modem, f"AT+CMGD={index}", timeout=5)
            except (OSError, serial.SerialException) as exc:
                raise ModemError(f"Serial modem error on {serial_port}: {exc}") from exc

        if "OK" not in response:
            raise ModemError(
                f"Could not delete SMS index {index} from {serial_port}: {response}"
            )
        logger.info("Deleted SMS index %s from %s after storage.", index, serial_port)
        return True

    def _run_ussd(self, code, dcs=15, port=None, replies=None, reply_strategy=None):
        try:
            import serial
        except ImportError as exc:
            raise ModemError("pyserial is required for the serial modem backend") from exc

        replies = list(replies or [])
        serial_port = port or settings.MODEM_PORT
        deadline = time.monotonic() + settings.MODEM_TIMEOUT
        lines = []
        ussd_received_at = None
        next_reply = 0
        replies_sent = 0
        max_replies = 12  # safety cap on interactive menu navigation

        with _modem_lock:
            try:
                with serial.Serial(
                    serial_port,
                    settings.MODEM_BAUDRATE,
                    timeout=1,
                    write_timeout=5,
                ) as modem:
                    modem.reset_input_buffer()
                    self._command(modem, "AT")
                    self._command(modem, "AT+CMEE=2")
                    _query(modem, "AT+CUSD=2", timeout=2)
                    command = f'AT+CUSD=1,"{code}",{dcs}'
                    modem.write((command + "\r").encode("ascii"))
                    modem.flush()

                    while time.monotonic() < deadline:
                        raw = modem.readline()
                        if not raw:
                            if ussd_received_at and time.monotonic() - ussd_received_at >= 2:
                                response = decode_ussd_response("\n".join(lines))
                                return ModemResult(
                                    _ussd_response_is_successful(response),
                                    response,
                                )
                            continue
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line or line == command:
                            continue
                        lines.append(line)
                        if line.startswith("+CUSD:"):
                            ussd_received_at = time.monotonic()
                            response = decode_ussd_response(line)
                            if ussd_session_is_interactive(response):
                                reply = None
                                if reply_strategy is not None:
                                    if replies_sent < max_replies:
                                        menu_text = extract_carrier_message(response)
                                        reply = reply_strategy(menu_text)
                                elif next_reply < len(replies):
                                    reply = str(replies[next_reply])
                                    next_reply += 1
                                if reply is not None:
                                    replies_sent += 1
                                    reply_command = f'AT+CUSD=1,"{reply}",15'
                                    lines.append(
                                        f"> RESPONSE {replies_sent} SENT ({reply})"
                                    )
                                    modem.write((reply_command + "\r").encode("ascii"))
                                    modem.flush()
                                    ussd_received_at = None
                        if line == "ERROR" or line.startswith("+CME ERROR"):
                            return ModemResult(
                                False,
                                decode_ussd_response("\n".join(lines)),
                            )
            except (OSError, serial.SerialException) as exc:
                raise ModemError(f"Serial modem error on {serial_port}: {exc}") from exc

        if lines:
            response = decode_ussd_response("\n".join(lines))
            return ModemResult(_ussd_response_is_successful(response), response)
        raise ModemError("Timed out waiting for the modem USSD response")

    @staticmethod
    def _command(modem, command):
        modem.write((command + "\r").encode("ascii"))
        modem.flush()
        deadline = time.monotonic() + 5
        lines = []
        while time.monotonic() < deadline:
            line = modem.readline().decode("utf-8", errors="replace").strip()
            if not line or line == command:
                continue
            lines.append(line)
            if line == "OK":
                return
            if line == "ERROR" or line.startswith("+CME ERROR"):
                break
        raise ModemError(f"Modem rejected initialization command: {command}")


def detect_serial_ports(timeout=1):
    try:
        import serial
        from serial.tools import list_ports
    except ImportError as exc:
        raise ModemError("pyserial is required for modem detection") from exc

    detected = []
    for port_info in sorted(list_ports.comports(), key=lambda item: item.device):
        detected.append(_probe_port(serial, port_info, timeout))
    return detected


def verify_sim_number(port, operator):
    try:
        primary_code = SIM_NUMBER_CODES[operator]
    except KeyError as exc:
        raise ModemError(f"No SIM-number check code configured for {operator}") from exc

    codes = SIM_NUMBER_FALLBACK_CODES.get(operator, (primary_code,))
    responses = []
    modem = get_modem()
    for code in codes:
        result = modem.send_ussd(code, dcs=15, port=port)
        responses.append(f"{code}\n{result.response}")
        number = extract_bangladesh_number(result.response)
        if number:
            return number, "\n\n".join(responses)
    if "+STKPCI" in result.response:
        logger.warning("SIM Toolkit response detected instead of USSD.")
        return "", result.response
    raise ModemError(
        "SIM number check failed; no Bangladesh mobile number was found. "
        "Responses:\n" + "\n\n".join(responses)
    )


def extract_bangladesh_number(response):
    decoded = decode_ussd_response(response)
    match = re.search(r"(?<!\d)(?:\+?88)?(01[3-9]\d{8})(?!\d)", decoded)
    return match.group(1) if match else ""


def decode_ussd_response(value):
    def replace_hex(match):
        encoded = match.group(1)
        try:
            return bytes.fromhex(encoded).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return encoded

    return re.sub(r'"([0-9A-Fa-f]{16,})"', lambda match: f'"{replace_hex(match)}"', value)


def decode_sms_value(value):
    value = value.strip()
    if len(value) < 4 or len(value) % 4 or not re.fullmatch(r"[0-9A-Fa-f]+", value):
        return value
    try:
        return bytes.fromhex(value).decode("utf-16-be").strip("\x00")
    except (ValueError, UnicodeDecodeError):
        return value


def parse_sms_messages(response):
    parsed = []
    current = None
    body = []

    def append_current():
        if current is None:
            return
        text = "\n".join(body).strip()
        parsed.append(
            ModemMessage(
                index=current["index"],
                status=decode_sms_value(current["status"]),
                sender=decode_sms_value(current["sender"]),
                timestamp=decode_sms_value(current["timestamp"]),
                text=decode_sms_value(text),
            )
        )

    # Some multi-port modems omit the CR/LF before the next +CMGL header.
    # Insert a boundary so one SMS cannot absorb all following records.
    response = re.sub(r"(?<!^)(?=\+CMGL:\s*\d+)", "\n", response)

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if line.startswith("+CMGL:"):
            append_current()
            body = []
            index_match = re.match(r"\+CMGL:\s*(\d+)", line)
            quoted_fields = re.findall(r'"([^"]*)"', line)
            if not index_match or len(quoted_fields) < 2:
                current = None
                continue
            current = {
                "index": int(index_match.group(1)),
                "status": quoted_fields[0],
                "sender": quoted_fields[1],
                "timestamp": quoted_fields[-1] if len(quoted_fields) >= 3 else "",
            }
        elif current is not None and line not in {"OK", "ERROR"}:
            body.append(line)

    append_current()
    return parsed


def _swap_semi_octets(value):
    return "".join(value[index + 1] + value[index] for index in range(0, len(value), 2))


def _decode_gsm7(data, septet_count):
    basic = (
        "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ"
        "\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
        "abcdefghijklmnopqrstuvwxyzäöñüà"
    )
    bits = "".join(f"{byte:08b}"[::-1] for byte in data)
    characters = []
    for index in range(septet_count):
        chunk = bits[index * 7 : (index + 1) * 7]
        if len(chunk) < 7:
            break
        value = int(chunk[::-1], 2)
        characters.append(basic[value] if value < len(basic) else "?")
    return "".join(characters)


def decode_sms_pdu(pdu):
    data = bytes.fromhex(pdu)
    cursor = 0
    smsc_length = data[cursor]
    cursor += 1 + smsc_length

    first_octet = data[cursor]
    cursor += 1
    sender_length = data[cursor]
    cursor += 1
    sender_type = data[cursor]
    cursor += 1
    sender_bytes = (sender_length + 1) // 2
    sender_digits = _swap_semi_octets(data[cursor : cursor + sender_bytes].hex())
    sender_digits = sender_digits[:sender_length]
    cursor += sender_bytes
    sender = ("+" if sender_type == 0x91 else "") + sender_digits

    cursor += 1
    dcs = data[cursor]
    cursor += 1
    timestamp_bytes = data[cursor : cursor + 7]
    cursor += 7
    timestamp_digits = _swap_semi_octets(timestamp_bytes.hex())
    timestamp = (
        f"{timestamp_digits[0:2]}/{timestamp_digits[2:4]}/"
        f"{timestamp_digits[4:6]} {timestamp_digits[6:8]}:"
        f"{timestamp_digits[8:10]}:{timestamp_digits[10:12]}"
    )

    user_length = data[cursor]
    cursor += 1
    user_data = data[cursor:]
    if dcs & 0x0C == 0x08:
        text = user_data[:user_length].decode("utf-16-be", errors="replace")
    elif dcs & 0x0C == 0x04:
        text = user_data[:user_length].decode("latin-1", errors="replace")
    else:
        text = _decode_gsm7(user_data, user_length)

    if first_octet & 0x40 and text:
        text = text.lstrip("\x00")
    return sender, timestamp, text


def parse_pdu_messages(response):
    parsed = []
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not line.startswith("+CMGL:") or index + 1 >= len(lines):
            continue
        match = re.match(r"\+CMGL:\s*(\d+),(\d+)", line)
        pdu = lines[index + 1]
        if not match or not re.fullmatch(r"[0-9A-Fa-f]+", pdu):
            continue
        try:
            sender, timestamp, text = decode_sms_pdu(pdu)
        except (IndexError, ValueError, UnicodeDecodeError):
            continue
        parsed.append(
            ModemMessage(
                index=int(match.group(1)),
                status=match.group(2),
                sender=sender,
                timestamp=timestamp,
                text=text,
            )
        )
    return parsed


def extract_carrier_transaction_id(response):
    decoded = decode_ussd_response(response)
    match = re.search(
        r"\bR[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)+\b",
        decoded,
        re.IGNORECASE,
    )
    if match:
        return match.group(0)

    # "Transaction ID R...", "Transaction ID BD...", "TXN ID : IRISUSSD...",
    # "TXN no 123" etc. The label can be "transaction"/"txn", the keyword
    # "id"/"number"/"no", with an optional ":" or "#" before the value.
    match = re.search(
        r"(?:transaction|txn)\s*(?:id|number|no\.?)\s*[:#]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9._&=\-]*)",
        decoded,
        re.IGNORECASE,
    )
    return match.group(1).strip().rstrip(".") if match else ""


def carrier_has_transaction_id(response):
    decoded = decode_ussd_response(response)
    return bool(
        extract_carrier_transaction_id(decoded)
        or re.search(
            r"(?:transaction|txn)\s*(?:id|number|no\.?)",
            decoded,
            re.IGNORECASE,
        )
    )


def carrier_response_is_pending(response):
    decoded = decode_ussd_response(response)
    # Don't mark as pending if it's clearly a failure
    if carrier_response_is_failure(decoded):
        return False
    return bool(
        re.search(
            r"\b(?:under\s*process|processing|confirmation\s+message\s+shortly)\b",
            decoded,
            re.IGNORECASE,
        )
    )


def carrier_response_is_failure(response):
    decoded = decode_ussd_response(response)
    return bool(
        re.search(
            r"\b(?:cannot\s+be\s+processed|failed|unsuccessful|declined|rejected"
            r"|incorrect\s+pin|invalid\s+pin|wrong\s+pin"
            r"|pin\b.{0,50}\b(?:incorrect|invalid|wrong)"
            r"|insufficient\s+(?:balance|funds)|not\s+enough\s+balance"
            r"|failed\s+due\s+to|you\s+do\s+not\s+have\s+sufficient"
            r"|\bfailed\b.{0,30}\b(?:due\s+to|because)|\bnot\s+sufficient\b)",
            decoded,
            re.IGNORECASE,
        )
    )


def carrier_response_is_success(response):
    decoded = decode_ussd_response(response)
    if carrier_response_is_pending(decoded) or carrier_response_is_failure(decoded):
        return False
    # Check for explicit success indicators
    return bool(
        re.search(
            r"\b(?:successful|successfully|completed|approved)\b",
            decoded,
            re.IGNORECASE,
        )
    ) or bool(re.search(r"ref\s*no\s*[:.]\s*[A-Z0-9]+", decoded, re.IGNORECASE))


def extract_carrier_message(response):
    decoded = decode_ussd_response(response)
    matches = re.findall(
        r'\+CUSD:\s*\d+\s*,\s*"((?:[^"\\]|\\.)*)"',
        decoded,
        flags=re.DOTALL,
    )
    if not matches:
        return decoded.strip()
    return matches[-1].replace(r"\"", '"').strip()


def normalize_bd_number(value):
    """Return a canonical Bangladesh mobile number (01XXXXXXXXX, 11 digits).

    Accepts the common formats seen in modem SMS and USSD text, including
    ``017XXXXXXXX``, ``88017XXXXXXXX`` and ``+88017XXXXXXXX``. Returns an
    empty string when no valid BD mobile number can be found, so callers can
    tell "no number" apart from a wrong guess and never match on garbage.
    """
    if not value:
        return ""
    digits = re.sub(r"\D", "", str(value))
    # Already in / ends with the national 01XXXXXXXXX form (drops any 88/880
    # country code that may sit in front of it).
    match = re.search(r"(01[3-9]\d{8})$", digits)
    if match:
        return match.group(1)
    # National number written without the leading zero (1XXXXXXXXX).
    match = re.search(r"(1[3-9]\d{8})$", digits)
    if match:
        return "0" + match.group(1)
    return ""


def extract_recipient_number(text):
    """Extract the recipient/target BD mobile number from an SMS body.

    Returns the normalized 01XXXXXXXXX form, or "" when the message carries
    no mobile number (for example a GP "Failed due to ... Ref no : ..." SMS).
    """
    if not text:
        return ""
    match = re.search(r"(?<!\d)(?:\+?88)?0?1[3-9]\d{8}(?!\d)", text)
    return normalize_bd_number(match.group(0)) if match else ""


def extract_sms_transaction_id(text):
    """Extract the carrier transaction/reference ID from a received SMS.

    Handles the real formats stored by this system, e.g.
    ``... Transaction ID BD060926162634048087.`` (success) and
    ``Failed due to 115:Duplicate transaction detected. Ref no : BD...``
    (failure). Returns "" when none is present.
    """
    if not text:
        return ""
    match = re.search(
        r"transaction\s*(?:id|no\.?|number)\s*[:.]?\s*([A-Za-z0-9][A-Za-z0-9.\-]*)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).rstrip(".")
    match = re.search(
        r"ref(?:erence)?\s*(?:no\.?|number)?\s*[:.]\s*([A-Za-z0-9][A-Za-z0-9.\-]*)",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).rstrip(".")
    return ""


def classify_sms_result(text):
    """Classify a received SMS as a recharge result.

    Returns ``"success"``, ``"failed"`` or ``""``. An empty string means the
    SMS is not a recharge/bill-payment result at all (promotional message,
    OTP, balance notice, ...) and must never change a transaction's status.
    Failure is checked first so a message that mentions both never slips
    through as a success.
    """
    if not text:
        return ""
    if carrier_response_is_failure(text):
        return "failed"
    if carrier_response_is_success(text):
        return "success"
    return ""


def parse_ussd_menu_options(menu_text):
    """Split an interactive USSD menu into (key, body) options.

    Options look like ``0:Default Recharge (749Tk) 1:40GB...-749Tk *:Next``.
    Returns e.g. [("0", "Default Recharge (749Tk)"), ("1", "40GB...-749Tk"),
    ("*", "Next")].
    """
    if not menu_text:
        return []
    matches = list(re.finditer(r"([0-9]|\*)\s*:", menu_text))
    options = []
    for index, match in enumerate(matches):
        key = match.group(1)
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(menu_text)
        options.append((key, menu_text[start:end].strip()))
    return options


def menu_option_price(body):
    """The package price of a menu option, or None.

    The price is the first ``<number>Tk`` in the option; a commission shown as
    ``C 50TK`` is ignored.
    """
    if not body:
        return None
    without_commission = re.sub(
        r"\bC\s*\d+\s*Tk\b", " ", body, flags=re.IGNORECASE
    )
    match = re.search(r"(\d+)\s*Tk\b", without_commission, re.IGNORECASE)
    return int(match.group(1)) if match else None


def package_reply_strategy(amount):
    """Build a reply chooser for a Banglalink package menu.

    On each menu page it looks for a package (any option other than the default
    ``0``) whose price equals ``amount`` and returns that option's key to buy
    it. If none matches and the page offers ``*:Next`` it returns ``"*"`` to go
    to the next page; once the pages run out it returns ``"0"`` (Default
    Recharge / cash).
    """
    def choose(menu_text):
        options = parse_ussd_menu_options(menu_text)
        for key, body in options:
            if key in ("0", "*"):
                continue
            if menu_option_price(body) == amount:
                return key
        if any(key == "*" for key, _ in options):
            return "*"
        return "0"

    return choose


def _cmgr_matches_expected(response, expected_text):
    """Return True when an AT+CMGR read-back holds ``expected_text``.

    Used to make single-message deletion safe: we only delete a slot that
    still contains the message we already stored.
    """
    if not response or "+CMGR:" not in response or not expected_text:
        return False
    body_lines = []
    for raw in response.splitlines():
        line = raw.strip()
        if (
            not line
            or line in {"OK", "ERROR"}
            or line.startswith("+CMGR:")
            or line.startswith("+CME ERROR")
        ):
            continue
        body_lines.append(line)
    body = "\n".join(decode_sms_value(line) for line in body_lines).strip()
    if not body:
        return False
    expected_signature = re.sub(r"\s+", "", expected_text)[:40]
    body_signature = re.sub(r"\s+", "", body)[:40]
    if expected_signature and (
        expected_signature in body_signature or body_signature in expected_signature
    ):
        return True
    expected_id = extract_sms_transaction_id(expected_text)
    return bool(expected_id and expected_id in body)


def _ussd_response_is_successful(response):
    upper = response.upper()
    if "ERROR" in upper or carrier_response_is_failure(response):
        return False
    statuses = re.findall(r"\+CUSD:\s*(\d+)", response)
    return bool(statuses and statuses[-1] in {"0", "2"})


def ussd_session_is_interactive(response):
    statuses = re.findall(r"\+CUSD:\s*(\d+)", response)
    return bool(statuses and statuses[-1] == "1")


def _probe_port(serial, port_info, timeout):
    details = {
        "device": port_info.device,
        "description": port_info.description or "",
        "responsive": False,
    }
    try:
        with serial.Serial(
            port_info.device,
            settings.MODEM_BAUDRATE,
            timeout=timeout,
            write_timeout=2,
        ) as modem:
            modem.reset_input_buffer()
            if "OK" not in _query(modem, "AT", timeout=2):
                return DetectedPort(**details, error="No AT response")

            details["responsive"] = True
            identity = _query(modem, "ATI", timeout=2)
            details["manufacturer"], details["model"] = _parse_identity(identity)
            details["imei"] = _first_digits(
                _query(modem, "AT+CGSN", timeout=2),
                minimum=14,
            )
            details["sim_status"] = _value_after_prefix(
                _query(modem, "AT+CPIN?", timeout=2),
                "+CPIN:",
            )
            operator_response = _query(modem, "AT+COPS?", timeout=3)
            details["operator_name"] = _parse_operator_name(operator_response)
            details["operator"] = normalize_operator(details["operator_name"])
            details["signal"] = _value_after_prefix(
                _query(modem, "AT+CSQ", timeout=2),
                "+CSQ:",
            )
    except (OSError, serial.SerialException) as exc:
        details["error"] = str(exc)
    except ModemError as exc:
        details["error"] = str(exc)
    return DetectedPort(**details)


def _query(modem, command, timeout=2):
    modem.reset_input_buffer()
    modem.write((command + "\r").encode("ascii"))
    modem.flush()
    deadline = time.monotonic() + timeout
    lines = []
    while time.monotonic() < deadline:
        raw = modem.readline()
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line == command:
            continue
        lines.append(line)
        if line == "OK" or line == "ERROR" or line.startswith("+CME ERROR"):
            break
    return "\n".join(lines)


def normalize_operator(value):
    lowered = value.lower()
    for operator, aliases in OPERATOR_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return operator
    return ""


def operator_matches_network(selected_operator, detected_operator, network_name=""):
    if not detected_operator or selected_operator == detected_operator:
        return True
    if "47002" in network_name and selected_operator in {"robi", "airtel"}:
        return True
    return False


def _parse_operator_name(response):
    match = re.search(r'\+COPS:\s*\d+,\d+,"([^"]+)"', response)
    return match.group(1).strip() if match else ""


def _parse_identity(response):
    lines = [
        line.strip()
        for line in response.splitlines()
        if line.strip() not in {"OK", "ERROR"}
    ]
    if not lines:
        return "", ""
    manufacturer = lines[0]
    model = lines[1] if len(lines) > 1 else ""
    return manufacturer, model


def _value_after_prefix(response, prefix):
    for line in response.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return ""


def _first_digits(response, minimum):
    for line in response.splitlines():
        value = line.strip()
        if value.isdigit() and len(value) >= minimum:
            return value
    return ""


def get_modem():
    if settings.MODEM_BACKEND == "mock":
        return MockModem()
    if settings.MODEM_BACKEND == "serial":
        return SerialModem()
    raise ModemError(f"Unknown MODEM_BACKEND: {settings.MODEM_BACKEND}")
