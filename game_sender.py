# sender.py
import asyncio
import json
import random
import struct
import time
import argparse
from aioquic.asyncio import connect
from aioquic.quic.configuration import QuicConfiguration
from hquic import GameQuicProtocol

def make_numeric_payload(i):
    return struct.pack(">I", i)

def make_string_payload(i):
    return f"msg-{i}".encode()

def make_json_payload(i):
    return json.dumps({"id": i, "ts": time.time(), "text": f"payload-{i}"}).encode()

async def run(host, port, count, rate, seed, certfile):
    random.seed(seed)
    cfg = QuicConfiguration(is_client=True)
    cfg.max_datagram_frame_size = 65536
    if certfile:
        cfg.load_verify_locations(certfile)

    sent_counts = {"reliable": 0, "unreliable": 0}
    seq_to_meta = {}

    async with connect(host, port, configuration=cfg, create_protocol=GameQuicProtocol) as connection:
        protocol = connection
        api = protocol.api

        print(f"[SENDER] Connected to {host}:{port}. Sending {count} packets at {rate} pkt/s")
        interval = 1.0 / rate if rate > 0 else 0.01

        for i in range(1, count+1):
            # choose channel randomly (50/50)
            reliable = random.random() < 0.5

            # choose payload type: numeric / string / json
            typ = random.choice(["num", "str", "json"])
            if typ == "num":
                payload = make_numeric_payload(i)
            elif typ == "str":
                payload = make_string_payload(i)
            else:
                payload = make_json_payload(i)

            seq = await api.send_data(payload, reliable)
            seq_to_meta[seq] = {
                "sent_ts": time.time(),
                "reliable": reliable,
                "payload_len": len(payload),
                "attempts_observed": 0
            }
            if reliable:
                sent_counts["reliable"] += 1
            else:
                sent_counts["unreliable"] += 1

            print(f"[SENDER] Sent seq={seq} reliable={reliable} len={len(payload)} type={typ}")
            await asyncio.sleep(interval)

        api.print_api_stats()
        # Wait a bit to allow retransmissions & acks to arrive
        await asyncio.sleep(1.0)

        # Build final stats control message and send reliably
        summary = {
            "type": "STATS",
            "sent_reliable": sent_counts["reliable"],
            "sent_unreliable": sent_counts["unreliable"],
            "total_sent": sent_counts["reliable"] + sent_counts["unreliable"],
            "run_ts": time.time()
        }
        print(summary)
        payload = json.dumps(summary).encode()
        seq = await api.send_data(payload, reliable=True)
        print(f"[SENDER] Sent final STATS (seq={seq})")

        # Wait for acks & allow receiver to print
        await asyncio.sleep(2.0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--rate", type=float, default=50.0, help="packets per second")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--certfile", default=None, help="CA cert to trust (optional)")
    args = parser.parse_args()

    try:
        asyncio.run(run(args.host, args.port, args.count, args.rate, args.seed, args.certfile))
    except KeyboardInterrupt:
        pass
