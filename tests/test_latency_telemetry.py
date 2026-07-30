from __future__ import annotations

import unittest

from app.agent.models import Outcome, RouteSource
from app.telemetry import TurnTrace

from tests.fakes import FakeClock


class TurnTraceTests(unittest.TestCase):
    def test_fake_clock_records_required_identity_route_and_component_durations(self) -> None:
        clock = FakeClock(seconds=100.0)
        trace = TurnTrace.start(
            clock=clock,
            trace_id="trace-fixed",
            request_id="req-trace",
            catalog_version="v42",
            cache_hit=True,
        )

        with trace.component("state_read"):
            clock.advance_ms(1)
        with trace.component("retrieval"):
            clock.advance_ms(4)
        with trace.component("agent"):
            clock.advance_ms(2)
        with trace.component("state_write"):
            clock.advance_ms(3)
        summary = trace.finish(
            route=RouteSource.DETERMINISTIC,
            outcome=Outcome.HANDLED,
            gigachat_calls=0,
        )

        self.assertEqual(summary.trace_id, "trace-fixed")
        self.assertEqual(summary.request_id, "req-trace")
        self.assertEqual(summary.route, RouteSource.DETERMINISTIC)
        self.assertEqual(summary.outcome, Outcome.HANDLED)
        self.assertEqual(summary.catalog_version, "v42")
        self.assertTrue(summary.cache_hit)
        self.assertEqual(summary.gigachat_calls, 0)
        self.assertAlmostEqual(summary.durations_ms["total"], 10.0)
        self.assertAlmostEqual(summary.durations_ms["state_read"], 1.0)
        self.assertAlmostEqual(summary.durations_ms["retrieval"], 4.0)
        self.assertAlmostEqual(summary.durations_ms["agent"], 2.0)
        self.assertAlmostEqual(summary.durations_ms["gigachat"], 0.0)
        self.assertAlmostEqual(summary.durations_ms["state_write"], 3.0)

    def test_gigachat_component_is_separately_visible(self) -> None:
        clock = FakeClock()
        trace = TurnTrace.start(
            clock=clock,
            trace_id="trace-fallback",
            request_id="req-fallback",
            catalog_version="v42",
            cache_hit=True,
        )

        with trace.component("gigachat"):
            clock.advance_ms(125)
        summary = trace.finish(
            route=RouteSource.GIGACHAT_FALLBACK,
            outcome=Outcome.PROVIDER_FAILURE,
            gigachat_calls=1,
        )

        self.assertAlmostEqual(summary.durations_ms["gigachat"], 125.0)
        self.assertAlmostEqual(summary.durations_ms["total"], 125.0)
        self.assertEqual(summary.gigachat_calls, 1)


if __name__ == "__main__":
    unittest.main()
