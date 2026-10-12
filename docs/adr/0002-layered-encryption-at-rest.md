# ADR 0002: Layered encryption at rest and master-key storage

- **Status:** Accepted (supersedes the token-encryption and key-file parts of ADR 0001)
- **Date:** 2026-10-12

## Context

ADR 0001 encrypted only Plaid access tokens (Fernet) and left everything else in a plaintext
SQLite file protected by file permissions. A copied database (backup, cloud sync, stray
`git add`) would still expose the whole transaction history, which is as sensitive as the
tokens. The key also lived in a file next to the data, and the tool has to work on macOS,
Linux and Windows on both x86-64 and arm64 without asking users to install system libraries.

## Decision

- **Whole-database encryption with SQLCipher** (`sqlcipher3`, prebuilt wheels for macOS,
  Linux glibc/musl and Windows on x86-64 and arm64; no system library). Pages, indexes and the
  WAL are encrypted. The key is passed as a raw 256-bit blob, so no password stretching and no
  quoting.
- **Field-level AES-256-GCM** (`cryptography`) on top, for Plaid access tokens and each raw
  Plaid payload. The ciphertext is bound to its column and row id as associated data, so values
  copied between rows fail to decrypt. Amounts, dates and categories stay ordinary columns
  (inside the encrypted file) so SQL filtering and aggregation keep working.
- **One master key, two derived keys.** 32 random bytes; HKDF-SHA256 derives the SQLCipher key
  and the field key under different labels, so neither can be used against the other.
- **Master key storage:** the OS keychain (macOS Keychain, Windows Credential Locker, Secret
  Service) via `keyring`, with an owner-only `master.key` file fallback where no keychain exists
  (`FINDATA_KEY_BACKEND=auto|keychain|file`). Keychain entries are per data directory.
- **A key is only created when no database exists.** If a database is present but its key cannot
  be found, findata stops with an error instead of minting a new key that would orphan the data.
- No cryptography is implemented by findata: ciphers, KDF and key storage all come from
  `cryptography`, SQLCipher and the OS.

## Consequences

- Databases created before this change are plaintext and are rejected with a clear error; they
  must be re-created (acceptable: Sandbox data only so far).
- Losing the master key makes all data unrecoverable. Back it up separately from the database.
- With the file fallback, the key and data sit on the same disk, which protects against a copied
  database but not against someone with full access to the user account. The keychain path
  improves on this; a passphrase-derived key is a possible later option for the file fallback.
- Field-level encryption is partly redundant with SQLCipher by design (defense in depth, and it
  keeps tokens protected in any future export or backup path).
- Windows has no POSIX permission bits; the permission checks are skipped there and the
  per-user profile ACL is relied on instead.
- Wheels exist per Python version and platform. CI runs on Linux (x86-64, arm64), macOS (arm64,
  x86-64) and Windows so a missing wheel fails the build. Linux ppc64le/s390x/armv7l have no
  `sqlcipher3` wheel.
