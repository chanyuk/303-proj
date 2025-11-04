import asyncio
import struct
import time
import random
from aioquic.asyncio import QuicConnectionProtocol
from aioquic.quic.events import DatagramFrameReceived, StreamDataReceived

# -----------------------------
# Constants
# -----------------------------
RETX_INTERVAL = 0.05     # 50 ms between retransmission attempts
T_THRESHOLD = 0.200      # 200 ms total lifetime for reliable retransmission

# -----------------------------
# Helpers
# -----------------------------
def now():
    return time.time()

def build_header(reliable: bool, seq: int, payload: bytes) -> bytes:
    ts = time.time()
    # Header = [bool, uint32 seq, float64 timestamp]
    return struct.pack("> ? I d", reliable, seq, ts) + payload

def parse_header(data: bytes):
    reliable, seq, ts = struct.unpack("> ? I d", data[:13])
    payload = data[13:]
    return reliable, seq, ts, payload

# -----------------------------
# GameNetAPI
# -----------------------------
class GameNetAPI:
    def __init__(self, quic_protocol):
        self.quic = quic_protocol
        self.next_seq = 1
        self.sent_buffer = {}        # seq -> {data, send_ts, acked, attempts}
        self.pending_reliable = {}   # receiver buffer for reordering
        self.last_delivered = 0
        self.receive_callback = None

    def set_receive_callback(self, cb):
        self.receive_callback = cb

    # -----------------------------
    # Sending
    # -----------------------------
    async def send_data(self, payload: bytes, reliable: bool):
        seq = self.next_seq
        self.next_seq += 1
        packet = build_header(reliable, seq, payload)

        if reliable:
            self.sent_buffer[seq] = {
                "data": packet,
                "send_ts": now(),
                "acked": False,
                "attempts": 0
            }
            asyncio.create_task(self._send_reliable(seq))
        else:
            # Unreliable uses QUIC datagram
            self.quic._quic.send_datagram_frame(packet)
        return seq

    async def _send_reliable(self, seq):
        entry = self.sent_buffer[seq]
        while not entry["acked"]:
            self.quic._quic.send_stream_data(0, entry["data"], end_stream=False)
            entry["attempts"] += 1
            await asyncio.sleep(RETX_INTERVAL)
            if now() - entry["send_ts"] >= T_THRESHOLD:
                print(f"[SENDER] Give up seq {seq}")
                del self.sent_buffer[seq]
                break

    def mark_acked(self, seq):
        if seq in self.sent_buffer:
            self.sent_buffer[seq]["acked"] = True
            print(f"[SENDER] ACK received for seq {seq}")

    # -----------------------------
    # Receiving
    # -----------------------------
    def receive_datagram(self, data: bytes):
        reliable, seq, ts, payload = parse_header(data)
        rtt = now() - ts

        if reliable:
            # Acknowledge back
            ack = f"ACK:{seq}".encode()
            self.quic._quic.send_datagram_frame(ack)
            self.pending_reliable[seq] = (payload, now())
            self._try_deliver_in_order()
        else:
            # Deliver immediately (unreliable)
            if self.receive_callback:
                self.receive_callback(seq, reliable, ts, payload, rtt)

    def _try_deliver_in_order(self):
        expected = self.last_delivered + 1
        while expected in self.pending_reliable:
            payload, arrival = self.pending_reliable.pop(expected)
            rtt = now() - arrival
            if self.receive_callback:
                self.receive_callback(expected, True, arrival, payload, rtt)
            self.last_delivered = expected
            expected += 1

        # Handle timeout skip
        for seq, (payload, arrival) in list(self.pending_reliable.items()):
            if now() - arrival >= T_THRESHOLD:
                print(f"[RECEIVER] Skipping lost seq {seq}")
                del self.pending_reliable[seq]
                self.last_delivered = seq

# -----------------------------
# GameQuicProtocol
# -----------------------------
class GameQuicProtocol(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = GameNetAPI(self)

    def set_receive_callback(self, cb):
        self.api.set_receive_callback(cb)

    def quic_event_received(self, event):
        if isinstance(event, DatagramFrameReceived):
            data = event.data
            if data.startswith(b"ACK:"):
                seq = int(data[4:].decode())
                self.api.mark_acked(seq)
            else:
                self.api.receive_datagram(data)
        elif isinstance(event, StreamDataReceived):
            data = event.data
            self.api.receive_datagram(data)
