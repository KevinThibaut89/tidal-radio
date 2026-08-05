"""Password protection for the control UI and API.

Optional by design: with no password set the station behaves exactly as it
always has, so upgrading can never lock someone out of their own container.
Once one is set, everything but the login handshake needs a bearer token.

The password itself is never stored — only a salted scrypt hash, kept in the
same settings store as the API keys and stripped from anything the API returns.
Session tokens live in memory only, so a restart signs every browser out.
"""
import hashlib
import hmac
import logging
import secrets
import threading
import time

log = logging.getLogger(__name__)

PASSWORD_KEY = "ui_password_hash"   # settings key; the API must never echo it
SESSION_TTL = 30 * 24 * 3600        # a home station shouldn't ask every day
MAX_FAILURES = 5
LOCKOUT_SECONDS = 30
MIN_LENGTH = 6

# ~100 ms and 16 MB per attempt on the container's CPU: expensive enough that a
# stolen hash is not worth cracking, cheap enough that a login feels instant.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 1 << 14, 8, 1


class Auth:
    """Owns the password hash and the in-memory session tokens."""

    def __init__(self, settings):
        self.settings = settings
        self._tokens: dict[str, float] = {}   # token -> expiry
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._locked: dict[str, float] = {}

    # ── password ─────────────────────────────────────────────────────────
    def is_enabled(self) -> bool:
        return bool(self.settings.get(PASSWORD_KEY))

    def set_password(self, password: str) -> None:
        """Store a new password, or remove protection when given an empty one.

        Every existing session is dropped, because changing the password is
        also how you lock out a device you no longer trust.
        """
        if password:
            salt = secrets.token_bytes(16)
            digest = self._derive(password, salt)
            self.settings.set_many({PASSWORD_KEY: "$".join(
                ["scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
                 salt.hex(), digest.hex()])})
        else:
            self.settings.set_many({PASSWORD_KEY: None})
        with self._lock:
            self._tokens.clear()
        log.info("Control UI password %s", "set" if password else "removed")

    def verify_password(self, password: str) -> bool:
        stored = self.settings.get(PASSWORD_KEY)
        if not stored or not password:
            return False
        try:
            algo, n, r, p, salt, digest = str(stored).split("$")
            if algo != "scrypt":
                raise ValueError(f"unknown hash algorithm: {algo}")
            expected = bytes.fromhex(digest)
            candidate = self._derive(password, bytes.fromhex(salt),
                                     int(n), int(r), int(p))
        except ValueError as e:
            log.warning("Stored password hash is unusable (%s) — refusing login", e)
            return False
        return hmac.compare_digest(candidate, expected)

    @staticmethod
    def _derive(password: str, salt: bytes, n: int = SCRYPT_N,
                r: int = SCRYPT_R, p: int = SCRYPT_P) -> bytes:
        return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)

    # ── session tokens ───────────────────────────────────────────────────
    def issue_token(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            self._tokens[token] = time.time() + SESSION_TTL
            self._failures.clear()        # a good login clears the bad ones
            self._locked.clear()
        return token

    def verify_token(self, token: str) -> bool:
        if not token or not token.isascii():
            return False
        with self._lock:
            self._prune()
            # Compare against every live token without short-circuiting, so the
            # timing can't leak how much of a guessed token was right.
            match = False
            for known in self._tokens:
                match |= hmac.compare_digest(known, token)
            return match

    def revoke_token(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def _prune(self) -> None:
        now = time.time()
        for token in [t for t, expiry in self._tokens.items() if expiry <= now]:
            del self._tokens[token]

    # ── brute-force pacing ───────────────────────────────────────────────
    def locked_for(self, who: str = "*") -> float:
        """Seconds until this caller's attempts are accepted again; 0 when open.

        Keyed per caller: a single global counter would let anyone on the
        network lock the owner out of their own station with five bad guesses.
        """
        with self._lock:
            self._prune_attempts()
            return max(0.0, self._locked.get(who, 0.0) - time.time())

    def note_failure(self, who: str = "*") -> None:
        with self._lock:
            self._prune_attempts()
            n = self._failures.get(who, 0) + 1
            self._failures[who] = n
            if n >= MAX_FAILURES:
                self._failures.pop(who, None)
                self._locked[who] = time.time() + LOCKOUT_SECONDS
                log.warning("%d failed logins from %s — refusing for %ds",
                            MAX_FAILURES, who, LOCKOUT_SECONDS)

    def note_success(self, who: str = "*") -> None:
        with self._lock:
            self._failures.pop(who, None)
            self._locked.pop(who, None)

    def _prune_attempts(self) -> None:
        """Keep the per-caller maps bounded; callers come and go."""
        now = time.time()
        for key, until in list(self._locked.items()):
            if until <= now:
                self._locked.pop(key, None)
        if len(self._failures) > 512:
            self._failures.clear()
