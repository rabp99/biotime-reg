from datetime import datetime

import pytest

from app.protocol import (
    DeviceStamps,
    build_handshake,
    decode_body,
    normalize_command,
    parse_attlog,
    parse_info,
    parse_options_payload,
    parse_timestamp,
)


# ---------------------------------------------------------------------------
# parse_attlog
# ---------------------------------------------------------------------------

def test_parse_attlog_linea_completa():
    payload = "123\t2026-08-07 08:00:00\t0\t15\t0\n"
    records, skipped = parse_attlog(payload)

    assert skipped == []
    assert len(records) == 1
    record = records[0]
    assert record.pin == "123"
    assert record.punch_time == datetime(2026, 8, 7, 8, 0, 0)
    assert record.status == 0
    assert record.verify_mode == 15
    # BioTime guarda literalmente "0" en las 56.891 marcaciones de los equipos
    # activos, no NULL. Se conserva el valor tal cual para no divergir.
    assert record.work_code == "0"


def test_parse_attlog_varias_lineas():
    payload = (
        "1\t2026-08-07 08:00:00\t0\t15\t0\n"
        "2\t2026-08-07 08:01:13\t1\t1\t0\n"
        "3\t2026-08-07 17:30:00\t1\t15\t0\n"
    )
    records, skipped = parse_attlog(payload)

    assert skipped == []
    assert [r.pin for r in records] == ["1", "2", "3"]


def test_parse_attlog_conserva_ceros_a_la_izquierda():
    """El PIN es texto: castearlo a int perdería identificadores como 00123."""
    records, _ = parse_attlog("00123\t2026-08-07 08:00:00\t0\t15\t0")

    assert records[0].pin == "00123"


def test_parse_attlog_ignora_campos_extra():
    """Firmwares distintos añaden campos reservados al final."""
    payload = "5\t2026-08-07 08:00:00\t0\t15\t0\t0\t0\t\t0\t99"
    records, skipped = parse_attlog(payload)

    assert skipped == []
    assert records[0].pin == "5"
    assert records[0].verify_mode == 15


def test_parse_attlog_acepta_campos_minimos():
    """Sin status/verify: se acepta la marcación, esos campos quedan a None."""
    records, skipped = parse_attlog("7\t2026-08-07 09:15:00")

    assert skipped == []
    assert records[0].punch_time == datetime(2026, 8, 7, 9, 15)
    assert records[0].status is None
    assert records[0].verify_mode is None


def test_parse_attlog_salta_lineas_invalidas_sin_perder_las_validas():
    """Una línea corrupta no puede tumbar el lote entero."""
    payload = (
        "1\t2026-08-07 08:00:00\t0\t15\t0\n"
        "basura sin tabuladores\n"
        "\n"
        "2\tfecha-imposible\t0\t15\t0\n"
        "3\t2026-08-07 08:02:00\t0\t15\t0\n"
    )
    records, skipped = parse_attlog(payload)

    assert [r.pin for r in records] == ["1", "3"]
    assert len(skipped) == 2


def test_parse_attlog_descarta_pin_vacio():
    records, skipped = parse_attlog("\t2026-08-07 08:00:00\t0\t15\t0")

    assert records == []
    assert len(skipped) == 1


def test_parse_attlog_soporta_crlf():
    """Los terminales terminan las líneas con CRLF."""
    payload = "1\t2026-08-07 08:00:00\t0\t15\t0\r\n2\t2026-08-07 08:05:00\t1\t15\t0\r\n"
    records, skipped = parse_attlog(payload)

    assert len(records) == 2
    assert skipped == []
    assert not records[0].raw_line.endswith("\r")


def test_parse_attlog_payload_vacio():
    assert parse_attlog("") == ([], [])
    assert parse_attlog("\n\n  \n") == ([], [])


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-07 08:00:00", datetime(2026, 8, 7, 8, 0, 0)),
        ("2026-08-07T08:00:00", datetime(2026, 8, 7, 8, 0, 0)),
        ("2026-08-07 08:00", datetime(2026, 8, 7, 8, 0)),
        ("2026/08/07 08:00:00", datetime(2026, 8, 7, 8, 0, 0)),
        ("  2026-08-07 08:00:00  ", datetime(2026, 8, 7, 8, 0, 0)),
    ],
)
def test_parse_timestamp_formatos_aceptados(value, expected):
    assert parse_timestamp(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "no es fecha", "2026-13-45 99:99:99", "123"])
def test_parse_timestamp_rechaza_invalidos(value):
    """'123' es un epoch fuera de rango razonable: se rechaza, probablemente es un PIN."""
    assert parse_timestamp(value) is None


def test_parse_timestamp_epoch_en_rango():
    assert parse_timestamp("1780000000") is not None


# ---------------------------------------------------------------------------
# normalize_command
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "subpath,expected",
    [
        ("cdata", "cdata"),
        ("cdata.aspx", "cdata"),
        ("CDATA", "cdata"),
        ("/getrequest", "getrequest"),
        ("devicecmd", "devicecmd"),
        ("registry/extra", "registry"),
        ("", ""),
    ],
)
def test_normalize_command(subpath, expected):
    assert normalize_command(subpath) == expected


# ---------------------------------------------------------------------------
# build_handshake
# ---------------------------------------------------------------------------

def test_build_handshake_incluye_sn_y_stamps():
    body = build_handshake(
        "ABC123",
        DeviceStamps(stamp="9000", op_stamp="10", photo_stamp="0"),
        {"TransFlag": "TransData AttLog", "Realtime": "1"},
    )
    lines = body.splitlines()

    assert lines[0] == "GET OPTION FROM: ABC123"
    assert "Stamp=9000" in lines
    assert "OpStamp=10" in lines
    assert "TransFlag=TransData AttLog" in lines
    assert "Realtime=1" in lines
    assert body.endswith("\n")


def test_build_handshake_options_puede_sobrescribir_stamps():
    """Permite forzar un reenvío completo desde configuración."""
    body = build_handshake("SN1", DeviceStamps(stamp="9000"), {"Stamp": "0"})

    assert "Stamp=0" in body.splitlines()
    assert "Stamp=9000" not in body.splitlines()


# ---------------------------------------------------------------------------
# parse_options_payload / parse_info
# ---------------------------------------------------------------------------

def test_parse_options_payload():
    payload = "{MaskDetectionFunOn=1,IRTempDetectionFunOn=0,PvFunOn=1}"

    assert parse_options_payload(payload) == {
        "MaskDetectionFunOn": "1",
        "IRTempDetectionFunOn": "0",
        "PvFunOn": "1",
    }


def test_parse_options_payload_separado_por_lineas():
    assert parse_options_payload("A=1\nB=2\r\nC=3") == {"A": "1", "B": "2", "C": "3"}


def test_parse_options_payload_ignora_ruido():
    assert parse_options_payload("A=1,,sin_igual,B=2") == {"A": "1", "B": "2"}


def test_parse_info():
    info = "Ver 2.0.1-20150901,1,2,4,192.168.18.212,10,7,12"

    assert parse_info(info) == {
        "firmware": "Ver 2.0.1-20150901",
        "user_count": "1",
        "fp_count": "2",
        "att_count": "4",
        "ip": "192.168.18.212",
    }


def test_parse_info_incompleto():
    assert parse_info("Ver 1.0,5") == {"firmware": "Ver 1.0", "user_count": "5"}


# ---------------------------------------------------------------------------
# decode_body
# ---------------------------------------------------------------------------

def test_decode_body_utf8():
    assert decode_body("Núñez\t2026-08-07".encode("utf-8")) == "Núñez\t2026-08-07"


def test_decode_body_no_revienta_con_bytes_invalidos():
    """Un byte corrupto no puede hacer perder el lote completo."""
    body = b"1\t2026-08-07 08:00:00\t0\t15\t0\n\xff\xfe basura"
    text = decode_body(body)

    assert "2026-08-07 08:00:00" in text
    records, _ = parse_attlog(text)
    assert len(records) == 1
