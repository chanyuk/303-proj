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
            packet = build_header(reliable, seq, payload)
            self.quic._quic.send_datagram_frame(packet)
            self.quic.transmit()
        return seq
                
    async def _send_reliable(self, seq):
        entry = self.sent_buffer[seq]
        
        while not entry["acked"]:
            # if not first attempt, check threshold timeout and rebuild header with new timestamp
            if entry["attempts"] > 0:
                if now() - entry["send_ts"] >= T_THRESHOLD:
                    print(f"[SENDER] Give up seq {seq}")
                    if seq in self.sent_buffer:
                        del self.sent_buffer[seq]
                    break
    
                # rebuild header so that there is new timestamp
                reliable, _, _ = True, seq, None
                payload = entry["data"][13:]   # strip old header
                packet = build_header(reliable, seq, payload)
                entry["data"] = packet
            
            self.quic._quic.send_datagram_frame(entry["data"])
            self.quic.transmit()
            
            entry["attempts"] += 1
            await asyncio.sleep(RETX_INTERVAL)

    def mark_acked(self, seq):
        entry = self.sent_buffer.get(seq)
        if entry:
            entry["acked"] = True
            print(f"[SENDER] ACK received for seq {seq}")
        else:
            print(f"[SENDER] Received ACK for already-removed seq {seq} (timeout or duplicate)")

    # -----------------------------
    # Receiving
    # -----------------------------
    def receive_datagram(self, data: bytes):
        reliable, seq, ts, payload = parse_header(data)
        rtt = now() - ts
        print("debug: ", data[:20])

        if reliable:
            # Acknowledge back
            ack = f"ACK:{seq}".encode()
            self.quic._quic.send_datagram_frame(ack)
            self.quic.transmit()
            if seq <= self.last_delivered:
                return
            if seq in self.pending_reliable:
                return
            self.pending_reliable[seq] = (payload, ts, now())
            self._try_deliver_in_order()
        else:
            # Deliver immediately (unreliable)
            if self.receive_callback:
                self.receive_callback(seq, reliable, ts, payload, rtt)
    
    def _try_deliver_in_order(self):
        self._cleanup_pending()

        delivered_count = 0
        skipped_count = 0

        while True:
            expected = self.last_delivered + 1

            # Case 1: Expected packet is available
            if expected in self.pending_reliable:
                payload, send_ts, arrival_ts = self.pending_reliable.pop(expected)
                rtt = arrival_ts - send_ts
                
                if self.receive_callback:
                    try:
                        self.receive_callback(expected, True, send_ts, payload, rtt)
                    except Exception as e:
                        print(f"[RECEIVER] Callback error for seq {expected}: {e}")
                
                self.last_delivered = expected
                delivered_count += 1
                self._cleanup_pending()
                continue

            # Case 2: Expected packet missing - check timeout
            if self.pending_reliable:
                higher_seqs = [s for s in self.pending_reliable if s > expected]
                # only skip if we have packets AFTER the expected one
                if higher_seqs:
                    # Check how long we've been waiting for the expected packet
                    # Use the arrival time of the earliest packet we DO have
                    oldest_arrival = min(arr for (_, _, arr) in self.pending_reliable.values())
                    wait_time = now() - oldest_arrival
                    
                    if wait_time >= T_THRESHOLD:
                        print(f"[RECEIVER] Skipping lost seq {expected} (waited {wait_time:.3f}s, have seq {min(higher_seqs)}+)")
                        self.last_delivered = expected
                        skipped_count += 1
                        self._cleanup_pending()
                        continue

            # Case 3: Nothing more to deliver
            break
        
        if delivered_count > 0 or skipped_count > 0:
            print(f"[RECEIVER] Delivered {delivered_count}, skipped {skipped_count} packets")

    def _cleanup_pending(self):
        if not self.pending_reliable:
            return
        stale = [seq for seq in self.pending_reliable if seq <= self.last_delivered]
        for seq in stale:
            self.pending_reliable.pop(seq, None)

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

