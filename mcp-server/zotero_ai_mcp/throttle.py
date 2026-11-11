"""Per-address rate limits for the sign-in, dynamic registration and consent endpoints, so one
address on the internet can't use up a limit for everyone.
"""
import ipaddress
import threading
import time

MAX_KEYS = 10_000  # addresses tracked at once; the least recently seen go first


def client_address(h):
    """Where a request comes from: the TCP peer, or, when the peer is loopback (the reverse proxy),
    the last X-Forwarded-For entry (the one that proxy added; earlier ones are whatever the client
    sent). IPv6 addresses count per /64, since one host usually has a whole /64."""
    peer = h.client_address[0]
    try:
        address = ipaddress.ip_address(peer.split("%")[0])
        forwarded = (h.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
        if address.is_loopback and forwarded:
            address = ipaddress.ip_address(forwarded.strip("[]"))
    except ValueError:
        return peer
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


class Throttle:
    """At most `limit` events per key in any `window` seconds."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.events = {}  # key → times, the most recently used key last
        self.lock = threading.Lock()

    def blocked(self, key):
        with self.lock:
            return len(self._recent(key, time.time())) >= self.limit

    def add(self, key):
        with self.lock:
            self._add(key, time.time())

    def take(self, key):
        """Counts an event unless the key is at its limit. → whether it was allowed."""
        with self.lock:
            now = time.time()
            if len(self._recent(key, now)) >= self.limit:
                return False
            self._add(key, now)
            return True

    def refund(self, key):
        """Takes back the latest event counted for `key` (an attempt that turned out fine). It may not
        be the caller's own event when attempts overlap, but the count comes out the same."""
        with self.lock:
            times = self.events.get(key)
            if times:
                times.pop()

    def _recent(self, key, now):
        times = [t for t in self.events.get(key, ()) if now - t < self.window]
        if times:
            self.events[key] = times
        else:
            self.events.pop(key, None)
        return times

    def _add(self, key, now):
        times = self.events.pop(key, [])
        self.events[key] = [t for t in times if now - t < self.window] + [now]
        while len(self.events) > MAX_KEYS:
            del self.events[next(iter(self.events))]
