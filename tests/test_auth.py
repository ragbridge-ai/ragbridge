"""Tests for API key generation and hashing. No database needed."""

from ragbridge.auth import api_key_prefix, generate_api_key, hash_api_key


def test_generated_keys_are_unique() -> None:
    assert generate_api_key() != generate_api_key()


def test_generated_key_has_the_expected_prefix() -> None:
    assert generate_api_key().startswith("rb_")


def test_hash_api_key_is_deterministic() -> None:
    key = generate_api_key()
    assert hash_api_key(key) == hash_api_key(key)


def test_hash_api_key_differs_for_different_keys() -> None:
    assert hash_api_key(generate_api_key()) != hash_api_key(generate_api_key())


def test_hash_does_not_contain_the_key() -> None:
    key = generate_api_key()
    assert key not in hash_api_key(key)


def test_api_key_prefix_is_short_and_a_prefix_of_the_key() -> None:
    key = generate_api_key()
    prefix = api_key_prefix(key)
    assert key.startswith(prefix)
    assert len(prefix) < len(key)
