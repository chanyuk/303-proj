import asyncio
import json
import time
from hquic import GameQuicProtocol  # your current QUIC wrapper
from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration

# -----------------------------
# Receiver stats
# -----------------------------
class ReceiverStats:
    def __init__(self):
        self.start_ts = time.time()
        self.end_ts = None
        self.data = {
            "reliable": {"rtts": [], "count": 0, "bytes_received": 0},
            "unreliable": {"rtts": [], "count": 0, "bytes_received": 0},
        }
        self.sender_stats = None

    def add_packet(self, seq, reliable, ts, payload, rtt):
        self.end_ts = time.time()
        channel = "reliable" if reliable else "unreliable"
        self.data[channel]["rtts"].append(rtt)
        self.data[channel]["count"] += 1
        self.data[channel]["bytes_received"] += len(payload)
        # print per-packet log
        print(f"SeqNo: {seq}, Channel: {channel}, RTT: {rtt:.3f}s, Payload: {payload}")

    
    def set_sender_stats(self, sent_reliable, sent_unreliable):
        self.sender_stats = {"reliable": sent_reliable, "unreliable": sent_unreliable}

    def avg_rtt(self, channel):
        rtts = self.data[channel]["rtts"]
        return sum(rtts) / len(rtts) if rtts else 0

    def calculate_jitter_rfc3550(self, channel):
        rtts = self.data[channel]["rtts"]
        if len(rtts) < 2:
            return 0
        jitter = 0
        for i in range(1, len(rtts)):
            diff = abs(rtts[i] - rtts[i-1])
            jitter = jitter + (diff - jitter) / 16
        return jitter

    def calculate_throughput_kbps(self, channel):
        if self.end_ts is None:
                return 0

        duration = self.end_ts - self.start_ts
        if duration <= 0:
            return 0
        bytes_received = self.data[channel]["bytes_received"]
        return (bytes_received * 8) / (duration * 1000)  # Kbps

    def calculate_delivery_ratio(self, channel):
        if self.sender_stats is None:
            return None
        packets_received = self.data[channel]["count"]
        packets_sent = self.sender_stats.get(channel, 0)
        if packets_sent == 0:
            return 0
        return (packets_received / packets_sent) * 100

    def print_stats(self):
        print("================== FINAL STATS ==================")
        if self.end_ts is None:
            self.end_ts = time.time()
        duration = self.end_ts - self.start_ts
        for ch in ["reliable", "unreliable"]:
            count = self.data[ch]["count"]
            avg_rtt = self.avg_rtt(ch)
            jitter = self.calculate_jitter_rfc3550(ch)
            throughput = self.calculate_throughput_kbps(ch)
            delivery_ratio = self.calculate_delivery_ratio(ch)
            delivery_str = f"{delivery_ratio:.1f}%" if delivery_ratio is not None else "N/A"
            
            print(f"[RECEIVER STATS] {ch.capitalize():10s} - "
                  f"Packets: {count:3d}, "
                  f"Latency: {avg_rtt*1000:6.2f}ms, "
                  f"Jitter: {jitter*1000:5.2f}ms, "
                  f"Throughput: {throughput:6.2f}Kbps, "
                  f"Delivery: {delivery_str}")
        
# -----------------------------
# Receiver callback
# -----------------------------
stats = ReceiverStats()

def receiver_callback(seq, reliable, ts, payload, rtt):
    if reliable:
        try:
            obj = json.loads(payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            obj = None
        if isinstance(obj, dict) and obj.get("type") == "STATS":
            stats.set_sender_stats(
                obj.get("sent_reliable", 0),
                obj.get("sent_unreliable", 0)
            )
            total = obj.get("total_sent")
            print(f"[RECEIVER] Sender summary received: {total} packets sent")
            return
    stats.add_packet(seq, reliable, ts, payload, rtt)

# -----------------------------
# QUIC server setup
# -----------------------------
from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration

async def run_receiver():
    cfg = QuicConfiguration(is_client=False)
    cfg.max_datagram_frame_size = 65536
    cfg.load_cert_chain("cert.pem", "key.pem")  # self-signed or generated cert
    
    # Set callback for stats collection
    def connection_made_cb(*args, **kwargs):
        protocol = GameQuicProtocol(*args, **kwargs)
        protocol.set_receive_callback(receiver_callback)
        return protocol
    
    try: 
        server = await serve(
            host="127.0.0.1",
            port=4433,
            configuration=cfg,
            create_protocol=connection_made_cb
        )
    except Exception as e:
        print("Server failed: ", e)
        return

    

    # Wait until interrupted
    print("[RECEIVER] Listening on 127.0.0.1:4433...")
    try:
        await asyncio.sleep(60)
    except KeyboardInterrupt:
        print("[RECEIVER] Shutting down...")
    finally:
        stats.print_stats()
        server.close()

try: 
    asyncio.run(run_receiver())
except Exception as e:
    print("RUN FAIL: ", e)
