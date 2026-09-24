"""MSB-first bit writer and reader, matching the f(n) descriptor of the AFGS1 spec."""

from __future__ import annotations


class BitWriter:
    def __init__(self) -> None:
        self._value = 0
        self._length = 0

    def __len__(self) -> int:
        return self._length

    def put(self, value: int, width: int) -> None:
        if width < 0:
            raise ValueError("negative bit width")
        if value < 0 or value >= (1 << width):
            raise ValueError(f"{value} does not fit in {width} bits")
        self._value = (self._value << width) | value
        self._length += width

    def put_bits(self, other: "BitWriter") -> None:
        self._value = (self._value << other._length) | other._value
        self._length += other._length

    def pad_to(self, total_bits: int) -> None:
        if total_bits < self._length:
            raise ValueError("cannot pad to fewer bits than already written")
        self.put(0, total_bits - self._length)

    def to_bytes(self) -> bytes:
        """Return the bits, zero-padded at the end to a whole number of bytes."""
        pad = -self._length % 8
        return (self._value << pad).to_bytes((self._length + pad) // 8, "big")


class BitReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.position = 0

    @property
    def bits_left(self) -> int:
        return len(self._data) * 8 - self.position

    def get(self, width: int) -> int:
        if width > self.bits_left:
            raise ValueError("read past the end of the payload")
        value = 0
        for _ in range(width):
            byte = self._data[self.position >> 3]
            value = (value << 1) | ((byte >> (7 - (self.position & 7))) & 1)
            self.position += 1
        return value
