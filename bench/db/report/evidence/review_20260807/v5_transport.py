#!/usr/bin/env python3
"""Claim E: is PostgreSQL really reached through an equivalent docker-proxy relay?

Instrument: a bare 8-byte PostgreSQL SSLRequest round trip (server answers one
byte) and a bare MongoDB OP_MSG ping, each issued to (a) the published host port
and (b) the container IP.  Same client, same loop, same host.  Also samples the
CPU of each docker-proxy process across the loop.
"""
import socket, struct, statistics, time, sys

PROXY = {"mongo": 1041268, "pg": 2219851}


def proc_cpu_us(pid):
    with open(f"/proc/{pid}/stat") as fh:
        f = fh.read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) * 10000.0  # utime+stime ticks -> us


def pg_ssl_rt(host, port, n):
    lat = []
    s = socket.create_connection((host, port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    pkt = struct.pack("!ii", 8, 80877103)  # SSLRequest
    for _ in range(n):
        t = time.perf_counter()
        s.sendall(pkt)
        r = s.recv(1)
        lat.append((time.perf_counter() - t) * 1e6)
        if not r:
            raise SystemExit("closed")
    s.close()
    lat.sort()
    return lat


def probe(label, host, port, pid, n=4000):
    c0 = proc_cpu_us(pid)
    lat = pg_ssl_rt(host, port, n)
    c1 = proc_cpu_us(pid)
    print(f"{label:<34} p50 {lat[len(lat)//2]:7.1f} us  p95 {lat[int(n*.95)]:7.1f}  "
          f"proxy_cpu {(c1-c0)/n:7.2f} us/op")
    return lat[len(lat)//2]


print("PostgreSQL bare SSLRequest round trip (8 bytes out, 1 byte back)")
a = probe("pg published 127.0.0.1:55432", "127.0.0.1", 55432, PROXY["pg"])
b = probe("pg container  172.17.0.2:5432", "172.17.0.2", 5432, PROXY["pg"])
print(f"  docker-proxy costs PostgreSQL {a-b:+.1f} us per round trip\n")

print("MongoDB same instrument, using its own relay")
c = probe("mongo published 127.0.0.1:57017", "127.0.0.1", 57017, PROXY["mongo"])
d = probe("mongo container 172.17.0.3:27017", "172.17.0.3", 27017, PROXY["mongo"])
print(f"  docker-proxy costs MongoDB   {c-d:+.1f} us per round trip")
