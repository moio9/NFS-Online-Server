"""Legacy Classic JSON account storage for explicitly non-SQLite setups."""


def open_legacy_store(path, *, failure_limit, lockout_seconds):
    # The old JSON format belongs to Classic. Keep this dependency confined to
    # the compatibility path; shared SQLite administration needs no game code.
    from classic.accounts.credentials import CredentialStore
    return CredentialStore(path, failure_limit=failure_limit,
                           lockout_seconds=lockout_seconds)
