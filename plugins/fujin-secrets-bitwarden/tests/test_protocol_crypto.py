"""Known-answer and EncString tests for the protocol adapter crypto."""

from __future__ import annotations

import base64
import hashlib
import hmac

import argon2.low_level as argon2_ll
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from fujin.errors import SecretResolutionError
from fujin_secrets_bitwarden.protocol import (
    Kdf,
    argon2id,
    decrypt_enc_string,
    derive_master_key,
    hkdf_expand,
    master_password_hash,
    pbkdf2_sha256,
    stretch_keys,
)

ENC_KEY = bytes(range(32))
MAC_KEY = bytes(range(32, 64))
IV = bytes(range(16))


def encrypt(
    plaintext: bytes,
    *,
    enc_key: bytes = ENC_KEY,
    mac_key: bytes = MAC_KEY,
    iv: bytes = IV,
    enc_type: int = 2,
) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(enc_key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    parts = [base64.b64encode(iv).decode(), base64.b64encode(ciphertext).decode()]
    if enc_type == 2:
        mac = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
        parts.append(base64.b64encode(mac).decode())
    return f"{enc_type}." + "|".join(parts)


# ---------------------------------------------------------------------------
# Known-answer tests
# ---------------------------------------------------------------------------


def test_pbkdf2_sha256_rfc7914_vector():
    # RFC 7914 section 11: PBKDF2-HMAC-SHA256("password", "salt", 1, 32).
    assert (
        pbkdf2_sha256(b"password", b"salt", 1).hex()
        == "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b"
    )


def test_hkdf_expand_rfc5869_test_case_1():
    prk = bytes.fromhex(
        "077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5"
    )
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    expected = bytes.fromhex(
        "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf"
        "34007208d5b887185865"
    )
    assert hkdf_expand(prk, info, 42) == expected
    # Output prefixes do not depend on the requested length.
    assert hkdf_expand(prk, info, 32) == expected[:32]


def _argon2_lib_ctx(
    password: bytes,
    salt: bytes,
    *,
    secret: bytes,
    associated_data: bytes,
    time_cost: int,
    memory_cost: int,
    parallelism: int,
    tag_length: int = 32,
) -> bytes:
    """Drive the bundled libargon2 context directly.

    The RFC vector uses the optional secret and associated-data inputs, which
    ``argon2.low_level.hash_secret_raw`` does not expose; the adapter itself
    never passes them. ``argon2_type`` 2 is ``Argon2_id``.
    """
    ffi = argon2_ll.ffi
    lib = argon2_ll.lib

    def buffer(data: bytes):
        return ffi.new("uint8_t[]", list(data))

    out = ffi.new("uint8_t[]", tag_length)
    context = ffi.new(
        "argon2_context *",
        {
            "out": out,
            "outlen": tag_length,
            "pwd": buffer(password),
            "pwdlen": len(password),
            "salt": buffer(salt),
            "saltlen": len(salt),
            "secret": buffer(secret),
            "secretlen": len(secret),
            "ad": buffer(associated_data),
            "adlen": len(associated_data),
            "t_cost": time_cost,
            "m_cost": memory_cost,
            "lanes": parallelism,
            "threads": parallelism,
            "version": 19,
            "allocate_cbk": ffi.NULL,
            "free_cbk": ffi.NULL,
            "flags": 0,
        },
    )
    if lib.argon2_ctx(context, 2) != 0:
        raise AssertionError("libargon2 rejected the RFC test vector")
    return bytes(ffi.buffer(out, tag_length))


def test_argon2id_rfc9106_vector():
    # RFC 9106 section 5.3: Argon2id v19, t=3, m=32 KiB, p=4,
    # password 0x01*32, salt 0x02*16, secret 0x03*8, associated data 0x04*12.
    tag = _argon2_lib_ctx(
        b"\x01" * 32,
        b"\x02" * 16,
        secret=b"\x03" * 8,
        associated_data=b"\x04" * 12,
        time_cost=3,
        memory_cost=32,
        parallelism=4,
    )
    assert (
        tag.hex() == "0d640df58d78766c08c037a34a8b53c9d01ef0452d75b65eb52520e96b01e659"
    )


def test_argon2id_matches_reference_implementation_vectors():
    # phc-winner-argon2 src/test.c, Argon2id v19, no secret/associated data.
    assert (
        argon2id(
            b"password", b"somesalt", iterations=2, memory=256, parallelism=1
        ).hex()
        == "9dfeb910e80bad0311fee20f9c0e2b12c17987b4cac90c2ef54d5b3021c68bfe"
    )
    assert (
        argon2id(
            b"password",
            b"somesalt",
            iterations=2,
            memory=1 << 16,
            parallelism=1,
        ).hex()
        == "09316115d5cf24ed5a15a31a3ba326e5cf32edc24702987c02b6566f61913cf7"
    )


def test_argon2id_wrapper_matches_libargon2_for_adapter_parameters():
    assert argon2id(
        b"\x01" * 32,
        b"\x02" * 16,
        iterations=3,
        memory=32,
        parallelism=4,
    ) == _argon2_lib_ctx(
        b"\x01" * 32,
        b"\x02" * 16,
        secret=b"",
        associated_data=b"",
        time_cost=3,
        memory_cost=32,
        parallelism=4,
    )


def test_derive_master_key_matches_primitives():
    kdf = Kdf(kind=0, iterations=7)
    assert derive_master_key("pw", "a@b.c", kdf) == pbkdf2_sha256(b"pw", b"a@b.c", 7)
    assert derive_master_key("pw", "a@b.c", Kdf(0, 7)) == pbkdf2_sha256(
        b"pw", b"a@b.c", 7
    )


def test_derive_master_key_rejects_unknown_kdf():
    with pytest.raises(SecretResolutionError):
        derive_master_key("pw", "a@b.c", Kdf(kind=99, iterations=1))


def test_master_password_hash_is_one_pbkdf2_round_over_the_key():
    master_key = bytes(range(32))
    assert master_password_hash(master_key, "hunter2") == hashlib.pbkdf2_hmac(
        "sha256", master_key, b"hunter2", 1, dklen=32
    )


def test_stretch_keys_are_hkdf_expansions_of_the_master_key():
    master_key = bytes(range(32))
    enc_key, mac_key = stretch_keys(master_key)
    assert enc_key == hkdf_expand(master_key, b"enc", 32)
    assert mac_key == hkdf_expand(master_key, b"mac", 32)
    assert len(enc_key) == len(mac_key) == 32
    assert enc_key != mac_key


# ---------------------------------------------------------------------------
# EncString
# ---------------------------------------------------------------------------


def test_decrypts_type_2_enc_string():
    value = encrypt(b"s3cr3t")
    assert value.startswith("2.")
    assert decrypt_enc_string(value, ENC_KEY, MAC_KEY) == b"s3cr3t"


def test_decrypts_type_0_enc_string_without_mac():
    value = encrypt(b"no mac here", enc_type=0)
    assert value.startswith("0.")
    assert decrypt_enc_string(value, ENC_KEY, MAC_KEY) == b"no mac here"


def test_type_0_ignores_the_mac_key():
    value = encrypt(b"value", enc_type=0)
    assert decrypt_enc_string(value, ENC_KEY, b"\x00" * 32) == b"value"


def test_tampered_ciphertext_fails_mac_verification():
    value = encrypt(b"payroll")
    enc_type, _, payload = value.partition(".")
    iv_b64, ct_b64, mac_b64 = payload.split("|")
    ciphertext = bytearray(base64.b64decode(ct_b64))
    ciphertext[-1] ^= 0x01
    tampered = "|".join([iv_b64, base64.b64encode(bytes(ciphertext)).decode(), mac_b64])
    with pytest.raises(SecretResolutionError) as excinfo:
        decrypt_enc_string(f"{enc_type}.{tampered}", ENC_KEY, MAC_KEY)
    assert "MAC" in excinfo.value.message


def test_tampered_mac_fails_closed():
    value = encrypt(b"payroll")
    enc_type, _, payload = value.partition(".")
    iv_b64, ct_b64, mac_b64 = payload.split("|")
    mac = bytearray(base64.b64decode(mac_b64))
    mac[0] ^= 0x01
    tampered = "|".join([iv_b64, ct_b64, base64.b64encode(bytes(mac)).decode()])
    with pytest.raises(SecretResolutionError):
        decrypt_enc_string(f"{enc_type}.{tampered}", ENC_KEY, MAC_KEY)


def test_wrong_encryption_key_fails_closed():
    value = encrypt(b"value")
    with pytest.raises(SecretResolutionError):
        decrypt_enc_string(value, b"\x00" * 32, MAC_KEY)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-an-enc-string",
        "2.only-two|parts",
        "2.a|b|c|d",
        "0.two|parts|with-mac",
        "3.a|b|c",
        "9.whatever",
        "2.not-base64!|AAAA|AAAA",
        "0.AAAA|AAAA",
        "2.",
        ".AAAA|AAAA|AAAA",
    ],
)
def test_malformed_enc_strings_fail_closed(value):
    with pytest.raises(SecretResolutionError):
        decrypt_enc_string(value, ENC_KEY, MAC_KEY)


def test_error_messages_never_contain_the_plaintext():
    value = encrypt(b"super-secret-plaintext")
    with pytest.raises(SecretResolutionError) as excinfo:
        decrypt_enc_string(value, ENC_KEY, b"\x00" * 32)
    assert "super-secret-plaintext" not in excinfo.value.message
