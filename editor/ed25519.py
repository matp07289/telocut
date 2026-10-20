"""Ed25519（RFC 8032）の署名と検証。外部ライブラリなしで動く（ライセンスキーの確認用）。

速さより確かさ優先の素直な実装。検証 1 回は数十ミリ秒。
"""
from __future__ import annotations

import hashlib

_p = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_I = pow(2, (_p - 1) // 4, _p)


def _H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p)
    x = pow(xx, (_p + 3) // 8, _p)
    if (x * x - xx) % _p != 0:
        x = (x * _I) % _p
    if x % 2 != 0:
        x = _p - x
    return x


_By = 4 * pow(5, _p - 2, _p) % _p
_B = (_xrecover(_By), _By, 1, _xrecover(_By) * _By % _p)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    A = (y1 - x1) * (y2 - x2) % _p
    Bv = (y1 + x1) * (y2 + x2) % _p
    Cv = t1 * 2 * _d * t2 % _p
    Dv = z1 * 2 * z2 % _p
    E, F, G, Hh = Bv - A, Dv - Cv, Dv + Cv, Bv + A
    return (E * F % _p, G * Hh % _p, F * G % _p, E * Hh % _p)


def _mul(s: int, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _enc(P) -> bytes:
    x, y, z, _t = P
    zi = pow(z, _p - 2, _p)
    x, y = x * zi % _p, y * zi % _p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _dec(s: bytes):
    if len(s) != 32:
        raise ValueError("長さが違います")
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _p:
        raise ValueError("点が正しくありません")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _p - x
    P = (x, y, 1, x * y % _p)
    if not _on_curve(P):
        raise ValueError("曲線上にありません")
    return P


def _on_curve(P) -> bool:
    x, y, z, t = P
    return (z % _p != 0 and x * y % _p == z * t % _p
            and (y * y - x * x - z * z - _d * t * t) % _p == 0)


def _eq(P, Q) -> bool:
    x1, y1, z1, _ = P
    x2, y2, z2, _ = Q
    return (x1 * z2 - x2 * z1) % _p == 0 and (y1 * z2 - y2 * z1) % _p == 0


def _secret_expand(sk: bytes):
    h = _H(sk)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(sk: bytes) -> bytes:
    a, _ = _secret_expand(sk)
    return _enc(_mul(a, _B))


def sign(sk: bytes, msg: bytes) -> bytes:
    a, prefix = _secret_expand(sk)
    A = _enc(_mul(a, _B))
    r = int.from_bytes(_H(prefix + msg), "little") % _L
    R = _enc(_mul(r, _B))
    h = int.from_bytes(_H(R + A + msg), "little") % _L
    s = (r + h * a) % _L
    return R + s.to_bytes(32, "little")


def verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    try:
        if len(sig) != 64:
            return False
        A = _dec(pk)
        R = _dec(sig[:32])
        s = int.from_bytes(sig[32:], "little")
        if s >= _L:
            return False
        h = int.from_bytes(_H(sig[:32] + pk + msg), "little") % _L
        return _eq(_mul(s, _B), _add(R, _mul(h, A)))
    except (ValueError, TypeError):
        return False
