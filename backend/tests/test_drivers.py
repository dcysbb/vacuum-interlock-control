import asyncio
import struct
import unittest

from app.drivers.adam6050 import Adam6050Client
from app.drivers.tpg366 import TPG366Client


class DriverProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        for server in getattr(self, "servers", []):
            server.close()
            await server.wait_closed()

    async def start_server(self, handler):
        if not hasattr(self, "servers"):
            self.servers = []
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def test_tpg_reads_six_status_value_pairs_and_converts_pa(self):
        values = {
            "UNI": "2",
            "PRX": "0,1.25,0,0.5,2,9.0,3,0,4,0,5,0",
        }

        async def handle(reader, writer):
            try:
                while True:
                    command = (await reader.readuntil(b"\r\n")).decode().strip()
                    writer.write(b"\x06\r\n")
                    await writer.drain()
                    await reader.readexactly(1)
                    writer.write(values[command].encode() + b"\r\n")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        port = await self.start_server(handle)
        client = TPG366Client("127.0.0.1", port)
        try:
            readings = await client.read_channels()
        finally:
            await client.close()

        self.assertEqual(len(readings), 6)
        self.assertEqual(readings[0].pressure_pa, 1.25)
        self.assertTrue(readings[0].valid)
        self.assertFalse(readings[2].valid)
        self.assertIsNone(readings[2].pressure_pa)

    async def test_adam_reads_input_output_and_verifies_coil_write(self):
        async def handle(reader, writer):
            try:
                while True:
                    header = await reader.readexactly(7)
                    tid, protocol, length, unit = struct.unpack(">HHHB", header)
                    request = await reader.readexactly(length - 1)
                    function = request[0]
                    if function == 2:
                        payload = b"\x02\x02\x01\x08"
                    elif function == 1:
                        payload = b"\x01\x01\x05"
                    else:
                        payload = request
                    frame = struct.pack(">HHHB", tid, protocol, len(payload) + 1, unit) + payload
                    writer.write(frame)
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        port = await self.start_server(handle)
        client = Adam6050Client("127.0.0.1", port)
        try:
            inputs = await client.read_inputs()
            outputs = await client.read_outputs()
            await client.write_output(2, True)
        finally:
            await client.close()

        self.assertTrue(inputs[0])
        self.assertTrue(inputs[11])
        self.assertTrue(outputs[0])
        self.assertTrue(outputs[2])
        self.assertFalse(outputs[1])


if __name__ == "__main__":
    unittest.main()
