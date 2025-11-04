# receiver.py
import asyncio
import json
import struct
import time
import argparse
import math
from aioquic.asyncio import serve
from aioquic.quic.configuration import QuicConfiguration
from hquic import GameQuicProtocol

# RFC 3550 interarrival jitter computation per stream/channel
class JitterEstimator:
    def __init__(self):
        self.J = 0.0
        self.prev_sender_ts = None
        self.prev_arrival_ts = None

    def update(self, sender_ts, arrival_ts):
        if self.prev_sender_ts is None:
            self.prev_sender_ts = sender_ts
            self.prev_arrival_ts = arrival_ts
            return self.J

        # D = (R_i - R_{i-1}) - (S_i - S_{i-1})
        D = (arrival_ts - self.prev_arrival_ts) - (sender_ts - self.prev_sender_ts)
        D = abs(D)
        # J = J + (|D| - J)/16
        self.J += (D - self.J) / 16.0

        self.prev_sender_ts = sender_ts
        self.prev_arrival_ts = arrival_ts
        return self.J

class ChannelStats:
    def __init__(self, name):
        self.name = name
        self.recv_count = 0
        self.total_bytes = 0
        self.first_arrival = None
        self.last_arrival = None
        self.jitter_est = JitterEstimator()
        self.rtts = []  # RTT samples
        self.seen_seqs = set()

    def record(self, seq, sender_ts, payload_len, arrival_ts, rtt):
        self.recv_count += 1
        self.total_bytes += payload_len
        if self.first_arrival is None:
            self.first_arrival = arrival_ts
        self.last_arrival = arrival_ts
        self.rtts.append(rtt)
        self.seen_seqs.add(seq)
        j = self.jitter_est.update(sender_ts, arrival_ts)
        return j

class ReceiverApp:
    def __init__(self):
        self.channels = {
            "reliable": ChannelStats("reliable"),
            "unreliable": ChannelStats("unreliable")
        }
        self.total_received = 0
        self.api_protocol = None
        self.receive_log = []

    def set_protocol(self, proto):
        self.api_protocol = proto
        proto.set_receive_callback(self.on_receive)

    def on_receive(self, seq, reliable, sender_ts, payload, rtt):
        arrival_ts = time.time()
        chan = "reliable" if reliable else "unreliable"
        cs = self.channels[chan]
        payload_len = len(payload)

        # detect special STATS message
        try:
            if payload.startswith(b"{"):
                decoded = json.loads(payload.decode())
                if isinstance(decoded, dict) and decoded.get("type") == "STATS":
                    # sender final stats
                    print("[RECEIVER] Received STATS from sender:", decoded)
                    self._report_final(decoded)
                    return
        except Exception:
            pass

        # record stats and compute jitter
        j = cs.record(seq, sender_ts, payload_len, arrival_ts, rtt)
        self.total_received += 1

        # Log detail line
        log_line = (f"[RECV] seq={seq} channel={chan} sender_ts={sender_ts:.6f} "
                    f"arrive_ts={arrival_ts:.6f} rtt={rtt*1000:.2f}ms len={payload_len} jitter={j*1000:.3f}ms")
        print(log_line)
        self.receive_log.append((seq, chan, sender_ts, arrival_ts, payload_len, rtt, j))

    def _report_final(self, sender_summary):
        # compute throughput, PDR using sender's counts
        rep = {}
        for name, cs in self.channels.items():
            duration = 0.0
            if cs.first_arrival and cs.last_arrival and cs.last_arrival > cs.first_arrival:
                duration = cs.last_arrival - cs.first_arrival
            throughput = (cs.total_bytes / duration) if duration > 0 else 0.0
            avg_rtt = sum(cs.rtts)/len(cs.rtts) if cs.rtts else 0.0
            rep[name] = {
                "received": cs.recv_count,
                "bytes": cs.total_bytes,
                "duration_s": duration,
                "throughput_Bps": throughput,
                "avg_rtt_s": avg_rtt,
                "jitter_s": cs.jitter_est.J
            }

        # use sender summary to compute PDR
        sent_reliable = sender_summary.get("sent_reliable", 0)
        sent_unreliable = sender_summary.get("sent_unreliable", 0)

        pdr_reliable = (rep["reliable"]["received"] / sent_reliable * 100.0) if sent_reliable > 0 else None
        pdr_unreliable = (rep["unreliable"]["received"] / sent_unreliable * 100.0) if sent_unreliable > 0 else None

        print("\n=== FINAL REPORT ===")
        print("Sender reported sent_reliable=", sent_reliable, "sent_unreliable=", sent_unreliable)
        for ch in ["reliable", "unreliable"]:
            info = rep[ch]
            print(f"\nChannel: {ch.upper()}")
            print(f"  Received: {info['received']}")
            print(f"  Bytes: {info['bytes']}")
            print(f"  Duration: {info['duration_s']:.3f} s")
            print(f"  Throughput: {info['throughput_Bps']:.2f} B/s")
            print(f"  Avg RTT: {info['avg_rtt_s']*1000:.3f} ms")
            print(f"  Jitter (RFC3550 est): {info['jitter_s']*1000:.3f} ms")
            pdr = pdr_reliable if ch == "reliable" else pdr_unreliable
            if pdr is not None:
                print(f"  Packet Delivery Ratio: {pdr:.2f} %")
            else:
                print("  Packet Delivery Ratio: N/A (sender did not report)")

        print("====================\n")

async def run(host, port, certfile, keyfile):
    cfg = QuicConfiguration(is_client=False)
    cfg.load_cert_chain(certfile, keyfile)

    app = ReceiverApp()

    async def handle_connection(protocol: GameQuicProtocol):
        print("[SERVER] New connection")
        app.set_protocol(protocol)

    server = await serve(
        host,
        port,
        configuration=cfg,
        create_protocol=GameQuicProtocol,
        stream_handler=None  # not used; GameQuicProtocol handles datagrams
    )
    print(f"[SERVER] listening on {host}:{port}")
    await asyncio.Event().wait()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--cert", default="cert.pem")
    parser.add_argument("--key", default="key.pem")
    args = parser.parse_args()

    try:
        asyncio.run(run(args.host, args.port, args.cert, args.key))
    except KeyboardInterrupt:
        pass
