"""Model tests that need no firmware image.

Expected values come from sources outside this repository's firmware:
the Bambu-Bus README (CRC parameters, a captured header), Python's binascii
(an independent CRC-CCITT implementation), and LLVM's Xwchc instruction
layout (encoded here by a separate encoder, not by the decoder under test).
"""
from __future__ import annotations

import binascii
import unittest

from sim.bambu_bus import crc8, crc16, parse_frames, short_frame

try:
    import unicorn  # noqa: F401
    HAVE_UNICORN = True
except ImportError:  # pragma: no cover
    HAVE_UNICORN = False


class BambuBusCrc(unittest.TestCase):
    def test_crc8_matches_a_captured_short_header(self):
        # 3D C0 1D B4: header bytes and CRC8 of a short frame published in
        # Bambu-Research-Group/Bambu-Bus (AMCU-v2 BambuBus.cpp, F00_res).
        self.assertEqual(crc8(bytes((0x3D, 0xC0, 0x1D))), 0xB4)

    def test_crc16_is_ccitt_with_the_bambu_seed(self):
        for data in (b"123456789", bytes(range(64)), b"\x3d\xc5\x0b"):
            self.assertEqual(crc16(data), binascii.crc_hqx(data, 0x913D))

    def test_short_frame_round_trip(self):
        f = short_frame(0x03, b"\x00\x00\xff\x00")
        self.assertEqual(f[0], 0x3D)
        self.assertEqual(f[2], len(f))
        (frame,) = parse_frames(f)
        self.assertTrue(frame.ok, frame.errors)
        self.assertEqual(frame.ftype, 0x03)
        self.assertEqual(frame.payload, b"\x00\x00\xff\x00")

    def test_corruption_is_reported_per_crc(self):
        f = bytearray(short_frame(0x20, b""))
        f[4] ^= 1
        (frame,) = parse_frames(bytes(f))
        self.assertEqual(frame.errors, ["crc16"])
        f = bytearray(short_frame(0x20, b""))
        f[3] ^= 1
        (frame,) = parse_frames(bytes(f))
        self.assertIn("crc8", frame.errors)


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed")
class CrcUnit(unittest.TestCase):
    def test_stm32_compatible_known_answer(self):
        # 0x12345678 -> 0xDF8A8A2B is the widely published result of the
        # STM32F1-family CRC unit (CRC-32/MPEG-2 on one word), whose register
        # interface the CH32V20x manual reproduces.
        from sim.periph import CRCUnit
        c = CRCUnit()
        c.feed(0x12345678)
        self.assertEqual(c.crc, 0xDF8A8A2B)
        c.write(0x08, 4, 1)
        self.assertEqual(c.crc, 0xFFFFFFFF)


def _creg(x):
    assert 8 <= x <= 15
    return x - 8


def enc_lbu_sb(store, rd_rs2, rs1, imm):
    # QK_C_LBU / QK_C_SB: Inst{12}=imm{0}, Inst{11-10}=imm{4-3}, Inst{6-5}=imm{2-1}
    return ((0b101 if store else 0b001) << 13 | (imm & 1) << 12 | (imm >> 3 & 3) << 10 |
            _creg(rs1) << 7 | (imm >> 1 & 3) << 5 | _creg(rd_rs2) << 2 | 0b00)


def enc_lhu_sh(store, rd_rs2, rs1, imm):
    # QK_C_LHU / QK_C_SH: Inst{12-10}=imm{5-3}, Inst{6-5}=imm{2-1}, opcode C2
    return ((0b101 if store else 0b001) << 13 | (imm >> 3 & 7) << 10 |
            _creg(rs1) << 7 | (imm >> 1 & 3) << 5 | _creg(rd_rs2) << 2 | 0b10)


def enc_sp(funct2, rd_rs2, imm):
    # QKStackInst: Inst{15-11}=10000, Inst{6-5}=funct2, byte: Inst{10-7}=imm,
    # half: Inst{10-8}=imm{3-1}, Inst{7}=imm{4}
    if funct2 & 1:
        field = (imm >> 1 & 7) << 8 | (imm >> 4 & 1) << 7
    else:
        field = (imm & 0xF) << 7
    return 0b10000 << 11 | field | funct2 << 5 | _creg(rd_rs2) << 2


@unittest.skipUnless(HAVE_UNICORN, "unicorn not installed")
class XwInstructions(unittest.TestCase):
    RAM = 0x2000_0100

    def run_program(self, halfwords, regs):
        from unicorn import riscv_const as R

        from sim.machine import Machine
        loop = 0xA001                      # c.j . (spin)
        image = b"".join(h.to_bytes(2, "little") for h in list(halfwords) + [loop])
        m = Machine(image)
        for r, v in regs.items():
            m.uc.reg_write(R.UC_RISCV_REG_X0 + r, v)
        m.uc.mem_write(self.RAM, bytes(range(0x40, 0x80)))
        m.run_until(200)
        self.assertIsNone(m.fault)
        return m, lambda r: m.uc.reg_read(R.UC_RISCV_REG_X0 + r)

    def test_byte_load_store(self):
        prog = [enc_lbu_sb(False, 15, 14, 0), enc_lbu_sb(False, 13, 14, 29), enc_lbu_sb(True, 13, 14, 1)]
        m, x = self.run_program(prog, {14: self.RAM})
        self.assertEqual(x(15), 0x40)
        self.assertEqual(x(13), 0x40 + 29)
        self.assertEqual(m.uc.mem_read(self.RAM + 1, 1)[0], 0x40 + 29)
        self.assertEqual(m.xw_count, 3)

    def test_halfword_load_store(self):
        prog = [enc_lhu_sh(False, 12, 8, 62), enc_lhu_sh(True, 12, 8, 2)]
        m, x = self.run_program(prog, {8: self.RAM})
        self.assertEqual(x(12), 0x40 + 62 | (0x40 + 63) << 8)
        self.assertEqual(bytes(m.uc.mem_read(self.RAM + 2, 2)), bytes((0x40 + 62, 0x40 + 63)))

    def test_sp_relative_forms(self):
        prog = [enc_sp(0b00, 9, 15), enc_sp(0b01, 10, 30), enc_sp(0b10, 9, 0), enc_sp(0b11, 10, 4)]
        m, x = self.run_program(prog, {2: self.RAM})
        self.assertEqual(x(9), 0x40 + 15)
        self.assertEqual(x(10), 0x40 + 30 | (0x40 + 31) << 8)
        self.assertEqual(m.uc.mem_read(self.RAM, 1)[0], 0x40 + 15)
        self.assertEqual(bytes(m.uc.mem_read(self.RAM + 4, 2)), bytes((0x40 + 30, 0x40 + 31)))


if __name__ == "__main__":
    unittest.main()
