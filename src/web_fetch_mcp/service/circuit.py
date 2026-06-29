"""Domain-level circuit breaker.

Tracks consecutive fetch failures per root domain. After a threshold is
crossed, immediately rejects further requests to that domain (circuit open)
until a cooldown elapses (half-open probe). Subdomains roll up to the root
domain (e.g. ``docs.example.com`` → ``example.com``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from web_fetch_mcp.core.config import CIRCUIT_FAIL_MAX, CIRCUIT_RESET_TIMEOUT
from web_fetch_mcp.core.models import FetchBlocked


class CircuitState(Enum):
    """The three states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass
class _DomainCircuit:
    """Internal mutable state for one domain's circuit."""

    failure_count: int = 0
    last_failure_time: float = 0.0
    last_reason: str = ""
    state: CircuitState = field(default=CircuitState.CLOSED)


class DomainCircuitRegistry:
    """Per-root-domain circuit breaker registry.

    Manages independent circuit states for each root domain. A root domain is
    the registrable domain (last two labels, or three for two-part TLDs like
    ``.co.uk``). Subdomains share their root's circuit — blocking on
    ``api.example.com`` also blocks ``www.example.com``.

    Typical usage (wired into ``fetch_url``)::

        registry.check(url)          # raises FetchBlocked if circuit is open
        try:
            result = await do_fetch(url)
        except FetchBlocked:
            registry.record_failure(url, reason="blocked")
            raise
        registry.record_success(url)

    Args:
        fail_max: Consecutive failures before the circuit opens.
        reset_timeout: Seconds before a half-open probe is allowed.
    """

    def __init__(
        self,
        fail_max: int = CIRCUIT_FAIL_MAX,
        reset_timeout: float = CIRCUIT_RESET_TIMEOUT,
    ) -> None:
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self._circuits: dict[str, _DomainCircuit] = {}

    @staticmethod
    def extract_root_domain(url: str) -> str:
        """Extract the registrable root domain from a URL.

        Subdomains roll up to the root: ``docs.example.com`` → ``example.com``.
        Two-part TLDs (``co.uk``, ``com.au``) are handled by taking the last
        three labels when the second-to-last label is short (<=3 chars) and the
        TLD is short (<=2 chars).

        Args:
            url: A fully-qualified URL (e.g. ``https://docs.example.co.uk/path``).

        Returns:
            The root domain string (e.g. ``"example.co.uk"``).
        """
        hostname = urlparse(url).hostname or ""
        parts = hostname.split(".")
        if len(parts) >= 3 and len(parts[-2]) <= 3 and len(parts[-1]) <= 2:
            return ".".join(parts[-3:])
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return hostname

    def _get_circuit(self, url: str) -> tuple[str, _DomainCircuit]:
        """Get or lazily create the circuit for a URL's root domain."""
        domain = self.extract_root_domain(url)
        if domain not in self._circuits:
            self._circuits[domain] = _DomainCircuit()
        return domain, self._circuits[domain]

    def _is_timeout_elapsed(self, circuit: _DomainCircuit) -> bool:
        """Whether enough time has passed since the last failure to allow a probe."""
        return (time.monotonic() - circuit.last_failure_time) >= self.reset_timeout

    def _remaining_seconds(self, circuit: _DomainCircuit) -> float:
        """Seconds remaining until the circuit transitions to half-open."""
        return max(0.0, self.reset_timeout - (time.monotonic() - circuit.last_failure_time))

    def check(self, url: str) -> None:
        """Verify the circuit allows a request to this URL's domain.

        Must be called before attempting a fetch. If the circuit is open and the
        cooldown hasn't elapsed, raises ``FetchBlocked`` immediately (no fetch is
        attempted, no browser launched). If the cooldown has elapsed, transitions
        to half-open and allows one probe request through.

        Args:
            url: The fully-qualified target URL.

        Raises:
            FetchBlocked: When the circuit is open. The message includes the
                domain name, failure count, seconds until the next probe is
                allowed, the last recorded failure reason, and guidance
                (residential proxy / manual CAPTCHA).
        """
        domain, circuit = self._get_circuit(url)

        if circuit.state == CircuitState.CLOSED:
            return

        if circuit.state == CircuitState.OPEN:
            if self._is_timeout_elapsed(circuit):
                circuit.state = CircuitState.HALF_OPEN
                return
            raise FetchBlocked(
                f"circuit breaker open for {domain}: "
                f"{circuit.failure_count} consecutive failures. "
                f"Retrying in {self._remaining_seconds(circuit):.0f}s. "
                f"Last reason: {circuit.last_reason or 'blocked'}. "
                f"Consider a residential proxy or manual CAPTCHA resolution."
            )

        # HALF_OPEN: allow the single probe request through.

    def record_failure(self, url: str, reason: str = "") -> None:
        """Record a fetch failure for this URL's root domain.

        Increments the failure counter. Opens the circuit if the counter reaches
        ``fail_max``, or re-opens it if the state was half-open (probe failed).

        Args:
            url: The URL that failed.
            reason: Short description of why it failed (e.g. ``"captcha"``).
                Stored and surfaced in future ``FetchBlocked`` messages.
        """
        _, circuit = self._get_circuit(url)
        circuit.failure_count += 1
        circuit.last_failure_time = time.monotonic()
        circuit.last_reason = reason

        if circuit.state == CircuitState.HALF_OPEN:
            circuit.state = CircuitState.OPEN
        elif circuit.failure_count >= self.fail_max:
            circuit.state = CircuitState.OPEN

    def record_success(self, url: str) -> None:
        """Record a successful fetch, closing the circuit and resetting failures.

        Args:
            url: The URL that succeeded.
        """
        _, circuit = self._get_circuit(url)
        circuit.failure_count = 0
        circuit.last_reason = ""
        circuit.state = CircuitState.CLOSED

    def get_state(self, url: str) -> str:
        """Return the current circuit state for a URL's root domain.

        Args:
            url: Any URL on the domain.

        Returns:
            One of ``"closed"``, ``"open"``, or ``"half-open"``.
        """
        _, circuit = self._get_circuit(url)
        return circuit.state.value

    def reset(self, url: str) -> None:
        """Manually close the circuit for a domain.

        Use after a human has solved a CAPTCHA or the block is known to have
        cleared. Resets the failure counter and closes the circuit immediately.

        Args:
            url: Any URL on the domain to reset.
        """
        _, circuit = self._get_circuit(url)
        circuit.failure_count = 0
        circuit.last_reason = ""
        circuit.state = CircuitState.CLOSED


# Module-level singleton used by fetch_url.
domain_circuits = DomainCircuitRegistry()
