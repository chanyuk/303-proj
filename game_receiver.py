import asyncio
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
            "reliable": {"rtts": [], "count": 0},
            "unreliable": {"rtts": [], "count": 0},
        }

    def add_packet(self, seq, reliable, ts, payload, rtt):
        channel = "reliable" if reliable else "unreliable"
        self.data[channel]["rtts"].append(rtt)
        self.data[channel]["count"] += 1
        # Optional: print per-packet log
        print(f"SeqNo: {seq}, Channel: {channel}, RTT: {rtt:.3f}s, Payload: {payload}")

    def print_stats(self):
        print("================== FINAL STATS ==================")
        self.end_ts = time.time()
        duration = self.end_ts - self.start_ts
        for ch in ["reliable", "unreliable"]:
            rtts = self.data[ch]["rtts"]
            count = self.data[ch]["count"]
            avg_rtt = sum(rtts)/len(rtts) if rtts else 0
            print(f"[RECEIVER STATS] {ch.capitalize()} - Packets received: {count}, Avg RTT: {avg_rtt:.3f}s, Duration: {duration:.3f}s")

# -----------------------------
# Receiver callback
# -----------------------------
stats = ReceiverStats()

def receiver_callback(seq, reliable, ts, payload, rtt):
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
