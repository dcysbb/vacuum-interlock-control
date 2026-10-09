import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from app.configuration import ConfigurationStore
from app.engine import InterlockEngine, InterlockError
from app.models import AppConfig, ChannelConfig
from app.storage import HistoryStore


class InterlockEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = ConfigurationStore(root)
        self.history = HistoryStore(root / "history.sqlite3")
        config = AppConfig(recovery_hold_s=0.05)
        self.settings.update(config)
        self.engine = InterlockEngine(self.settings, self.history)
        await self.engine._poll_tpg()

    async def asyncTearDown(self):
        await self.engine.stop()
        self.history.close()
        self.temp.cleanup()

    async def test_pressure_over_limit_latches_and_closes(self):
        await self.engine.arm("test")
        await self.engine.open_valve("test")
        await asyncio.sleep(0.5)
        await self.engine.inject_pressure(1, 0.02)
        await self.engine._poll_tpg()
        fault = self.engine._pressure_fault()

        self.assertIn("关阀阈值", fault)
        await self.engine.trip(fault)
        self.assertEqual(self.engine.interlock, "tripped")
        self.assertFalse(self.engine._open_output)
        self.assertEqual(self.engine.snapshot()["valve"], "closing")
        await asyncio.sleep(0.5)
        await self.engine._poll_tpg()
        self.assertEqual(self.engine.snapshot()["valve"], "closed")

    async def test_invalid_gauge_reading_trips_in_running_loop(self):
        await self.engine.start()
        await self.engine.arm("test")
        self.engine._sim_gauge_status[0] = 3

        for _ in range(20):
            if self.engine.interlock == "tripped":
                break
            await asyncio.sleep(0.05)

        self.assertEqual(self.engine.interlock, "tripped")
        self.assertIn("测量异常", self.engine.trip_reason)

    async def test_disarm_closes_valve_and_does_not_remain_armed(self):
        await self.engine.arm("test")
        await self.engine.open_valve("test")
        await asyncio.sleep(0.5)
        await self.engine._poll_tpg()
        await self.engine.disarm("test")

        self.assertEqual(self.engine.interlock, "disarmed")
        self.assertFalse(self.engine._open_output)
        with self.assertRaisesRegex(InterlockError, "先启用"):
            await self.engine.open_valve("test")

    async def test_manual_reset_requires_recovered_pressure_and_keeps_valve_closed(self):
        await self.engine.arm("test")
        await self.engine.inject_pressure(1, 0.02)
        await self.engine._poll_tpg()
        await self.engine.trip(self.engine._pressure_fault())
        with self.assertRaisesRegex(InterlockError, "关阀阈值"):
            await self.engine.reset("test")

        await self.engine.inject_pressure(1, 0.001)
        await self.engine._poll_tpg()
        self.engine._recovery_since = time.monotonic() - 0.1
        await self.engine.reset("test")

        self.assertEqual(self.engine.interlock, "armed")
        self.assertEqual(self.engine.snapshot()["valve"], "closed")
        events = self.history.list_events(None, None, 20)
        self.assertTrue(any(event["type"] == "interlock_reset" for event in events))

    async def test_reset_refuses_stale_pressure_data(self):
        await self.engine.arm("test")
        await self.engine.trip("test trip")
        self.engine._last_tpg_read = time.monotonic() - 10

        with self.assertRaisesRegex(InterlockError, "读数已超时"):
            await self.engine.reset("test")


class ConfigurationTests(unittest.TestCase):
    def test_enabled_channel_requires_valid_hysteresis(self):
        with self.assertRaises(ValueError):
            ChannelConfig(enabled=True, trip_pa=0.01, reset_pa=0.01)
        config = AppConfig(
            channels=[ChannelConfig(enabled=True, trip_pa=0.01, reset_pa=0.005)]
            + [ChannelConfig() for _ in range(5)]
        )
        self.assertTrue(config.channels[0].enabled)

    def test_permissive_inputs_are_inactive_by_default(self):
        config = AppConfig()
        self.assertTrue(config.permit_ok_active_high)
        self.assertTrue(config.watchdog_ok_active_high)
        self.assertFalse(config.fail_closed_confirmed)


if __name__ == "__main__":
    unittest.main()
